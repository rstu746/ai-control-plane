"""
GitHub Copilot connector — seat usage and per-user token activity.

Pulls from the GitHub Copilot Business/Enterprise Usage API. GitHub Copilot
is seat-billed (not token-billed), so UsageEvents use ResourceType.SEAT for
the licence cost and token counts from the "suggestions" metrics where
available.

Requires requests package:
    pip install requests

Authentication: GitHub PAT or GitHub App installation token with the
'manage_billing:copilot' scope.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

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


class GitHubCopilotConnector(UsageConnector):
    """Pulls GitHub Copilot seat usage from the GitHub API."""

    def __init__(
        self,
        org: str,
        token: str,
        team_id: str = "",
        seat_cost_usd: float = 19.0,  # per seat per month — update to match your plan
    ):
        self._org = org
        self._token = token
        self._team_id = team_id
        self._seat_cost_usd = seat_cost_usd

    def source_name(self) -> str:
        return "github_copilot"

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def pull(self, since: datetime) -> list[UsageEvent]:
        """Pull Copilot usage metrics. Returns one UsageEvent per active seat
        per day in the requested window."""
        try:
            import requests
        except ImportError as exc:
            raise ImportError(
                "requests is required for GitHubCopilotConnector. "
                "Install it with: pip install requests"
            ) from exc

        events: list[UsageEvent] = []
        daily_cost = self._seat_cost_usd / 30  # rough daily cost per seat

        try:
            # GitHub Copilot Usage API — daily breakdown
            resp = requests.get(
                f"https://api.github.com/orgs/{self._org}/copilot/usage",
                headers=self._headers(),
                timeout=30,
            )
            resp.raise_for_status()
            usage_data = resp.json()

            for day in usage_data:
                day_str = day.get("day", "")
                try:
                    day_dt = datetime.strptime(day_str, "%Y-%m-%d")
                except ValueError:
                    continue
                if day_dt < since:
                    continue

                total_active = day.get("total_active_users", 0)
                total_suggestions = day.get("total_suggestions_count", 0)
                total_acceptances = day.get("total_acceptances_count", 0)

                # One aggregate event per day for the org
                events.append(UsageEvent(
                    timestamp=day_dt,
                    actor_id=f"{self._org}/copilot",
                    team_id=self._team_id,
                    source_app=SourceApp.GITHUB_COPILOT,
                    resource_type=ResourceType.SEAT,
                    quantity=float(total_active),
                    unit_cost_usd=daily_cost,
                    model="github-copilot",
                    metadata={
                        "suggestions": total_suggestions,
                        "acceptances": total_acceptances,
                        "acceptance_rate": (
                            round(total_acceptances / total_suggestions, 3)
                            if total_suggestions else 0
                        ),
                    },
                ))

        except Exception as exc:
            logger.warning("GitHubCopilotConnector.pull failed: %s", exc)

        return events

    def pull_manifest_fragments(
        self, since: datetime
    ) -> list[AgentManifestFragment]:
        """GitHub Copilot seats are human-driven coding assistants. They are
        not agents in the tier sense by default (Tier 1 or not in scope).
        Return a fragment indicating no execution rights and invoker-only scope."""
        # Copilot seats don't expose individual user tool lists via API.
        # Return a single org-level fragment with known safe defaults.
        return [
            build_manifest_fragment(
                agent_id=f"{self._org}/copilot",
                source=DiscoverySource.CONNECTOR,
                observed_at=datetime.now(),
                tool_names=["code-completion", "chat"],
                execution_rights=False,
                data_sources=["ide-context"],
            )
        ]
