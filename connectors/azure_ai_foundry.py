"""
Azure AI Foundry connector — deployment usage and tool manifest inference.

Azure AI Foundry (formerly Azure Machine Learning / AI Studio) hosts model
deployments and agent projects. This connector pulls token consumption from
the Azure Monitor Metrics API and deployment metadata from the AI Foundry
management API to infer manifest fragments.

Requires azure-identity and azure-monitor-query packages:
    pip install azure-identity azure-monitor-query requests

Authentication: Azure AD service principal or managed identity with
'Cognitive Services User' and 'Monitoring Reader' roles.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta

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


class AzureAIFoundryConnector(UsageConnector):
    """Pulls Azure AI Foundry deployment usage from Azure Monitor."""

    def __init__(
        self,
        subscription_id: str,
        resource_group: str,
        workspace_name: str,
        team_id: str = "",
        credential=None,  # azure.identity credential; defaults to DefaultAzureCredential
    ):
        self._subscription_id = subscription_id
        self._resource_group = resource_group
        self._workspace_name = workspace_name
        self._team_id = team_id
        self._credential = credential

    def source_name(self) -> str:
        return "azure_ai_foundry"

    def _get_credential(self):
        if self._credential:
            return self._credential
        try:
            from azure.identity import DefaultAzureCredential
            return DefaultAzureCredential()
        except ImportError as exc:
            raise ImportError(
                "azure-identity is required for AzureAIFoundryConnector. "
                "Install it with: pip install azure-identity"
            ) from exc

    def pull(self, since: datetime) -> list[UsageEvent]:
        """Pull token usage from Azure Monitor Metrics for AI Foundry deployments."""
        try:
            from azure.monitor.query import MetricsQueryClient
            from azure.monitor.query import MetricAggregationType
        except ImportError as exc:
            raise ImportError(
                "azure-monitor-query is required for AzureAIFoundryConnector. "
                "Install it with: pip install azure-monitor-query"
            ) from exc

        events: list[UsageEvent] = []
        credential = self._get_credential()

        resource_id = (
            f"/subscriptions/{self._subscription_id}"
            f"/resourceGroups/{self._resource_group}"
            f"/providers/Microsoft.MachineLearningServices"
            f"/workspaces/{self._workspace_name}"
        )

        try:
            client = MetricsQueryClient(credential)
            result = client.query_resource(
                resource_uri=resource_id,
                metric_names=["TokenTransaction"],
                timespan=(since, datetime.now()),
                granularity=timedelta(hours=1),
                aggregations=[MetricAggregationType.TOTAL],
            )

            for metric in result.metrics:
                for ts_item in metric.timeseries:
                    model = "unknown"
                    agent_id = None
                    # Extract model from dimension values if available
                    for dim in (ts_item.metadata_values or []):
                        if dim.name and "model" in dim.name.lower():
                            model = dim.value or model
                        if dim.name and "deployment" in dim.name.lower():
                            agent_id = dim.value

                    for point in ts_item.data:
                        if point.total is None or point.timestamp is None:
                            continue
                        ts = point.timestamp.replace(tzinfo=None)
                        if ts < since:
                            continue
                        qty, unit_cost = normalise_token_count(int(point.total), model)
                        events.append(UsageEvent(
                            timestamp=ts,
                            actor_id=agent_id or self._workspace_name,
                            team_id=self._team_id,
                            source_app=SourceApp.AZURE_AI_FOUNDRY,
                            resource_type=ResourceType.TOKENS,
                            quantity=qty,
                            unit_cost_usd=unit_cost,
                            model=model,
                            agent_id=agent_id,
                            metadata={"workspace": self._workspace_name},
                        ))

        except Exception as exc:
            logger.warning("AzureAIFoundryConnector.pull failed: %s", exc)

        return events

    def pull_manifest_fragments(
        self, since: datetime
    ) -> list[AgentManifestFragment]:
        """Infer manifest fragments from AI Foundry project/deployment metadata."""
        try:
            import requests
            from azure.identity import DefaultAzureCredential
        except ImportError:
            return []

        fragments: list[AgentManifestFragment] = []
        try:
            credential = self._get_credential()
            token = credential.get_token(
                "https://management.azure.com/.default"
            ).token

            headers = {"Authorization": f"Bearer {token}"}
            url = (
                f"https://management.azure.com/subscriptions/{self._subscription_id}"
                f"/resourceGroups/{self._resource_group}"
                f"/providers/Microsoft.MachineLearningServices"
                f"/workspaces/{self._workspace_name}"
                f"/onlineEndpoints?api-version=2024-04-01"
            )
            resp = requests.get(url, headers=headers, timeout=30)
            resp.raise_for_status()

            for endpoint in resp.json().get("value", []):
                agent_id = endpoint.get("name", "")
                properties = endpoint.get("properties", {})
                # AI Foundry endpoints can hold keys (credentials)
                auth_mode = properties.get("authMode", "").lower()
                credentials_held = auth_mode in ("key", "amltoken")

                fragments.append(build_manifest_fragment(
                    agent_id=agent_id,
                    source=DiscoverySource.CONNECTOR,
                    observed_at=datetime.now(),
                    credentials_held=credentials_held,
                    execution_rights=False,  # endpoints serve models, don't execute code
                ))

        except Exception as exc:
            logger.warning(
                "AzureAIFoundryConnector.pull_manifest_fragments failed: %s", exc
            )

        return fragments
