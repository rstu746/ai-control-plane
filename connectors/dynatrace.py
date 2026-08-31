"""
Dynatrace connector — pull AI observability spans and normalise into
UsageEvents and AgentManifestFragments.

Dynatrace captures distributed traces including LLM API calls. This connector
queries the Dynatrace API for AI-related spans, extracts token counts and
model names, and normalises them into the control plane's canonical event format.

Requires requests package:
    pip install requests

Configure with your Dynatrace environment URL and API token (scope:
metrics.read, events.read, or Davis® AI platform scope depending on your setup).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from connectors.base import UsageConnector
from core.models import (
    AgentManifestFragment,
    DiscoverySource,
    ResourceType,
    SourceApp,
    UsageEvent,
)
from core.observability import build_manifest_fragment, normalise_token_count

logger = logging.getLogger(__name__)


class DynatraceConnector(UsageConnector):
    """Pulls Dynatrace distributed traces for AI workloads."""

    def __init__(
        self,
        environment_url: str,
        api_token: str,
        team_id: str = "",
        entity_selector: str = "type(SERVICE),tag(ai-control-plane)",
    ):
        """
        environment_url: e.g. "https://xyz.live.dynatrace.com"
        api_token: Dynatrace API token with metrics.read / events.read scope
        team_id: default team to attribute events to
        entity_selector: DQL entity selector to scope the query to AI services
        """
        self._base_url = environment_url.rstrip("/")
        self._api_token = api_token
        self._team_id = team_id
        self._entity_selector = entity_selector

    def source_name(self) -> str:
        return "dynatrace"

    def _headers(self) -> dict:
        return {
            "Authorization": f"Api-Token {self._api_token}",
            "Content-Type": "application/json",
        }

    def pull(self, since: datetime) -> list[UsageEvent]:
        """Query Dynatrace Metrics API for LLM token consumption metrics."""
        try:
            import requests
        except ImportError as exc:
            raise ImportError(
                "requests is required for DynatraceConnector. "
                "Install it with: pip install requests"
            ) from exc

        events: list[UsageEvent] = []

        # Query token usage via Dynatrace Metrics v2 API
        # Metric key convention: ext.ai.llm.tokens.total (custom metric pushed by instrumentation)
        since_ms = int(since.replace(tzinfo=timezone.utc).timestamp() * 1000)
        params = {
            "metricSelector": "ext.ai.llm.tokens.total",
            "resolution": "1h",
            "from": since_ms,
            "entitySelector": self._entity_selector,
        }

        try:
            resp = requests.get(
                f"{self._base_url}/api/v2/metrics/query",
                headers=self._headers(),
                params=params,
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()

            for result in data.get("resolution", {}).get("results", []):
                entity_id = result.get("dimensionMap", {}).get("dt.entity.service", "unknown")
                model = result.get("dimensionMap", {}).get("ai.model", "unknown")
                actor_id = result.get("dimensionMap", {}).get("ai.actor_id", entity_id)
                agent_id = result.get("dimensionMap", {}).get("ai.agent_id", None)
                team_dim = result.get("dimensionMap", {}).get("ai.team_id", self._team_id)

                for point in result.get("data", []):
                    ts_ms = point.get("timestamps", [None])[0]
                    value = point.get("values", [None])[0]
                    if ts_ms is None or value is None:
                        continue

                    ts = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).replace(tzinfo=None)
                    qty, unit_cost = normalise_token_count(int(value), model)

                    events.append(UsageEvent(
                        timestamp=ts,
                        actor_id=actor_id,
                        team_id=team_dim,
                        source_app=SourceApp.DYNATRACE,
                        resource_type=ResourceType.TOKENS,
                        quantity=qty,
                        unit_cost_usd=unit_cost,
                        model=model,
                        agent_id=agent_id,
                        metadata={"dynatrace_entity_id": entity_id},
                    ))

        except Exception as exc:
            logger.warning("DynatraceConnector.pull (metrics) failed: %s", exc)

        return events

    def pull_manifest_fragments(
        self, since: datetime
    ) -> list[AgentManifestFragment]:
        """Query Dynatrace for AI agent service metadata to build manifest fragments.
        Uses custom properties set on Dynatrace monitored entities."""
        try:
            import requests
        except ImportError:
            return []

        fragments: list[AgentManifestFragment] = []
        try:
            resp = requests.get(
                f"{self._base_url}/api/v2/entities",
                headers=self._headers(),
                params={
                    "entitySelector": self._entity_selector,
                    "fields": "+properties,+tags",
                    "pageSize": 500,
                },
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()

            for entity in data.get("entities", []):
                props = entity.get("properties", {})
                agent_id = props.get("ai.agent_id") or entity.get("entityId")
                tool_list = [t.strip() for t in props.get("ai.tools", "").split(",") if t.strip()]
                data_sources = [s.strip() for s in props.get("ai.data_sources", "").split(",") if s.strip()]
                exec_rights_raw = props.get("ai.execution_rights", "").lower()
                execution_rights = True if exec_rights_raw == "true" else (
                    False if exec_rights_raw == "false" else None
                )

                if not agent_id:
                    continue

                fragments.append(build_manifest_fragment(
                    agent_id=agent_id,
                    source=DiscoverySource.CONNECTOR,
                    observed_at=datetime.now(),
                    tool_names=tool_list,
                    data_sources=data_sources,
                    execution_rights=execution_rights,
                ))

        except Exception as exc:
            logger.warning("DynatraceConnector.pull_manifest_fragments failed: %s", exc)

        return fragments
