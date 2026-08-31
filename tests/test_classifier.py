"""Tests for the rule-based classifier."""

from datetime import datetime

import pytest

from core.classifier import classify
from core.models import (
    Agent,
    AgentManifest,
    AgentStatus,
    AgentTier,
    ClassificationRequest,
    ClassificationResult,
    ControlFlowType,
    CostConstruct,
    DiscoverySource,
    FunctionalRole,
    RegulatoryFlag,
    SourceApp,
)


def _make_agent(owner_id: str = "owner-1", team_id: str = "team-a") -> Agent:
    return Agent(
        agent_id="agent-test",
        name="Test Agent",
        owner_id=owner_id,
        team_id=team_id,
        source_platform=SourceApp.AI_GATEWAY,
        status=AgentStatus.REGISTERED,
        discovery_source=DiscoverySource.SELF_REGISTRATION,
        discovered_at=datetime.now(),
    )


def _make_manifest(**kwargs) -> AgentManifest:
    defaults = {
        "data_scope": "invoker_only",
        "tool_list": ["search"],
        "execution_rights": False,
        "credentials_held": False,
        "can_modify_repos_or_pipelines": False,
        "can_modify_other_agents": False,
        "writes_to_system_of_record": False,
        "reaches_external_party": False,
        "distributes_output_autonomously": False,
        "grounding_includes_confidential_of_others": False,
    }
    defaults.update(kwargs)
    return AgentManifest(**defaults)


# ------------------------------------------------------------------
# Tier 1 — Contained
# ------------------------------------------------------------------

class TestTier1:
    def test_basic_contained_agent_is_tier1(self):
        agent = _make_agent()
        manifest = _make_manifest()
        result = classify(agent, manifest)
        assert isinstance(result, ClassificationResult)
        assert result.tier == AgentTier.TIER_1

    def test_tier1_has_correct_role(self):
        agent = _make_agent()
        manifest = _make_manifest()
        result = classify(agent, manifest)
        # Read-only, no writes, no external: Watcher
        assert result.functional_role == FunctionalRole.WATCHER

    def test_tier1_no_regulatory_flags_by_default(self):
        agent = _make_agent()
        manifest = _make_manifest()
        result = classify(agent, manifest)
        assert result.regulatory_flags == []


# ------------------------------------------------------------------
# Tier 2 — Crossing
# ------------------------------------------------------------------

class TestTier2:
    def test_beyond_invoker_data_scope_is_tier2(self):
        agent = _make_agent()
        manifest = _make_manifest(data_scope="beyond_invoker")
        result = classify(agent, manifest)
        assert isinstance(result, ClassificationResult)
        assert result.tier == AgentTier.TIER_2

    def test_writes_to_system_of_record_is_tier2(self):
        agent = _make_agent()
        manifest = _make_manifest(writes_to_system_of_record=True)
        result = classify(agent, manifest)
        assert result.tier == AgentTier.TIER_2

    def test_reaches_external_party_is_tier2(self):
        agent = _make_agent()
        manifest = _make_manifest(reaches_external_party=True)
        result = classify(agent, manifest)
        assert result.tier == AgentTier.TIER_2

    def test_distributes_autonomously_is_tier2(self):
        agent = _make_agent()
        manifest = _make_manifest(distributes_output_autonomously=True)
        result = classify(agent, manifest)
        assert result.tier == AgentTier.TIER_2

    def test_external_party_raises_external_facing_flag(self):
        agent = _make_agent()
        manifest = _make_manifest(reaches_external_party=True)
        result = classify(agent, manifest)
        assert RegulatoryFlag.EXTERNAL_FACING in result.regulatory_flags

    def test_tier2_striker_role(self):
        agent = _make_agent()
        manifest = _make_manifest(
            writes_to_system_of_record=True,
            data_scope="invoker_only",
        )
        result = classify(agent, manifest)
        assert result.functional_role == FunctionalRole.STRIKER


# ------------------------------------------------------------------
# Tier 3 — Executing
# ------------------------------------------------------------------

