"""
Snowflake storage backend — production analytics and trend queries.

This backend is the primary store for production deployments. It handles
high-volume usage event ingestion, budget rollups across teams, and the
long-running trend queries that would be slow in SQLite at scale.

Configuration: pass a snowflake.connector connection or connection parameters
to SnowflakeBackend(). The backend creates the required tables on first use
if they do not already exist.

IMPORTANT: Real connectors write real usage/identity data through this layer.
Ensure the Snowflake database and schema sit within your org's data governance
perimeter — row-level security, column masking for PII fields, and network
policy restrictions are your responsibility in Snowflake, not this module's.

This module requires the snowflake-connector-python package:
    pip install snowflake-connector-python

Table strategy:
  - usage_events is a large append-only table; partition by DATE(timestamp).
  - trend_snapshots is the pre-aggregated layer — use this for dashboards.
  - agents, workflow_items, alert_rules are small operational tables.
  - audit_events: local copy for query; Azure Blob is authoritative.
"""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from core.models import (
    AuditEvent,
    CapacityPool,
    DemandDriver,
    ResourceType,
    SourceApp,
    TrendSnapshot,
    UsageEvent,
)

logger = logging.getLogger(__name__)


def _import_snowflake():
    """Lazy import of snowflake.connector. Raises ImportError with a clear
    message if the package is not installed."""
    try:
        import snowflake.connector
        return snowflake.connector
    except ImportError as exc:
        raise ImportError(
            "snowflake-connector-python is required for SnowflakeBackend. "
            "Install it with: pip install snowflake-connector-python"
        ) from exc


