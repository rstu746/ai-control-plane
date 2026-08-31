"""
Copilot Studio connector — flow usage and connector manifest inference.

Microsoft Copilot Studio flows are tracked via the Power Platform admin APIs.
This connector pulls per-flow session counts and infers manifest fragments
from the flow's configured connectors (which determine tier: a flow with only
the invoker's own Graph connectors is Tier 1; shared deployments or external
connectors push it to Tier 2).

Requires requests and msal packages:
    pip install requests msal

Authentication: Azure AD app registration with Power Platform API permissions.
"""

from __future__ import annotations

import logging
from datetime import datetime

from connectors.base import UsageConnector
from core.models import (
    AgentManifestFragment,
    DiscoverySource,
    ResourceType,
    SourceApp,
    UsageEvent,
)
from core.observability import build_manifest_fragment

logger = logging.getLogger(__name__)

POWER_PLATFORM_API = "https://api.powerplatform.com"


class CopilotStudioConnector(UsageConnector):
    """Pulls Copilot Studio flow usage and connector metadata."""

    def __init__(
        self,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        environment_id: str,
        team_id: str = "",
    ):
        self._tenant_id = tenant_id
        self._client_id = client_id
        self._client_secret = client_secret
        self._environment_id = environment_id
        self._team_id = team_id
        self._token: str | None = None

    def source_name(self) -> str:
        return "copilot_studio"

    def _get_token(self) -> str:
        if self._token:
            return self._token
        try:
            import msal
        except ImportError as exc:
            raise ImportError(
                "msal is required for CopilotStudioConnector. "
                "Install it with: pip install msal"
            ) from exc

        app = msal.ConfidentialClientApplication(
            self._client_id,
            authority=f"https://login.microsoftonline.com/{self._tenant_id}",
            client_credential=self._client_secret,
        )
        result = app.acquire_token_for_client(
            scopes=["https://api.powerplatform.com/.default"]
        )
        self._token = result.get("access_token", "")
        return self._token

    def pull(self, since: datetime) -> list[UsageEvent]:
        """Pull Copilot Studio session/conversation counts per flow."""
        try:
            import requests
        except ImportError as exc:
            raise ImportError(
                "requests is required for CopilotStudioConnector. "
                "Install it with: pip install requests"
            ) from exc

        events: list[UsageEvent] = []
        try:
            headers = {
                "Authorization": f"Bearer {self._get_token()}",
                "Content-Type": "application/json",
            }
            resp = requests.get(
                f"{POWER_PLATFORM_API}/appmanagement/environments/"
                f"{self._environment_id}/bots",
                headers=headers,
                timeout=30,
            )
            resp.raise_for_status()
            bots = resp.json().get("value", [])

            for bot in bots:
                bot_id = bot.get("id", "")
                bot_name = bot.get("displayName", bot_id)
                sessions = bot.get("sessionCount", 0)

                events.append(UsageEvent(
                    timestamp=datetime.now(),
                    actor_id=bot_id,
                    team_id=self._team_id,
                    source_app=SourceApp.COPILOT_STUDIO,
                    resource_type=ResourceType.TOKENS,
                    quantity=float(sessions),
                    unit_cost_usd=0.0,  # metered separately via Power Platform billing
                    model="copilot-studio",
                    agent_id=bot_id,
                    metadata={"bot_name": bot_name},
                ))

        except Exception as exc:
            logger.warning("CopilotStudioConnector.pull failed: %s", exc)

        return events

    def pull_manifest_fragments(
        self, since: datetime
    ) -> list[AgentManifestFragment]:
        """Infer manifest fragments from each bot's configured connectors.
        A bot with only Graph connectors scoped to the creator is Tier 1 eligible.
        A shared bot (multiple users) or one with premium connectors is Tier 2."""
        try:
            import requests
        except ImportError:
            return []

        fragments: list[AgentManifestFragment] = []
        try:
            headers = {"Authorization": f"Bearer {self._get_token()}"}
            resp = requests.get(
                f"{POWER_PLATFORM_API}/appmanagement/environments/"
                f"{self._environment_id}/bots",
                headers=headers,
                timeout=30,
            )
            resp.raise_for_status()
            bots = resp.json().get("value", [])

            for bot in bots:
                bot_id = bot.get("id", "")
                connectors = [c.get("displayName", "") for c in bot.get("connections", [])]
                is_shared = bot.get("sharedWithCount", 0) > 1

                # Infer data scope: shared deployment → beyond_invoker
                data_scope = "beyond_invoker" if is_shared else "invoker_only"
                # Premium connectors (non-Graph) push to Tier 2
                premium_connectors = [
                    c for c in connectors
                    if "graph" not in c.lower() and c
                ]

                fragments.append(build_manifest_fragment(
                    agent_id=bot_id,
                    source=DiscoverySource.CONNECTOR,
                    observed_at=datetime.now(),
                    tool_names=connectors,
                    data_sources=connectors,
                    execution_rights=False,  # Copilot Studio cannot execute code natively
                ))
                # Manually set data_scope and writes_to_system_of_record
                frag = fragments[-1]
                frag.data_scope = data_scope
                frag.writes_to_system_of_record = bool(premium_connectors)
                frag.reaches_external_party = any(
                    "external" in c.lower() or "email" in c.lower() or "teams" in c.lower()
                    for c in premium_connectors
                )

        except Exception as exc:
            logger.warning("CopilotStudioConnector.pull_manifest_fragments failed: %s", exc)

        return fragments
