"""
Risk assessment engine.

Evaluates an agent against the autonomy matrix (blast radius × reversibility)
and infers regulatory flags. Also detects reclassification triggers — changes
in an agent's operational profile that warrant re-running the classifier.

Autonomy matrix (from the governance framework):

             Reversible          Irreversible
  Low BR   | let_run            | detect_fast
  High BR  | rate_limit         | human_gate

Reclassification triggers (any should prompt a new classification run):
  • Tooling or data scope change in manifest
  • New sub-agent becomes reachable
  • Burn rate step-change > 2σ from 28-day baseline (signals new capability)
  • Any capability flag changes value in the manifest

Note: regulatory flags are also inferred by the classifier from the manifest.
This module computes them independently from the risk perspective so the two
can be compared — discrepancies are themselves a signal.
"""

from __future__ import annotations

import logging
import math
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta

from core.models import (
    Agent,
    AgentManifest,
    AgentTier,
    AuditEvent,
    AutonomyControl,
    RegulatoryFlag,
    RiskAssessment,
    UsageEvent,
)

logger = logging.getLogger(__name__)


def assess_risk(
    agent: Agent,
    manifest: AgentManifest,
    blast_radius: str | None = None,
    reversibility: str | None = None,
) -> RiskAssessment:
    """Compute a RiskAssessment for an agent.

    blast_radius and reversibility can be supplied by the caller (from
    self-registration metadata) or inferred from the manifest as a best effort.
    Caller-supplied values always win.
    """
    inferred_blast = _infer_blast_radius(agent, manifest)
    inferred_rev = _infer_reversibility(manifest)

    br = blast_radius or inferred_blast
    rev = reversibility or inferred_rev

    control = _autonomy_matrix(br, rev)
    flags = _infer_regulatory_flags(manifest, agent.tier)

    return RiskAssessment(
        agent_id=agent.agent_id,
        autonomy_control=control,
        blast_radius=br,
        reversibility=rev,
        regulatory_flags=flags,
        assessed_at=datetime.now(),
        notes=_risk_notes(control, flags, agent.tier),
    )


# ------------------------------------------------------------------
# Reclassification trigger detection
# ------------------------------------------------------------------

@dataclass
class ReclassificationTrigger:
    agent_id: str
    reason: str
    triggered_at: datetime


def detect_manifest_change(
    agent_id: str,
    old_manifest: AgentManifest | None,
    new_manifest: AgentManifest,
) -> ReclassificationTrigger | None:
    """Return a trigger if the manifest has changed in a way that warrants
    re-running the classifier (new tool, data scope change, new sub-agent,
    or any capability flag changing value)."""
    if old_manifest is None:
        return None  # First manifest — not a change, it's an initial submission

    changes: list[str] = []

    # New tools or MCP servers
    new_tools = set(new_manifest.tool_list) - set(old_manifest.tool_list)
    if new_tools:
        changes.append(f"new tools: {', '.join(new_tools)}")

    new_mcp = set(new_manifest.mcp_servers) - set(old_manifest.mcp_servers)
    if new_mcp:
        changes.append(f"new MCP servers: {', '.join(new_mcp)}")

    # New sub-agents (Summoner floor rule may change)
    new_subs = set(new_manifest.sub_agent_ids) - set(old_manifest.sub_agent_ids)
    if new_subs:
        changes.append(f"new sub-agents: {', '.join(new_subs)}")

    # Data scope change
    if old_manifest.data_scope != new_manifest.data_scope:
        changes.append(
            f"data_scope changed: {old_manifest.data_scope!r} → {new_manifest.data_scope!r}"
        )

    # Capability flag flips (any False→True is a tier escalation risk)
    flag_pairs = [
        ("execution_rights", old_manifest.execution_rights, new_manifest.execution_rights),
        ("credentials_held", old_manifest.credentials_held, new_manifest.credentials_held),
        ("can_modify_repos_or_pipelines", old_manifest.can_modify_repos_or_pipelines, new_manifest.can_modify_repos_or_pipelines),
        ("can_modify_other_agents", old_manifest.can_modify_other_agents, new_manifest.can_modify_other_agents),
        ("writes_to_system_of_record", old_manifest.writes_to_system_of_record, new_manifest.writes_to_system_of_record),
        ("reaches_external_party", old_manifest.reaches_external_party, new_manifest.reaches_external_party),
        ("distributes_output_autonomously", old_manifest.distributes_output_autonomously, new_manifest.distributes_output_autonomously),
        ("grounding_includes_confidential_of_others", old_manifest.grounding_includes_confidential_of_others, new_manifest.grounding_includes_confidential_of_others),
    ]
    for name, old_val, new_val in flag_pairs:
        if old_val != new_val and new_val is not None:
            changes.append(f"{name}: {old_val} → {new_val}")

    if not changes:
        return None

    return ReclassificationTrigger(
        agent_id=agent_id,
        reason="Manifest change detected: " + "; ".join(changes),
        triggered_at=datetime.now(),
    )


