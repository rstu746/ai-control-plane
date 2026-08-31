"""
LangSmith connector — pull runs/traces and normalise into UsageEvents
and AgentManifestFragments.

LangSmith is a run-tracing platform for LangChain and LangGraph workloads.
Each run has a model, token counts, tool calls, and a session/agent identity.
This connector pulls recent runs and converts them into the control plane's
canonical event format.

Requires langsmith package:
    pip install langsmith

Set LANGSMITH_API_KEY in your environment or pass api_key to the constructor.
"""

from __future__ import annotations

from datetime import datetime

from connectors.base import UsageConnector
from core.models import (
    AgentManifestFragment,
    DiscoverySource,
    ResourceType,
    SourceApp,
    UsageEvent,
)
from core.observability import build_manifest_fragment, normalise_token_count


class LangSmithConnector(UsageConnector):
    """Pulls LangSmith run data and normalises to UsageEvents."""

    def __init__(
        self,
        api_key: str | None = None,
        project_name: str | None = None,
        team_id: str = "",
    ):
        self._api_key = api_key
        self._project_name = project_name
        self._team_id = team_id

    def source_name(self) -> str:
        return "langsmith"

    def _get_client(self):
        try:
            from langsmith import Client
        except ImportError as exc:
            raise ImportError(
                "langsmith is required for LangSmithConnector. "
                "Install it with: pip install langsmith"
            ) from exc
        return Client(api_key=self._api_key)

    def pull(self, since: datetime) -> list[UsageEvent]:
        client = self._get_client()
        events: list[UsageEvent] = []

        try:
            runs = client.list_runs(
                project_name=self._project_name,
                start_time=since,
                run_type="llm",  # only LLM runs have token counts
            )
            for run in runs:
                usage = (run.total_tokens or 0) if hasattr(run, "total_tokens") else 0
                model = getattr(run, "extra", {}).get("invocation_params", {}).get("model_name") or "unknown"
                agent_id = str(getattr(run, "session_id", None) or run.id)
                actor_id = str(getattr(run, "reference_example_id", None) or agent_id)

                qty, unit_cost = normalise_token_count(usage, model)

                events.append(UsageEvent(
                    timestamp=run.start_time or since,
                    actor_id=actor_id,
                    team_id=self._team_id,
                    source_app=SourceApp.LANGSMITH,
                    resource_type=ResourceType.TOKENS,
                    quantity=qty,
                    unit_cost_usd=unit_cost,
                    model=model,
                    agent_id=agent_id,
                    metadata={
                        "run_id": str(run.id),
                        "run_type": run.run_type,
                        "latency_ms": (
                            int((run.end_time - run.start_time).total_seconds() * 1000)
                            if run.end_time and run.start_time else None
                        ),
                        "error": str(run.error) if run.error else None,
                    },
                ))
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning(
                "LangSmithConnector.pull failed: %s", exc
            )

        return events

    def pull_manifest_fragments(
        self, since: datetime
    ) -> list[AgentManifestFragment]:
        """Infer AgentManifestFragments from tool-type runs in LangSmith.
        Each unique tool name seen in a session contributes to that session's
        manifest fragment."""
        client = self._get_client()
        fragments_by_agent: dict[str, AgentManifestFragment] = {}

        try:
            # Pull tool-type runs to discover which tools each agent uses
            tool_runs = client.list_runs(
                project_name=self._project_name,
                start_time=since,
                run_type="tool",
            )
            for run in tool_runs:
                agent_id = str(getattr(run, "session_id", None) or run.id)
                tool_name = getattr(run, "name", None) or "unknown"
                observed_at = run.start_time or since

                if agent_id not in fragments_by_agent:
                    fragments_by_agent[agent_id] = build_manifest_fragment(
                        agent_id=agent_id,
                        source=DiscoverySource.CONNECTOR,
                        observed_at=observed_at,
                        tool_names=[],
                    )

                fragment = fragments_by_agent[agent_id]
                if tool_name not in fragment.tool_list:
                    fragment.tool_list.append(tool_name)
                # Update observed_at to latest
                if observed_at > fragment.observed_at:
                    fragment.observed_at = observed_at

        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning(
                "LangSmithConnector.pull_manifest_fragments failed: %s", exc
            )

        return list(fragments_by_agent.values())
