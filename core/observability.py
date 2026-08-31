"""
Observability normalisation layer.

LangSmith and Dynatrace are pull-only sources: the control plane reads from
them; it does not push back. This module provides shared normalisation
utilities used by both connectors.

Normalisation contract:
  - Every run/trace/span is normalised into a UsageEvent
  - Where the run reveals agent structure (tool calls, model used) an
    AgentManifestFragment is also emitted
  - The agent_id field on UsageEvent is the run's "agent name" or "session
    owner" as reported by the observability platform — callers should merge
    these with the agent registry to confirm or create registry entries
"""

from __future__ import annotations

from datetime import datetime

from core.models import (
    AgentManifestFragment,
    DiscoverySource,
    ResourceType,
    SourceApp,
    UsageEvent,
)


def normalise_token_count(
    token_count: int | None,
    model: str | None,
    unit_cost_usd_per_token: float = 0.0,
) -> tuple[float, float]:
    """Return (quantity, unit_cost_usd). If no token count is available,
    returns (0.0, 0.0) so the event can still be recorded for attribution."""
    if token_count is None:
        return 0.0, 0.0
    return float(token_count), unit_cost_usd_per_token


def build_manifest_fragment(
    agent_id: str,
    source: DiscoverySource,
    observed_at: datetime,
    tool_names: list[str] | None = None,
    mcp_servers: list[str] | None = None,
    data_sources: list[str] | None = None,
    execution_rights: bool | None = None,
) -> AgentManifestFragment:
    """Build a partial manifest fragment from observability data."""
    return AgentManifestFragment(
        agent_id=agent_id,
        source=source,
        observed_at=observed_at,
        tool_list=tool_names or [],
        mcp_servers=mcp_servers or [],
        data_sources=data_sources or [],
        execution_rights=execution_rights,
    )