def detect_burn_rate_spike(
    agent_id: str,
    events: list[UsageEvent],
    window_days: int = 28,
    spike_z_score: float = 2.0,
) -> ReclassificationTrigger | None:
    """Return a trigger if the most recent day's token usage is more than
    spike_z_score standard deviations above the rolling window mean.
    A consumption spike almost always means a new tool, data source, or
    wider scope — catch it before the builder declares the change."""
    agent_events = [e for e in events if e.agent_id == agent_id]
    if not agent_events:
        return None

    by_day: dict[datetime, float] = {}
    for e in agent_events:
        day = datetime(e.timestamp.year, e.timestamp.month, e.timestamp.day)
        by_day[day] = by_day.get(day, 0.0) + e.quantity

    if len(by_day) < 3:
        return None  # Not enough history for meaningful statistics

    sorted_days = sorted(by_day.keys())
    cutoff = sorted_days[-1] - timedelta(days=window_days)
    window = [by_day[d] for d in sorted_days if d >= cutoff]

    if len(window) < 2:
        return None

    mean = statistics.mean(window[:-1])  # exclude today from baseline
    std = statistics.pstdev(window[:-1])
    today = window[-1]

    if std == 0:
        # Perfectly flat baseline — use a relative multiplier instead of z-score.
        # Any day more than (1 + spike_z_score)x the baseline mean is flagged.
        if mean > 0 and today > mean * (1 + spike_z_score):
            return ReclassificationTrigger(
                agent_id=agent_id,
                reason=(
                    f"Burn rate spike: {today:,.0f} tokens today vs "
                    f"flat baseline of {mean:,.0f} "
                    f"({today/mean:.1f}x baseline, threshold={(1+spike_z_score):.1f}x). "
                    f"Consumption jumps almost always signal new tooling or wider scope."
                ),
                triggered_at=datetime.now(),
            )
        return None

    z = (today - mean) / std
    if z > spike_z_score:
        return ReclassificationTrigger(
            agent_id=agent_id,
            reason=(
                f"Burn rate spike: {today:,.0f} tokens today vs "
                f"baseline mean {mean:,.0f} (z={z:.1f}, threshold={spike_z_score}). "
                f"Consumption jumps almost always signal new tooling or wider scope."
            ),
            triggered_at=datetime.now(),
        )

    return None


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------

def _autonomy_matrix(blast_radius: str, reversibility: str) -> AutonomyControl:
    """Map the 2x2 matrix to a recommended control."""
    high = blast_radius == "high"
    irreversible = reversibility == "irreversible"

    if not high and not irreversible:
        return AutonomyControl.LET_RUN
    if not high and irreversible:
        return AutonomyControl.DETECT_FAST
    if high and not irreversible:
        return AutonomyControl.RATE_LIMIT
    return AutonomyControl.HUMAN_GATE


def _infer_blast_radius(agent: Agent, manifest: AgentManifest) -> str:
    """Infer blast radius from manifest and tier.
    High if: Tier 2+, reaches external party, shared deployment (agent-metered),
    or has multiple sub-agents."""
    if agent.tier == AgentTier.TIER_3:
        return "high"
    if manifest.reaches_external_party or manifest.distributes_output_autonomously:
        return "high"
    if len(manifest.sub_agent_ids) > 1:
        return "high"
    if manifest.data_scope == "beyond_invoker":
        return "high"
    return "low"


def _infer_reversibility(manifest: AgentManifest) -> str:
    """Infer reversibility from manifest.
    Irreversible if: executes code, holds credentials, reaches external party,
    or distributes output without human review."""
    if (
        manifest.execution_rights
        or manifest.credentials_held
        or manifest.reaches_external_party
        or manifest.distributes_output_autonomously
    ):
        return "irreversible"
    return "reversible"


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

    if manifest.external_targets and any(
        "trad" in t.lower() or "market" in t.lower()
        for t in manifest.external_targets
    ):
        flags.append(RegulatoryFlag.MARKET_FACING)

    return flags


def _risk_notes(
    control: AutonomyControl,
    flags: list[RegulatoryFlag],
    tier: AgentTier,
) -> str:
    parts = []
    if control == AutonomyControl.HUMAN_GATE:
        parts.append(
            "High blast radius + irreversible actions: no autonomy without human approval."
        )
    if control == AutonomyControl.RATE_LIMIT:
        parts.append(
            "High blast radius + reversible actions: rate-limit to prevent systemic impact."
        )
    if RegulatoryFlag.PERSONAL_DATA in flags and tier == AgentTier.TIER_1:
        parts.append(
            "Personal data flag at Tier 1 is unusual — verify data scope declaration."
        )
    return " ".join(parts)
