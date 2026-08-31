"""
Databricks connector — AI/ML workload usage and execution rights inference.

Databricks AI workloads (Model Serving endpoints, DBRX, Mosaic AI) consume
compute credits. This connector pulls cluster compute usage for AI-tagged
workloads and infers execution rights (Databricks always has execution rights
when running notebooks or jobs — this is a Tier 3 signal by default).

Requires requests package:
    pip install requests

Authentication: Databricks personal access token or service principal token.
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


class DatabricksConnector(UsageConnector):
    """Pulls Databricks AI workload usage."""

    def __init__(
        self,
        workspace_url: str,
        token: str,
        team_id: str = "",
        ai_cluster_tags: list[str] | None = None,
    ):
        """
        workspace_url: e.g. "https://adb-1234567890.azuredatabricks.net"
        token: Databricks PAT or service principal token
        ai_cluster_tags: cluster tags used to identify AI workloads
        """
        self._workspace_url = workspace_url.rstrip("/")
        self._token = token
        self._team_id = team_id
        self._ai_cluster_tags = ai_cluster_tags or ["ai", "ml", "llm", "genai"]

    def source_name(self) -> str:
        return "databricks"

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._token}"}

    def pull(self, since: datetime) -> list[UsageEvent]:
        """Pull cluster usage for AI-tagged clusters."""
        try:
            import requests
        except ImportError as exc:
            raise ImportError(
                "requests is required for DatabricksConnector. "
                "Install it with: pip install requests"
            ) from exc

        events: list[UsageEvent] = []
        since_ms = int(since.replace(tzinfo=timezone.utc).timestamp() * 1000)

        try:
            # List clusters and filter to AI-tagged ones
            resp = requests.get(
                f"{self._workspace_url}/api/2.0/clusters/list",
                headers=self._headers(),
                timeout=30,
            )
            resp.raise_for_status()
            clusters = resp.json().get("clusters", [])

            ai_cluster_ids = []
            for cluster in clusters:
                tags = cluster.get("custom_tags", {})
                tag_values = [v.lower() for v in list(tags.keys()) + list(tags.values())]
                if any(ai_tag in " ".join(tag_values) for ai_tag in self._ai_cluster_tags):
                    ai_cluster_ids.append(cluster["cluster_id"])

            # Pull job runs for AI clusters
            runs_resp = requests.get(
                f"{self._workspace_url}/api/2.1/jobs/runs/list",
                headers=self._headers(),
                params={"start_time_from": since_ms, "limit": 100},
                timeout=30,
            )
            runs_resp.raise_for_status()
            runs = runs_resp.json().get("runs", [])

            for run in runs:
                cluster_id = run.get("cluster_instance", {}).get("cluster_id", "")
                if ai_cluster_ids and cluster_id not in ai_cluster_ids:
                    continue

                start_ms = run.get("start_time", 0)
                ts = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc).replace(tzinfo=None)
                if ts < since:
                    continue

                dbu = float(run.get("cluster_spec", {}).get("autoscale", {}).get("max_workers", 1))
                actor = run.get("creator_user_name", "unknown")
                job_id = str(run.get("job_id", ""))

                events.append(UsageEvent(
                    timestamp=ts,
                    actor_id=actor,
                    team_id=self._team_id,
                    source_app=SourceApp.DATABRICKS,
                    resource_type=ResourceType.COMPUTE_CREDIT,
                    quantity=dbu,
                    unit_cost_usd=0.0,  # DBU cost depends on cluster type and instance
                    model="databricks-ai",
                    agent_id=job_id or cluster_id or None,
                    metadata={
                        "run_id": run.get("run_id"),
                        "cluster_id": cluster_id,
                        "state": run.get("state", {}).get("life_cycle_state"),
                    },
                ))

        except Exception as exc:
            logger.warning("DatabricksConnector.pull failed: %s", exc)

        return events

    def pull_manifest_fragments(
        self, since: datetime
    ) -> list[AgentManifestFragment]:
        """Databricks notebooks and jobs always have execution rights —
        Tier 3 by definition for any agent running there."""
        return [
            build_manifest_fragment(
                agent_id="databricks",
                source=DiscoverySource.CONNECTOR,
                observed_at=datetime.now(),
                tool_names=["notebook", "job", "model-serving"],
                data_sources=["delta-lake"],
                execution_rights=True,  # Always — Databricks executes code
            )
        ]