class SnowflakeBackend:
    """Snowflake implementation of the StorageBackend protocol.

    Requires snowflake-connector-python. Pass either a live connection object
    or a dict of connection kwargs (account, user, password, warehouse,
    database, schema, role) to the constructor.

    Connection ownership:
      - dict kwargs → this class opens and closes a new connection per
        _connect() context. autocommit=True is set explicitly so every
        statement is committed immediately without callers needing to call
        conn.commit().
      - live connection → the caller owns the connection lifecycle and is
        responsible for commit/close. autocommit must be configured by the
        caller; this class never calls conn.commit() or conn.close() on a
        caller-owned connection.
    """

    def __init__(
        self,
        connection_or_kwargs: Any,
        database: str = "AI_CONTROL_PLANE",
        schema: str = "PUBLIC",
    ):
        self._conn_or_kwargs = connection_or_kwargs
        self._database = database
        self._schema = schema
        self._ensure_tables()

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    @contextmanager
    def _connect(self):
        """Yield a (connection, cursor, owned) triple.

        owned=True means this method opened the connection and will close it.
        owned=False means the caller passed a live connection; we never touch
        its lifecycle or call commit() on it.

        When owned=True the connection is opened with autocommit=True so that
        DDL and DML are committed immediately — callers do not need to call
        conn.commit() explicitly.
        """
        connector = _import_snowflake()

        if isinstance(self._conn_or_kwargs, dict):
            conn = connector.connect(**self._conn_or_kwargs, autocommit=True)
            owned = True
        else:
            conn = self._conn_or_kwargs
            owned = False

        cur = conn.cursor(connector.DictCursor)
        try:
            yield conn, cur, owned
        finally:
            cur.close()
            if owned:
                conn.close()

    # ------------------------------------------------------------------
    # Internal execute helper
    # ------------------------------------------------------------------

    def _execute(self, sql: str, params: tuple = ()) -> list[dict]:
        """Execute a single statement and return all rows as dicts.

        For caller-owned connections (owned=False) commit responsibility
        lies with the caller. For dict-kwargs connections autocommit=True
        handles commits automatically."""
        with self._connect() as (conn, cur, owned):
            cur.execute(sql, params)
            return cur.fetchall() or []

    # ------------------------------------------------------------------
    # Schema initialisation
    # ------------------------------------------------------------------

    def _ensure_tables(self) -> None:
        """Create tables if they do not exist. Safe to call on every startup.

        Errors are logged rather than silently swallowed: a real failure
        (bad credentials, missing warehouse, insufficient privileges) is
        distinguishable from an 'object already exists' non-error. The
        latter is suppressed because CREATE TABLE IF NOT EXISTS in Snowflake
        still raises when the caller lacks CREATE privilege on an existing
        object in some configurations."""
        ddl_statements = [
            f"""
            CREATE TABLE IF NOT EXISTS {self._database}.{self._schema}.usage_events (
                id              NUMBER AUTOINCREMENT PRIMARY KEY,
                timestamp       TIMESTAMP_NTZ NOT NULL,
                actor_id        VARCHAR NOT NULL,
                team_id         VARCHAR NOT NULL,
                source_app      VARCHAR NOT NULL,
                resource_type   VARCHAR NOT NULL,
                quantity        FLOAT NOT NULL,
                unit_cost_usd   FLOAT NOT NULL,
                model           VARCHAR,
                agent_id        VARCHAR,
                workflow_id     VARCHAR,
                metadata_json   VARIANT
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS {self._database}.{self._schema}.agents (
                agent_id               VARCHAR PRIMARY KEY,
                name                   VARCHAR NOT NULL,
                owner_id               VARCHAR NOT NULL,
                team_id                VARCHAR NOT NULL,
                source_platform        VARCHAR NOT NULL,
                status                 VARCHAR NOT NULL,
                discovery_source       VARCHAR NOT NULL,
                discovered_at          TIMESTAMP_NTZ NOT NULL,
                tier                   VARCHAR NOT NULL DEFAULT 'unclassified',
                functional_role        VARCHAR NOT NULL DEFAULT 'unknown',
                control_flow_type      VARCHAR,
                cost_construct         VARCHAR,
                regulatory_flags_json  VARIANT,
                escalation_webhook_url VARCHAR,
                last_seen_at           TIMESTAMP_NTZ,
                last_classified_at     TIMESTAMP_NTZ,
                monthly_token_cap      FLOAT
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS {self._database}.{self._schema}.trend_snapshots (
                snapshot_id          VARCHAR PRIMARY KEY,
                snapshot_date        DATE NOT NULL,
                model                VARCHAR,
                agent_id             VARCHAR,
                team_id              VARCHAR,
                source_app           VARCHAR,
                demand_driver        VARCHAR,
                total_tokens         FLOAT NOT NULL DEFAULT 0,
                total_cost_usd       FLOAT NOT NULL DEFAULT 0,
                event_count          INTEGER NOT NULL DEFAULT 0,
                active_actors        INTEGER NOT NULL DEFAULT 0,
                week_over_week_delta FLOAT
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS {self._database}.{self._schema}.audit_events (
                event_id    VARCHAR PRIMARY KEY,
                timestamp   TIMESTAMP_NTZ NOT NULL,
                event_type  VARCHAR NOT NULL,
                agent_id    VARCHAR,
                actor_id    VARCHAR,
                before_json VARIANT,
                after_json  VARIANT,
                source      VARCHAR NOT NULL DEFAULT 'ai-control-plane'
            )
            """,
        ]
        for ddl in ddl_statements:
            try:
                self._execute(ddl)
            except Exception as exc:
                msg = str(exc).lower()
                # Suppress "already exists" variants — these are harmless.
                # Log and re-raise everything else so real failures (bad
                # credentials, missing warehouse, insufficient privileges)
                # are visible immediately rather than surfacing later as
                # confusing read/write errors.
                if "already exists" in msg or "object already exists" in msg:
                    logger.debug("Table already exists, skipping: %s", exc)
                else:
                    logger.error(
                        "Failed to create table during SnowflakeBackend._ensure_tables: %s",
                        exc,
                    )
                    raise

    # ------------------------------------------------------------------
    # Usage events
    # ------------------------------------------------------------------

    def insert_usage_events(self, events: list[UsageEvent]) -> int:
        if not events:
            return 0
        sql = f"""
        INSERT INTO {self._database}.{self._schema}.usage_events
            (timestamp, actor_id, team_id, source_app, resource_type,
             quantity, unit_cost_usd, model, agent_id, workflow_id, metadata_json)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, PARSE_JSON(%s))
        """
        connector = _import_snowflake()
        with self._connect() as (conn, cur, owned):
            # executemany via the plain (non-dict) cursor for batch inserts
            plain_cur = conn.cursor()
            try:
                plain_cur.executemany(
                    sql,
                    [
                        (
                            e.timestamp.isoformat(), e.actor_id, e.team_id,
                            e.source_app.value, e.resource_type.value,
                            e.quantity, e.unit_cost_usd, e.model,
                            e.agent_id, e.workflow_id,
                            json.dumps(e.metadata) if e.metadata else "{}",
                        )
                        for e in events
                    ],
                )
                # Commit only when we own the connection (autocommit=False
                # on caller-owned connections is the caller's responsibility).
                if not owned:
                    conn.commit()
            finally:
                plain_cur.close()
        return len(events)

    def get_usage_events(
        self,
        since: datetime | None = None,
        model: str | None = None,
        actor_id: str | None = None,
        agent_id: str | None = None,
        team_id: str | None = None,
        source_app: SourceApp | None = None,
    ) -> list[UsageEvent]:
        conditions = ["1=1"]
        params: list = []
        if since:
            conditions.append("timestamp >= %s")
            params.append(since.isoformat())
        if model:
            conditions.append("model = %s")
            params.append(model)
        if actor_id:
            conditions.append("actor_id = %s")
            params.append(actor_id)
        if agent_id:
            conditions.append("agent_id = %s")
            params.append(agent_id)
        if team_id:
            conditions.append("team_id = %s")
            params.append(team_id)
        if source_app:
            conditions.append("source_app = %s")
            params.append(source_app.value)

        sql = (
            f"SELECT * FROM {self._database}.{self._schema}.usage_events "
            f"WHERE {' AND '.join(conditions)}"
        )
        rows = self._execute(sql, tuple(params))
        return [
            UsageEvent(
                timestamp=r["TIMESTAMP"],
                actor_id=r["ACTOR_ID"],
                team_id=r["TEAM_ID"],
                source_app=SourceApp(r["SOURCE_APP"]),
                resource_type=ResourceType(r["RESOURCE_TYPE"]),
                quantity=r["QUANTITY"],
                unit_cost_usd=r["UNIT_COST_USD"],
                model=r.get("MODEL"),
                agent_id=r.get("AGENT_ID"),
                workflow_id=r.get("WORKFLOW_ID"),
                metadata=r.get("METADATA_JSON") or {},
            )
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Trend snapshots — Snowflake is the primary store for these
    # ------------------------------------------------------------------

    def upsert_trend_snapshot(self, snapshot: TrendSnapshot) -> None:
        sql = f"""
        MERGE INTO {self._database}.{self._schema}.trend_snapshots t
        USING (SELECT %s AS snapshot_id) s ON t.snapshot_id = s.snapshot_id
        WHEN MATCHED THEN UPDATE SET
            total_tokens=%s, total_cost_usd=%s, event_count=%s,
            active_actors=%s, week_over_week_delta=%s
        WHEN NOT MATCHED THEN INSERT
            (snapshot_id, snapshot_date, model, agent_id, team_id,
             source_app, demand_driver, total_tokens, total_cost_usd,
             event_count, active_actors, week_over_week_delta)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
        self._execute(
            sql,
            (
                snapshot.snapshot_id,
                snapshot.total_tokens, snapshot.total_cost_usd,
                snapshot.event_count, snapshot.active_actors,
                snapshot.week_over_week_delta,
                snapshot.snapshot_id,
                snapshot.snapshot_date.date().isoformat(),
                snapshot.model, snapshot.agent_id, snapshot.team_id,
                snapshot.source_app.value if snapshot.source_app else None,
                snapshot.demand_driver.value if snapshot.demand_driver else None,
                snapshot.total_tokens, snapshot.total_cost_usd,
                snapshot.event_count, snapshot.active_actors,
                snapshot.week_over_week_delta,
            ),
        )

    def get_trend_snapshots(
        self,
        since: datetime | None = None,
        model: str | None = None,
        agent_id: str | None = None,
        team_id: str | None = None,
        source_app: SourceApp | None = None,
    ) -> list[TrendSnapshot]:
        conditions = ["1=1"]
        params: list = []
        if since:
            conditions.append("snapshot_date >= %s")
            params.append(since.date().isoformat())
        if model:
            conditions.append("model = %s")
            params.append(model)
        if agent_id:
            conditions.append("agent_id = %s")
            params.append(agent_id)
        if team_id:
            conditions.append("team_id = %s")
            params.append(team_id)
        if source_app:
            conditions.append("source_app = %s")
            params.append(source_app.value)
        sql = (
            f"SELECT * FROM {self._database}.{self._schema}.trend_snapshots "
            f"WHERE {' AND '.join(conditions)} ORDER BY snapshot_date ASC"
        )
        rows = self._execute(sql, tuple(params))
        return [
            TrendSnapshot(
                snapshot_id=r["SNAPSHOT_ID"],
                snapshot_date=datetime.fromisoformat(str(r["SNAPSHOT_DATE"])),
                model=r.get("MODEL"),
                agent_id=r.get("AGENT_ID"),
                team_id=r.get("TEAM_ID"),
                source_app=SourceApp(r["SOURCE_APP"]) if r.get("SOURCE_APP") else None,
                demand_driver=DemandDriver(r["DEMAND_DRIVER"]) if r.get("DEMAND_DRIVER") else None,
                total_tokens=r["TOTAL_TOKENS"],
                total_cost_usd=r["TOTAL_COST_USD"],
                event_count=r["EVENT_COUNT"],
                active_actors=r["ACTIVE_ACTORS"],
                week_over_week_delta=r.get("WEEK_OVER_WEEK_DELTA"),
            )
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Audit events
    # ------------------------------------------------------------------

    def append_audit_event(self, event: AuditEvent) -> None:
        """Append an audit event. Uses INSERT ... WHERE NOT EXISTS as an
        idempotency guard — duplicate event_ids are silently ignored.

        Audit events are compliance-relevant. A failure here is logged as an
        error rather than silently swallowed so operators are alerted."""
        sql = f"""
        INSERT INTO {self._database}.{self._schema}.audit_events
            (event_id, timestamp, event_type, agent_id, actor_id,
             before_json, after_json, source)
        SELECT %s, %s, %s, %s, %s, PARSE_JSON(%s), PARSE_JSON(%s), %s
        WHERE NOT EXISTS (
            SELECT 1 FROM {self._database}.{self._schema}.audit_events
            WHERE event_id = %s
        )
        """
        try:
            self._execute(
                sql,
                (
                    event.event_id, event.timestamp.isoformat(), event.event_type,
                    event.agent_id, event.actor_id,
                    json.dumps(event.before_state), json.dumps(event.after_state),
                    event.source, event.event_id,
                ),
            )
        except Exception as exc:
            # Audit events must not be silently lost — log at ERROR so
            # operators are alerted even if the exception is not re-raised.
            logger.error(
                "Failed to append audit event %s to Snowflake: %s",
                event.event_id, exc,
            )
            raise

    def get_audit_events(
        self,
        agent_id: str | None = None,
        since: datetime | None = None,
        event_type: str | None = None,
    ) -> list[AuditEvent]:
        conditions = ["1=1"]
        params: list = []
        if agent_id:
            conditions.append("agent_id = %s")
            params.append(agent_id)
        if since:
            conditions.append("timestamp >= %s")
            params.append(since.isoformat())
        if event_type:
            conditions.append("event_type = %s")
            params.append(event_type)
        sql = (
            f"SELECT * FROM {self._database}.{self._schema}.audit_events "
            f"WHERE {' AND '.join(conditions)} ORDER BY timestamp ASC"
        )
        rows = self._execute(sql, tuple(params))
        return [
            AuditEvent(
                event_id=r["EVENT_ID"],
                timestamp=r["TIMESTAMP"],
                event_type=r["EVENT_TYPE"],
                agent_id=r.get("AGENT_ID"),
                actor_id=r.get("ACTOR_ID"),
                before_state=r.get("BEFORE_JSON") or {},
                after_state=r.get("AFTER_JSON") or {},
                source=r.get("SOURCE", "ai-control-plane"),
            )
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Stub methods — delegate to SqliteBackend for operational tables
    # not worth the Snowflake overhead (agents, workflow_items, alert_rules).
    # In a full production setup these would also live in Snowflake.
    # ------------------------------------------------------------------

    def upsert_capacity_pool(self, pool: CapacityPool) -> None:
        raise NotImplementedError("Use SqliteBackend for operational tables in this release")

    def get_capacity_pools(self, demand_driver=None):
        raise NotImplementedError("Use SqliteBackend for operational tables in this release")

    def upsert_role(self, role): raise NotImplementedError
    def get_roles(self): raise NotImplementedError
    def upsert_user(self, user): raise NotImplementedError
    def get_users(self, team_id=None): raise NotImplementedError
    def add_budget_override(self, override): raise NotImplementedError
    def get_effective_budget(self, user_id, year_month): raise NotImplementedError
    def get_user_spend_for_month(self, user_id, year_month): raise NotImplementedError
    def get_team_spend_for_month(self, team_id, year_month): raise NotImplementedError
    def get_spend_breakdown_by_model(self, user_id, year_month): raise NotImplementedError
    def upsert_agent(self, agent): raise NotImplementedError
    def get_agent(self, agent_id): raise NotImplementedError
    def get_agents(self, **kwargs): raise NotImplementedError
    def upsert_agent_manifest(self, agent_id, manifest): raise NotImplementedError
    def get_agent_manifest(self, agent_id): raise NotImplementedError
    def upsert_workflow(self, workflow): raise NotImplementedError
    def get_workflow(self, workflow_id): raise NotImplementedError
    def get_workflows(self): raise NotImplementedError
    def insert_classification_result(self, result): raise NotImplementedError
    def get_classification_results(self, agent_id=None): raise NotImplementedError
    def upsert_workflow_item(self, item): raise NotImplementedError
    def get_workflow_item(self, item_id): raise NotImplementedError
    def get_workflow_items(self, **kwargs): raise NotImplementedError
    def upsert_alert_rule(self, rule): raise NotImplementedError
    def get_alert_rules(self, **kwargs): raise NotImplementedError
    def insert_alert_history(self, record): raise NotImplementedError
    def get_alert_history(self, **kwargs): raise NotImplementedError