class TestTier3:
    def test_execution_rights_is_tier3(self):
        agent = _make_agent()
        manifest = _make_manifest(execution_rights=True)
        result = classify(agent, manifest)
        assert result.tier == AgentTier.TIER_3

    def test_credentials_held_is_tier3(self):
        agent = _make_agent()
        manifest = _make_manifest(credentials_held=True)
        result = classify(agent, manifest)
        assert result.tier == AgentTier.TIER_3

    def test_can_modify_repos_is_tier3(self):
        agent = _make_agent()
        manifest = _make_manifest(can_modify_repos_or_pipelines=True)
        result = classify(agent, manifest)
        assert result.tier == AgentTier.TIER_3

    def test_can_modify_other_agents_is_tier3(self):
        agent = _make_agent()
        manifest = _make_manifest(can_modify_other_agents=True)
        result = classify(agent, manifest)
        assert result.tier == AgentTier.TIER_3

    def test_tier3_artificer_role(self):
        agent = _make_agent()
        manifest = _make_manifest(
            can_modify_repos_or_pipelines=True,
            execution_rights=True,
        )
        result = classify(agent, manifest)
        assert result.functional_role == FunctionalRole.ARTIFICER

    def test_tier3_wins_over_tier2(self):
        """Tier 3 triggers take precedence over Tier 2 triggers."""
        agent = _make_agent()
        manifest = _make_manifest(
            execution_rights=True,
            data_scope="beyond_invoker",
            reaches_external_party=True,
        )
        result = classify(agent, manifest)
        assert result.tier == AgentTier.TIER_3


# ------------------------------------------------------------------
# Summoner floor rule
# ------------------------------------------------------------------

class TestSummonerFloor:
    def test_summoner_inherits_tier3_from_sub_agent(self):
        agent = _make_agent()
        manifest = _make_manifest(
            data_scope="invoker_only",
            sub_agent_ids=["sub-agent-1"],
        )
        result = classify(
            agent, manifest,
            sub_agent_tiers=[AgentTier.TIER_3],
        )
        assert result.tier == AgentTier.TIER_3

    def test_summoner_inherits_tier2_from_sub_agent(self):
        agent = _make_agent()
        manifest = _make_manifest(
            data_scope="invoker_only",
            sub_agent_ids=["sub-agent-1"],
        )
        result = classify(
            agent, manifest,
            sub_agent_tiers=[AgentTier.TIER_2],
        )
        assert result.tier == AgentTier.TIER_2

    def test_summoner_own_tier3_not_lowered_by_tier1_sub(self):
        agent = _make_agent()
        manifest = _make_manifest(execution_rights=True)
        result = classify(
            agent, manifest,
            sub_agent_tiers=[AgentTier.TIER_1],
        )
        assert result.tier == AgentTier.TIER_3


# ------------------------------------------------------------------
# Classification request (incomplete manifest)
# ------------------------------------------------------------------

class TestClassificationRequest:
    def test_missing_owner_raises_request(self):
        agent = _make_agent(owner_id="")
        manifest = _make_manifest()
        result = classify(agent, manifest)
        assert isinstance(result, ClassificationRequest)
        assert "owner_id" in result.missing_fields

    def test_missing_team_raises_request(self):
        agent = _make_agent(team_id="")
        manifest = _make_manifest()
        result = classify(agent, manifest)
        assert isinstance(result, ClassificationRequest)
        assert "team_id" in result.missing_fields

    def test_missing_data_scope_raises_request(self):
        agent = _make_agent()
        manifest = AgentManifest(tool_list=["search"])  # no data_scope
        result = classify(agent, manifest)
        assert isinstance(result, ClassificationRequest)
        assert "data_scope" in result.missing_fields

    def test_no_tools_or_data_sources_raises_request(self):
        agent = _make_agent()
        manifest = AgentManifest(data_scope="invoker_only")  # no tool_list etc.
        result = classify(agent, manifest)
        assert isinstance(result, ClassificationRequest)


# ------------------------------------------------------------------
# Control flow type and agent-type note
# ------------------------------------------------------------------

class TestControlFlowType:
    def test_scripted_is_default(self):
        agent = _make_agent()
        manifest = _make_manifest()
        result = classify(agent, manifest)
        assert isinstance(result, ClassificationResult)
        assert result.control_flow_type == ControlFlowType.SCRIPTED

    def test_agent_type_adds_note(self):
        agent = _make_agent()
        manifest = _make_manifest()
        result = classify(agent, manifest, control_flow_type=ControlFlowType.AGENT)
        assert isinstance(result, ClassificationResult)
        assert result.control_flow_type == ControlFlowType.AGENT
        assert "holistic review" in result.notes.lower()


# ------------------------------------------------------------------
# Functional role inference
# ------------------------------------------------------------------

class TestFunctionalRole:
    def test_sub_agents_imply_summoner(self):
        agent = _make_agent()
        manifest = _make_manifest(
            sub_agent_ids=["sub-1"],
            data_scope="beyond_invoker",
        )
        result = classify(agent, manifest)
        assert isinstance(result, ClassificationResult)
        assert result.functional_role == FunctionalRole.SUMMONER

    def test_code_modifier_is_artificer(self):
        agent = _make_agent()
        manifest = _make_manifest(can_modify_repos_or_pipelines=True)
        result = classify(agent, manifest)
        assert isinstance(result, ClassificationResult)
        assert result.functional_role == FunctionalRole.ARTIFICER
