"""
Rule-based tier classifier.

Applies the governance framework mechanically from an agent's manifest —
no ML, no heuristics. The same rule runs regardless of platform, team, or
build tool. Platform is context; configuration decides the tier.

Classification produces one of two outcomes:
  ClassificationResult  — manifest was complete enough; tier assigned
  ClassificationRequest — manifest too sparse; WorkflowItem will be raised

Tier rules (evaluated in order; first match wins):

  Tier 3 — Executing (any one trigger):
    • execution_rights is True         — runs code
    • credentials_held is True         — holds secrets or long-lived credentials
    • can_modify_repos_or_pipelines    — writes to source control or CI/CD
    • can_modify_other_agents          — can build/modify/delete other agents

  Tier 2 — Crossing (any one trigger, if not already Tier 3):
    • data_scope == "beyond_invoker"   — reaches data beyond the invoking user
    • writes_to_system_of_record       — writes to a persistent system of record
    • reaches_external_party           — sends to customers, suppliers, regulators
    • distributes_output_autonomously  — outputs reach others without human adoption
    • grounding_includes_confidential_of_others — Confidential+ data belonging to others

  Tier 1 — Contained (default if nothing above triggers):
    All contained conditions hold; actions reversible; output returns to invoker.

Summoner floor rule (applied after base tier):
  An orchestrator's effective tier = max(own_tier, max(sub_agent_tiers)).
  This is applied by the registry after classification; the classifier itself
  only sets the agent's base tier from its own manifest.

Control-flow type:
  Scripted — all actions declared in manifest; tierable mechanically.
  Agent    — runtime tool/MCP selection; ALWAYS raises a holistic review
             WorkflowItem, regardless of tier. Never goes through fast path.

Regulatory flags:
  Inferred from manifest fields and the computed tier. A flag attaching at
  Tier 1 is unusual and noted in the result.

Cost construct:
  Caller supplies this (derived from billing model); not inferable from manifest.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from core.models import (
    Agent,
    AgentManifest,
    AgentTier,
    ClassificationRequest,
    ClassificationResult,
    ControlFlowType,
    CostConstruct,
    FunctionalRole,
    RegulatoryFlag,
)

# Minimum fraction of capability flags that must be declared before the
# classifier will attempt to assign a tier.
MINIMUM_COMPLETENESS = 0.5


def classify(
    agent: Agent,
    manifest: AgentManifest,
    sub_agent_tiers: list[AgentTier] | None = None,
    cost_construct: CostConstruct | None = None,
    control_flow_type: ControlFlowType | None = None,
) -> ClassificationResult | ClassificationRequest:
    """Classify an agent from its manifest.

    Returns ClassificationResult if the manifest is sufficient, or
    ClassificationRequest if more information is needed.

    sub_agent_tiers: pass the tiers of all reachable sub-agents so the
    Summoner floor rule can be applied here (optional; registry also applies
    it after the fact via effective_tier()).

    cost_construct: caller-supplied billing model — not inferable from manifest.

    control_flow_type: caller-supplied; defaults to SCRIPTED if not provided.
    Passing AGENT always results in a holistic_review WorkflowItem being raised
    alongside the ClassificationResult (handled by workflow.py caller)."""

    # ------------------------------------------------------------------
    # Completeness check — must pass before classification runs
    # ------------------------------------------------------------------
    missing = _minimum_viable_missing(agent, manifest)
    if missing or manifest.completeness < MINIMUM_COMPLETENESS:
        if not missing:
            missing = manifest.missing_capability_flags
        return ClassificationRequest(
            request_id=str(uuid.uuid4()),
            agent_id=agent.agent_id,
            missing_fields=missing,
            raised_at=datetime.now(),
        )

    # ------------------------------------------------------------------
    # Tier inference
    # ------------------------------------------------------------------
    tier = _infer_tier(manifest)

    # Summoner floor rule
    if sub_agent_tiers:
        tier = _max_tier([tier] + sub_agent_tiers)

    # ------------------------------------------------------------------
    # Functional role inference
    # ------------------------------------------------------------------
    functional_role = _infer_functional_role(manifest, control_flow_type)

    # ------------------------------------------------------------------
    # Regulatory flags
    # ------------------------------------------------------------------
    flags = _infer_regulatory_flags(manifest, tier)

    # ------------------------------------------------------------------
    # Control-flow type (default to SCRIPTED if caller did not specify)
    # ------------------------------------------------------------------
    cft = control_flow_type or ControlFlowType.SCRIPTED

    result = ClassificationResult(
        agent_id=agent.agent_id,
        tier=tier,
        functional_role=functional_role,
        control_flow_type=cft,
        cost_construct=cost_construct,
        regulatory_flags=flags,
        confidence=manifest.completeness,
        classified_at=datetime.now(),
        classified_by="auto",
        notes=_notes(tier, flags, cft),
    )
    return result


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------

def _infer_tier(manifest: AgentManifest) -> AgentTier:
    """Apply tier rules in order. First match wins."""

    # Tier 3 triggers
    if (
        manifest.execution_rights
        or manifest.credentials_held
        or manifest.can_modify_repos_or_pipelines
        or manifest.can_modify_other_agents
    ):
        return AgentTier.TIER_3

    # Tier 2 triggers
    if (
        manifest.data_scope == "beyond_invoker"
        or manifest.writes_to_system_of_record
        or manifest.reaches_external_party
        or manifest.distributes_output_autonomously
        or manifest.grounding_includes_confidential_of_others
    ):
        return AgentTier.TIER_2

    # Default: Tier 1
    return AgentTier.TIER_1


def _infer_functional_role(
    manifest: AgentManifest,
    control_flow_type: ControlFlowType | None,
) -> FunctionalRole:
    """Infer functional role from manifest signals. This is a best-effort
    inference; owners can override via self-registration."""

    # Artificer: modifies code, pipelines, or other agents
    if manifest.can_modify_repos_or_pipelines or manifest.can_modify_other_agents:
        return FunctionalRole.ARTIFICER

    # Summoner: delegates to other agents
    if manifest.sub_agent_ids:
        return FunctionalRole.SUMMONER

    # Watcher: read-only, no writes, no external reach
    if (
        not manifest.writes_to_system_of_record
        and not manifest.reaches_external_party
        and not manifest.distributes_output_autonomously
        and not manifest.execution_rights
        and not manifest.credentials_held
    ):
        return FunctionalRole.WATCHER

    # Default: Striker — does the work
    return FunctionalRole.STRIKER


def _infer_regulatory_flags(
    manifest: AgentManifest, tier: AgentTier
) -> list[RegulatoryFlag]:
    flags: list[RegulatoryFlag] = []

    if manifest.reaches_external_party or manifest.distributes_output_autonomously:
        flags.append(RegulatoryFlag.EXTERNAL_FACING)

    if manifest.grounding_includes_confidential_of_others or (
        manifest.data_scope == "beyond_invoker" and manifest.data_sources
    ):
        flags.append(RegulatoryFlag.PERSONAL_DATA)

    # Market-facing agents that reach external parties in a trading context
    # are inferred from external_targets — caller should refine via manifest
    if manifest.external_targets and any(
        "trad" in t.lower() or "market" in t.lower() or "remit" in t.lower()
        for t in manifest.external_targets
    ):
        flags.append(RegulatoryFlag.MARKET_FACING)

    return flags


def _notes(
    tier: AgentTier,
    flags: list[RegulatoryFlag],
    cft: ControlFlowType,
) -> str:
    parts = []
    if tier == AgentTier.TIER_1 and flags:
        parts.append(
            "Regulatory flags at Tier 1 are unusual — verify grounding scope."
        )
    if cft == ControlFlowType.AGENT:
        parts.append(
            "Agent-type control flow: holistic review WorkflowItem will be raised. "
            "Fast path is not available regardless of individual capability."
        )
    return " ".join(parts)


def _minimum_viable_missing(agent: Agent, manifest: AgentManifest) -> list[str]:
    """Return fields that are missing from the minimum viable set required for
    classification to proceed at all."""
    missing = []
    if not agent.owner_id:
        missing.append("owner_id")
    if not agent.team_id:
        missing.append("team_id")
    if manifest.data_scope is None:
        missing.append("data_scope")
    if (
        not manifest.tool_list
        and not manifest.data_sources
        and manifest.execution_rights is None
    ):
        missing.append("tool_list or data_sources or execution_rights (at least one required)")
    return missing


_TIER_ORDER = {
    AgentTier.UNCLASSIFIED: 0,
    AgentTier.TIER_1: 1,
    AgentTier.TIER_2: 2,
    AgentTier.TIER_3: 3,
}


def _max_tier(tiers: list[AgentTier]) -> AgentTier:
    return max(tiers, key=lambda t: _TIER_ORDER.get(t, 0))
