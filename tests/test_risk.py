"""Tests for the risk assessment engine."""

from datetime import datetime, timedelta

import pytest

from core.models import (
    Agent,
    AgentManifest,
    AgentStatus,
    AgentTier,
    AutonomyControl,
    DiscoverySource,
    RegulatoryFlag,
    ResourceType,
    SourceApp,
    UsageEvent,
)
from core.risk import (
    assess_risk,
    detect_burn_rate_spike,
    detect_manifest_change,
)


def _make_agent(tier: AgentTier = AgentTier.TIER_1) -> Agent:
    return Agent(
        agent_id="agent-risk",
        name="Risk Test Agent",
        owner_id="owner-1",
        team_id="team-a",
        source_platform=SourceApp.AI_GATEWAY,
        status=AgentStatus.ACTIVE,
        discovery_source=DiscoverySource.SELF_REGISTRATION,
        discovered_at=datetime.now(),
        tier=tier,
    )


def _make_manifest(**kwargs) -> AgentManifest:
    defaults = {
        "data_scope": "invoker_only",
        "tool_list": ["search"],
        "execution_rights": False,
        "credentials_held": False,
        "writes_to_system_of_record": False,
        "reaches_external_party": False,
        "distributes_output_autonomously": False,
    }
    defaults.update(kwargs)
    return AgentManifest(**defaults)


class TestAutonomyMatrix:
    def test_low_blast_reversible_is_let_run(self):
        agent = _make_agent(AgentTier.TIER_1)
        manifest = _make_manifest()
        result = assess_risk(agent, manifest, blast_radius="low", reversibility="reversible")
        assert result.autonomy_control == AutonomyControl.LET_RUN

    def test_low_blast_irreversible_is_detect_fast(self):
        agent = _make_agent(AgentTier.TIER_2)
        manifest = _make_manifest(reaches_external_party=True)
        result = assess_risk(agent, manifest, blast_radius="low", reversibility="irreversible")
        assert result.autonomy_control == AutonomyControl.DETECT_FAST

    def test_high_blast_reversible_is_rate_limit(self):
        agent = _make_agent(AgentTier.TIER_2)
        manifest = _make_manifest(data_scope="beyond_invoker")
        result = assess_risk(agent, manifest, blast_radius="high", reversibility="reversible")
        assert result.autonomy_control == AutonomyControl.RATE_LIMIT

    def test_high_blast_irreversible_is_human_gate(self):
        agent = _make_agent(AgentTier.TIER_3)
        manifest = _make_manifest(execution_rights=True, reaches_external_party=True)
        result = assess_risk(agent, manifest, blast_radius="high", reversibility="irreversible")
        assert result.autonomy_control == AutonomyControl.HUMAN_GATE

    def test_tier3_inferred_as_high_blast(self):
        agent = _make_agent(AgentTier.TIER_3)
        manifest = _make_manifest(execution_rights=True)
        result = assess_risk(agent, manifest)
        assert result.blast_radius == "high"

    def test_external_party_inferred_as_irreversible(self):
        agent = _make_agent(AgentTier.TIER_2)
        manifest = _make_manifest(reaches_external_party=True)
        result = assess_risk(agent, manifest)
        assert result.reversibility == "irreversible"


class TestRegulatoryFlags:
    def test_external_party_raises_external_facing_flag(self):
        agent = _make_agent(AgentTier.TIER_2)
        manifest = _make_manifest(reaches_external_party=True)
        result = assess_risk(agent, manifest)
        assert RegulatoryFlag.EXTERNAL_FACING in result.regulatory_flags

    def test_confidential_others_raises_personal_data_flag(self):
        agent = _make_agent(AgentTier.TIER_2)
        manifest = _make_manifest(
            grounding_includes_confidential_of_others=True,
            data_scope="beyond_invoker",
            data_sources=["hr-system"],
        )
        result = assess_risk(agent, manifest)
        assert RegulatoryFlag.PERSONAL_DATA in result.regulatory_flags


class TestManifestChangeDetection:
    def test_no_change_returns_none(self):
        manifest = _make_manifest()
        result = detect_manifest_change("agent-1", manifest, manifest)
        assert result is None

    def test_new_tool_triggers_reclassification(self):
        old = _make_manifest(tool_list=["search"])
        new = _make_manifest(tool_list=["search", "email"])
        result = detect_manifest_change("agent-1", old, new)
        assert result is not None
        assert "email" in result.reason

    def test_data_scope_change_triggers_reclassification(self):
        old = _make_manifest(data_scope="invoker_only")
        new = _make_manifest(data_scope="beyond_invoker")
        result = detect_manifest_change("agent-1", old, new)
        assert result is not None
        assert "data_scope" in result.reason

    def test_new_sub_agent_triggers_reclassification(self):
        old = _make_manifest()
        new = _make_manifest()
        new.sub_agent_ids = ["new-sub"]
        result = detect_manifest_change("agent-1", old, new)
        assert result is not None
        assert "sub-agent" in result.reason.lower()

    def test_first_manifest_never_triggers(self):
        new = _make_manifest()
        result = detect_manifest_change("agent-1", None, new)
        assert result is None


class TestBurnRateSpike:
    def _make_events(self, agent_id: str, daily_tokens: list[float]) -> list[UsageEvent]:
        base = datetime(2026, 1, 1)
        events = []
        for i, tokens in enumerate(daily_tokens):
            events.append(UsageEvent(
                timestamp=base + timedelta(days=i),
                actor_id="user-1",
                team_id="team-a",
                source_app=SourceApp.AI_GATEWAY,
                resource_type=ResourceType.TOKENS,
                quantity=tokens,
                unit_cost_usd=0.000001,
                model="gpt-4o",
                agent_id=agent_id,
            ))
        return events

    def test_spike_detected_above_threshold(self):
        # Stable at 1000, then spikes to 5000
        tokens = [1000.0] * 14 + [5000.0]
        events = self._make_events("agent-1", tokens)
        result = detect_burn_rate_spike("agent-1", events)
        assert result is not None
        assert "spike" in result.reason.lower()

    def test_no_spike_for_stable_usage(self):
        tokens = [1000.0] * 15
        events = self._make_events("agent-1", tokens)
        result = detect_burn_rate_spike("agent-1", events)
        assert result is None

    def test_no_spike_for_missing_agent(self):
        tokens = [1000.0] * 15
        events = self._make_events("agent-1", tokens)
        result = detect_burn_rate_spike("other-agent", events)
        assert result is None

    def test_too_few_events_returns_none(self):
        events = self._make_events("agent-1", [1000.0, 5000.0])
        result = detect_burn_rate_spike("agent-1", events)
        assert result is None
