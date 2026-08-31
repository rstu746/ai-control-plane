"""
Snowflake Cortex connector — token usage and execution rights inference.

Snowflake Cortex provides LLM functions (COMPLETE, SUMMARIZE, etc.) that can
run code against the warehouse. The critical governance question is whether
the deployment is in suggest-only mode or has execution rights — this
determines Tier 3 vs Tier 2 classification.

Requires snowflake-connector-python:
    pip install snowflake-connector-python

Authentication: Snowflake user with USAGE on the database and SELECT on
SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY.
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
from core.observability import build_manifest_fragment, normalise_token_count

logger = logging.getLogger(__name__)


class SnowflakeCortexConnector(UsageConnector):
    """Pulls Snowflake Cortex LLM function usage."""

    def __init__(
        self,
        connection_params: dict,
        team_id: str = "",
        has_execution_rights: bool | None = None,
    ):
        """
        connection_params: dict passed to snowflake.connector.connect()
        team_id: team to attribute events to
        has_execution_rights: True if this deployment can execute generated code
          (e.g. Cortex Analyst with EXECUTE IMMEDIATE rights). None = unknown.
        """
        self._connection_params = connection_params
        self._team_id = team_id
        self._has_execution_rights = has_execution_rights

    def source_name(self) -> str:
        return "snowflake"

    def _get_connection(self):
        try:
            import snowflake.connector
        except ImportError as exc:
            raise ImportError(
                "snowflake-connector-python is required for SnowflakeCortexConnector. "
                "Install it with: pip install snowflake-connector-python"
            ) from exc
        return snowflake.connector.connect(**self._connection_params)

    def pull(self, since: datetime) -> list[UsageEvent]:
        """Query SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY for Cortex LLM calls."""
        events: list[UsageEvent] = []
        conn = self._get_connection()

        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT
                    START_TIME,
                    USER_NAME,
                    QUERY_TEXT,
                    QUERY_TAG,
                    BYTES_SCANNED,
                    CREDITS_USED_CLOUD_SERVICES
                FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
                WHERE START_TIME >= %s
                  AND QUERY_TYPE = 'SELECT'
                  AND (
                      UPPER(QUERY_TEXT) LIKE '%SNOWFLAKE.CORTEX.COMPLETE%'
                      OR UPPER(QUERY_TEXT) LIKE '%SNOWFLAKE.CORTEX.SUMMARIZE%'
                      OR UPPER(QUERY_TEXT) LIKE '%SNOWFLAKE.CORTEX.TRANSLATE%'
                      OR UPPER(QUERY_TEXT) LIKE '%SNOWFLAKE.CORTEX.EXTRACT_ANSWER%'
                  )
                ORDER BY START_TIME ASC
                """,
                (since,),
            )
            rows = cur.fetchall()
            for row in rows:
                start_time, user_name, query_text, query_tag, bytes_scanned, credits = row
                # Approximate token count from bytes (rough heuristic)
                approx_tokens = int((bytes_scanned or 0) / 4)
                qty, unit_cost = normalise_token_count(approx_tokens, "snowflake-cortex")

                events.append(UsageEvent(
                    timestamp=start_time,
                    actor_id=user_name or "unknown",
                    team_id=self._team_id,
                    source_app=SourceApp.SNOWFLAKE,
                    resource_type=ResourceType.TOKENS,
                    quantity=qty,
                    unit_cost_usd=unit_cost,
                    model="snowflake-cortex",
                    metadata={
                        "credits": credits,
                        "query_tag": query_tag,
                    },
                ))
        except Exception as exc:
            logger.warning("SnowflakeCortexConnector.pull failed: %s", exc)
        finally:
            conn.close()

        return events

    def pull_manifest_fragments(
        self, since: datetime
    ) -> list[AgentManifestFragment]:
        """Return a manifest fragment indicating whether this deployment has
        execution rights. This is the key Tier 3 trigger for Snowflake Cortex."""
        return [
            build_manifest_fragment(
                agent_id="snowflake-cortex",
                source=DiscoverySource.CONNECTOR,
                observed_at=datetime.now(),
                tool_names=["cortex.complete", "cortex.summarize"],
                data_sources=["snowflake-warehouse"],
                execution_rights=self._has_execution_rights,
            )
        ]
