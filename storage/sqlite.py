"""
SQLite storage backend — zero-setup, stdlib-only.

Use for local development, the demo runner, and tests. Swapping to Snowflake
for production is a configuration change: replace SqliteBackend with
SnowflakeBackend anywhere a StorageBackend is constructed; callers do not
change.

IMPORTANT: This backend only ever writes synthetic or test data in this public
repo. If you point a real connector at it, the resulting database file
contains real usage/identity data and should be treated accordingly —
access-controlled, kept private, and in scope for your org's data-protection
review. See README for the public/private split policy.

Schema overview:
    usage_events         — normalized UsageEvent rows
    capacity_pools       — pre-purchased model capacity, with lifecycle status
    roles                — budget tier definitions
    users                — identities mapped to a role + team
    budget_overrides     — one-off monthly budget per user
    agents               — registered/discovered agent registry
    agent_manifests      — JSON manifest blob per agent
    workflows            — multi-agent pipeline definitions
    classification_results — historical tier classification outputs
    workflow_items       — classification request and holistic review tracking
    alert_rules          — registered webhook destinations per team
    alert_history        — dispatched webhook records
    trend_snapshots      — daily aggregation snapshots
    audit_events         — local copy of audit trail (Blob is authoritative)
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from core.models import (
    Agent,
    AgentManifest,
    AgentStatus,
    AgentTier,
    AlertEventType,
    AlertHistory,
    AlertRule,
    AuditEvent,
    BudgetOverride,
    CapacityPool,
    ClassificationResult,
    ControlFlowType,
    CostConstruct,
    DemandDriver,
    DiscoverySource,
    FunctionalRole,
    ModelStatus,
    RegulatoryFlag,
    ResourceType,
    Role,
    Severity,
    SourceApp,
    TrendSnapshot,
    UsageEvent,
    User,
    WebhookEnvelope,
    Workflow,
    WorkflowItem,
    WorkflowItemStatus,
    WorkflowItemType,
)

DEFAULT_ROLE_NAME = "non_tech"

SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_events (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp        TEXT NOT NULL,
    actor_id         TEXT NOT NULL,
    team_id          TEXT NOT NULL,
    source_app       TEXT NOT NULL,
    resource_type    TEXT NOT NULL,
    quantity         REAL NOT NULL,
    unit_cost_usd    REAL NOT NULL,
    model            TEXT,
    agent_id         TEXT,
    workflow_id      TEXT,
    metadata_json    TEXT
);
CREATE INDEX IF NOT EXISTS idx_usage_events_actor     ON usage_events(actor_id);
CREATE INDEX IF NOT EXISTS idx_usage_events_model     ON usage_events(model);
CREATE INDEX IF NOT EXISTS idx_usage_events_timestamp ON usage_events(timestamp);
CREATE INDEX IF NOT EXISTS idx_usage_events_agent     ON usage_events(agent_id);
CREATE INDEX IF NOT EXISTS idx_usage_events_team      ON usage_events(team_id);

CREATE TABLE IF NOT EXISTS capacity_pools (
    pool_id                              TEXT PRIMARY KEY,
    model                                TEXT NOT NULL,
    model_family                         TEXT NOT NULL,
    version_number                       INTEGER NOT NULL,
    region                               TEXT NOT NULL,
    ptu_quantity                         INTEGER NOT NULL,
    cost_usd                             REAL NOT NULL,
    start_date                           TEXT NOT NULL,
    end_date                             TEXT NOT NULL,
    demand_driver                        TEXT NOT NULL,
    status                               TEXT NOT NULL,
    throughput_capacity_tokens_per_hour  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS roles (
    role_id            TEXT PRIMARY KEY,
    role_name          TEXT NOT NULL UNIQUE,
    monthly_budget_usd REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    user_id      TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    team_id      TEXT NOT NULL,
    role_id      TEXT REFERENCES roles(role_id)
);
CREATE INDEX IF NOT EXISTS idx_users_team ON users(team_id);

CREATE TABLE IF NOT EXISTS budget_overrides (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id             TEXT NOT NULL REFERENCES users(user_id),
    year_month          TEXT NOT NULL,
    override_budget_usd REAL NOT NULL,
    reason              TEXT,
    UNIQUE(user_id, year_month)
);

CREATE TABLE IF NOT EXISTS agents (
    agent_id               TEXT PRIMARY KEY,
    name                   TEXT NOT NULL,
    owner_id               TEXT NOT NULL,
    team_id                TEXT NOT NULL,
    source_platform        TEXT NOT NULL,
    status                 TEXT NOT NULL,
    discovery_source       TEXT NOT NULL,
    discovered_at          TEXT NOT NULL,
    tier                   TEXT NOT NULL DEFAULT 'unclassified',
    functional_role        TEXT NOT NULL DEFAULT 'unknown',
    control_flow_type      TEXT,
    cost_construct         TEXT,
    regulatory_flags_json  TEXT NOT NULL DEFAULT '[]',
    escalation_webhook_url TEXT,
    last_seen_at           TEXT,
    last_classified_at     TEXT,
    monthly_token_cap      REAL
);
CREATE INDEX IF NOT EXISTS idx_agents_team   ON agents(team_id);
CREATE INDEX IF NOT EXISTS idx_agents_tier   ON agents(tier);
CREATE INDEX IF NOT EXISTS idx_agents_status ON agents(status);

CREATE TABLE IF NOT EXISTS agent_manifests (
    agent_id      TEXT PRIMARY KEY REFERENCES agents(agent_id),
    manifest_json TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workflows (
    workflow_id              TEXT PRIMARY KEY,
    name                     TEXT NOT NULL,
    orchestrator_agent_id    TEXT NOT NULL,
    participating_agent_ids  TEXT NOT NULL DEFAULT '[]',
    description              TEXT NOT NULL DEFAULT '',
    created_at               TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS classification_results (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id         TEXT NOT NULL,
    tier             TEXT NOT NULL,
    functional_role  TEXT NOT NULL,
    control_flow_type TEXT NOT NULL,
    cost_construct   TEXT,
    regulatory_flags_json TEXT NOT NULL DEFAULT '[]',
    confidence       REAL NOT NULL,
    classified_at    TEXT NOT NULL,
    classified_by    TEXT NOT NULL DEFAULT 'auto',
    notes            TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_classification_agent ON classification_results(agent_id);

CREATE TABLE IF NOT EXISTS workflow_items (
    item_id                    TEXT PRIMARY KEY,
    agent_id                   TEXT NOT NULL,
    item_type                  TEXT NOT NULL,
    status                     TEXT NOT NULL,
    raised_at                  TEXT NOT NULL,
    due_at                     TEXT NOT NULL,
    resolved_at                TEXT,
    reminder_count             INTEGER NOT NULL DEFAULT 0,
    escalation_target_webhook  TEXT,
    missing_fields_json        TEXT NOT NULL DEFAULT '[]',
    notes                      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_workflow_items_agent  ON workflow_items(agent_id);
CREATE INDEX IF NOT EXISTS idx_workflow_items_status ON workflow_items(status);

CREATE TABLE IF NOT EXISTS alert_rules (
    rule_id      TEXT PRIMARY KEY,
    webhook_url  TEXT NOT NULL,
    event_types  TEXT NOT NULL DEFAULT '[]',
    min_severity TEXT NOT NULL DEFAULT 'low',
    team_id      TEXT,
    active       INTEGER NOT NULL DEFAULT 1,
    description  TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_alert_rules_team ON alert_rules(team_id);

CREATE TABLE IF NOT EXISTS alert_history (
    history_id    TEXT PRIMARY KEY,
    rule_id       TEXT NOT NULL,
    event_id      TEXT NOT NULL,
    event_type    TEXT NOT NULL,
    dispatched_at TEXT NOT NULL,
    http_status   INTEGER,
    success       INTEGER NOT NULL,
    error_message TEXT
);
CREATE INDEX IF NOT EXISTS idx_alert_history_rule ON alert_history(rule_id);

CREATE TABLE IF NOT EXISTS trend_snapshots (
    snapshot_id         TEXT PRIMARY KEY,
    snapshot_date       TEXT NOT NULL,
    model               TEXT,
    agent_id            TEXT,
    team_id             TEXT,
    source_app          TEXT,
    demand_driver       TEXT,
    total_tokens        REAL NOT NULL DEFAULT 0,
    total_cost_usd      REAL NOT NULL DEFAULT 0,
    event_count         INTEGER NOT NULL DEFAULT 0,
    active_actors       INTEGER NOT NULL DEFAULT 0,
    week_over_week_delta REAL
);
CREATE INDEX IF NOT EXISTS idx_trend_snapshots_date ON trend_snapshots(snapshot_date);

CREATE TABLE IF NOT EXISTS audit_events (
    event_id     TEXT PRIMARY KEY,
    timestamp    TEXT NOT NULL,
    event_type   TEXT NOT NULL,
    agent_id     TEXT,
    actor_id     TEXT,
    before_json  TEXT NOT NULL DEFAULT '{}',
    after_json   TEXT NOT NULL DEFAULT '{}',
    source       TEXT NOT NULL DEFAULT 'ai-control-plane'
);
CREATE INDEX IF NOT EXISTS idx_audit_events_agent ON audit_events(agent_id);
CREATE INDEX IF NOT EXISTS idx_audit_events_ts    ON audit_events(timestamp);
"""


class SqliteBackend:
    def __init__(self, db_path: str | Path = "control_plane.db"):
        self.db_path = str(db_path)
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Usage events
    # ------------------------------------------------------------------

    def insert_usage_events(self, events: list[UsageEvent]) -> int:
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO usage_events
                    (timestamp, actor_id, team_id, source_app, resource_type,
                     quantity, unit_cost_usd, model, agent_id, workflow_id, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        e.timestamp.isoformat(),
                        e.actor_id,
                        e.team_id,
                        e.source_app.value,
                        e.resource_type.value,
                        e.quantity,
                        e.unit_cost_usd,
                        e.model,
                        e.agent_id,
                        e.workflow_id,
                        json.dumps(e.metadata) if e.metadata else None,
                    )
                    for e in events
                ],
            )
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
        query = "SELECT * FROM usage_events WHERE 1=1"
        params: list = []
        if since is not None:
            query += " AND timestamp >= ?"
            params.append(since.isoformat())
        if model is not None:
            query += " AND model = ?"
            params.append(model)
        if actor_id is not None:
            query += " AND actor_id = ?"
            params.append(actor_id)
        if agent_id is not None:
            query += " AND agent_id = ?"
            params.append(agent_id)
        if team_id is not None:
            query += " AND team_id = ?"
            params.append(team_id)
        if source_app is not None:
            query += " AND source_app = ?"
            params.append(source_app.value)

        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()

        return [
            UsageEvent(
                timestamp=datetime.fromisoformat(r["timestamp"]),
                actor_id=r["actor_id"],
                team_id=r["team_id"],
                source_app=SourceApp(r["source_app"]),
                resource_type=ResourceType(r["resource_type"]),
                quantity=r["quantity"],
                unit_cost_usd=r["unit_cost_usd"],
                model=r["model"],
                agent_id=r["agent_id"],
                workflow_id=r["workflow_id"],
                metadata=json.loads(r["metadata_json"]) if r["metadata_json"] else {},
            )
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Capacity pools
    # ------------------------------------------------------------------

    def upsert_capacity_pool(self, pool: CapacityPool) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO capacity_pools
                    (pool_id, model, model_family, version_number, region,
                     ptu_quantity, cost_usd, start_date, end_date,
                     demand_driver, status, throughput_capacity_tokens_per_hour)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(pool_id) DO UPDATE SET
                    model=excluded.model,
                    model_family=excluded.model_family,
                    version_number=excluded.version_number,
                    region=excluded.region,
                    ptu_quantity=excluded.ptu_quantity,
                    cost_usd=excluded.cost_usd,
                    start_date=excluded.start_date,
                    end_date=excluded.end_date,
                    demand_driver=excluded.demand_driver,
                    status=excluded.status,
                    throughput_capacity_tokens_per_hour=excluded.throughput_capacity_tokens_per_hour
                """,
                (
                    pool.pool_id, pool.model, pool.model_family, pool.version_number,
                    pool.region, pool.ptu_quantity, pool.cost_usd,
                    pool.start_date.isoformat(), pool.end_date.isoformat(),
                    pool.demand_driver.value, pool.status.value,
                    pool.throughput_capacity_tokens_per_hour,
                ),
            )

    def get_capacity_pools(
        self, demand_driver: DemandDriver | None = None
    ) -> list[CapacityPool]:
        query = "SELECT * FROM capacity_pools WHERE 1=1"
        params: list = []
        if demand_driver is not None:
            query += " AND demand_driver = ?"
            params.append(demand_driver.value)

        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()

        return [
            CapacityPool(
                pool_id=r["pool_id"],
                model=r["model"],
                model_family=r["model_family"],
                version_number=r["version_number"],
                region=r["region"],
                ptu_quantity=r["ptu_quantity"],
                cost_usd=r["cost_usd"],
                start_date=datetime.fromisoformat(r["start_date"]),
                end_date=datetime.fromisoformat(r["end_date"]),
                demand_driver=DemandDriver(r["demand_driver"]),
                status=ModelStatus(r["status"]),
                throughput_capacity_tokens_per_hour=r["throughput_capacity_tokens_per_hour"],
            )
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Roles / users / budget overrides
    # ------------------------------------------------------------------

    def upsert_role(self, role: Role) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO roles (role_id, role_name, monthly_budget_usd)
                VALUES (?, ?, ?)
                ON CONFLICT(role_id) DO UPDATE SET
                    role_name=excluded.role_name,
                    monthly_budget_usd=excluded.monthly_budget_usd
                """,
                (role.role_id, role.role_name, role.monthly_budget_usd),
            )

    def get_roles(self) -> list[Role]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM roles").fetchall()
        return [
            Role(role_id=r["role_id"], role_name=r["role_name"],
                 monthly_budget_usd=r["monthly_budget_usd"])
            for r in rows
        ]

    def upsert_user(self, user: User) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO users (user_id, display_name, team_id, role_id)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    display_name=excluded.display_name,
                    team_id=excluded.team_id,
                    role_id=excluded.role_id
                """,
                (user.user_id, user.display_name, user.team_id, user.role_id),
            )

    def get_users(self, team_id: str | None = None) -> list[User]:
        query = "SELECT * FROM users WHERE 1=1"
        params: list = []
        if team_id is not None:
            query += " AND team_id = ?"
            params.append(team_id)
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [
            User(user_id=r["user_id"], display_name=r["display_name"],
                 team_id=r["team_id"], role_id=r["role_id"])
            for r in rows
        ]

    def add_budget_override(self, override: BudgetOverride) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO budget_overrides
                    (user_id, year_month, override_budget_usd, reason)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id, year_month) DO UPDATE SET
                    override_budget_usd=excluded.override_budget_usd,
                    reason=excluded.reason
                """,
                (override.user_id, override.year_month,
                 override.override_budget_usd, override.reason),
            )

    def get_effective_budget(self, user_id: str, year_month: str) -> tuple[float, str]:
        with self._connect() as conn:
            override = conn.execute(
                "SELECT override_budget_usd FROM budget_overrides "
                "WHERE user_id = ? AND year_month = ?",
                (user_id, year_month),
            ).fetchone()
            if override:
                return override["override_budget_usd"], "override"

            user = conn.execute(
                "SELECT role_id FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
            if user and user["role_id"]:
                role = conn.execute(
                    "SELECT monthly_budget_usd FROM roles WHERE role_id = ?",
                    (user["role_id"],),
                ).fetchone()
                if role:
                    return role["monthly_budget_usd"], "role"

            default = conn.execute(
                "SELECT monthly_budget_usd FROM roles WHERE role_name = ?",
                (DEFAULT_ROLE_NAME,),
            ).fetchone()
            if default:
                return default["monthly_budget_usd"], "default_role"

        return 0.0, "unconfigured"

    def get_user_spend_for_month(self, user_id: str, year_month: str) -> float:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT SUM(quantity * unit_cost_usd) as total FROM usage_events "
                "WHERE actor_id = ? AND substr(timestamp, 1, 7) = ?",
                (user_id, year_month),
            ).fetchone()
        return row["total"] or 0.0

    def get_team_spend_for_month(self, team_id: str, year_month: str) -> float:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT SUM(quantity * unit_cost_usd) as total FROM usage_events "
                "WHERE team_id = ? AND substr(timestamp, 1, 7) = ?",
                (team_id, year_month),
            ).fetchone()
        return row["total"] or 0.0

    def get_spend_breakdown_by_model(
        self, user_id: str, year_month: str
    ) -> dict[str, float]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT model, SUM(quantity * unit_cost_usd) as total FROM usage_events "
                "WHERE actor_id = ? AND substr(timestamp, 1, 7) = ? GROUP BY model",
                (user_id, year_month),
            ).fetchall()
        return {r["model"] or "unknown": r["total"] for r in rows}

    # ------------------------------------------------------------------
    # Agents
    # ------------------------------------------------------------------

    def upsert_agent(self, agent: Agent) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO agents (
                    agent_id, name, owner_id, team_id, source_platform, status,
                    discovery_source, discovered_at, tier, functional_role,
                    control_flow_type, cost_construct, regulatory_flags_json,
                    escalation_webhook_url, last_seen_at, last_classified_at,
                    monthly_token_cap
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(agent_id) DO UPDATE SET
                    name=excluded.name,
                    owner_id=excluded.owner_id,
                    team_id=excluded.team_id,
                    source_platform=excluded.source_platform,
                    status=excluded.status,
                    discovery_source=excluded.discovery_source,
                    tier=excluded.tier,
                    functional_role=excluded.functional_role,
                    control_flow_type=excluded.control_flow_type,
                    cost_construct=excluded.cost_construct,
                    regulatory_flags_json=excluded.regulatory_flags_json,
                    escalation_webhook_url=excluded.escalation_webhook_url,
                    last_seen_at=excluded.last_seen_at,
                    last_classified_at=excluded.last_classified_at,
                    monthly_token_cap=excluded.monthly_token_cap
                """,
                (
                    agent.agent_id, agent.name, agent.owner_id, agent.team_id,
                    agent.source_platform.value, agent.status.value,
                    agent.discovery_source.value, agent.discovered_at.isoformat(),
                    agent.tier.value, agent.functional_role.value,
                    agent.control_flow_type.value if agent.control_flow_type else None,
                    agent.cost_construct.value if agent.cost_construct else None,
                    json.dumps([f.value for f in agent.regulatory_flags]),
                    agent.escalation_webhook_url,
                    agent.last_seen_at.isoformat() if agent.last_seen_at else None,
                    agent.last_classified_at.isoformat() if agent.last_classified_at else None,
                    agent.monthly_token_cap,
                ),
            )

    def _row_to_agent(self, r: sqlite3.Row) -> Agent:
        return Agent(
            agent_id=r["agent_id"],
            name=r["name"],
            owner_id=r["owner_id"],
            team_id=r["team_id"],
            source_platform=SourceApp(r["source_platform"]),
            status=AgentStatus(r["status"]),
            discovery_source=DiscoverySource(r["discovery_source"]),
            discovered_at=datetime.fromisoformat(r["discovered_at"]),
            tier=AgentTier(r["tier"]),
            functional_role=FunctionalRole(r["functional_role"]),
            control_flow_type=ControlFlowType(r["control_flow_type"]) if r["control_flow_type"] else None,
            cost_construct=CostConstruct(r["cost_construct"]) if r["cost_construct"] else None,
            regulatory_flags=[RegulatoryFlag(f) for f in json.loads(r["regulatory_flags_json"] or "[]")],
            escalation_webhook_url=r["escalation_webhook_url"],
            last_seen_at=datetime.fromisoformat(r["last_seen_at"]) if r["last_seen_at"] else None,
            last_classified_at=datetime.fromisoformat(r["last_classified_at"]) if r["last_classified_at"] else None,
            monthly_token_cap=r["monthly_token_cap"],
        )

    def get_agent(self, agent_id: str) -> Agent | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM agents WHERE agent_id = ?", (agent_id,)
            ).fetchone()
        return self._row_to_agent(row) if row else None

    def get_agents(
        self,
        team_id: str | None = None,
        tier: AgentTier | None = None,
        status: str | None = None,
        source_platform: SourceApp | None = None,
    ) -> list[Agent]:
        query = "SELECT * FROM agents WHERE 1=1"
        params: list = []
        if team_id is not None:
            query += " AND team_id = ?"
            params.append(team_id)
        if tier is not None:
            query += " AND tier = ?"
            params.append(tier.value)
        if status is not None:
            query += " AND status = ?"
            params.append(status)
        if source_platform is not None:
            query += " AND source_platform = ?"
            params.append(source_platform.value)
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._row_to_agent(r) for r in rows]

    def upsert_agent_manifest(self, agent_id: str, manifest: AgentManifest) -> None:
        manifest_dict = {
            "tool_list": manifest.tool_list,
            "mcp_servers": manifest.mcp_servers,
            "data_sources": manifest.data_sources,
            "sub_agent_ids": manifest.sub_agent_ids,
            "external_targets": manifest.external_targets,
            "execution_rights": manifest.execution_rights,
            "credentials_held": manifest.credentials_held,
            "can_modify_repos_or_pipelines": manifest.can_modify_repos_or_pipelines,
            "can_modify_other_agents": manifest.can_modify_other_agents,
            "writes_to_system_of_record": manifest.writes_to_system_of_record,
            "reaches_external_party": manifest.reaches_external_party,
            "distributes_output_autonomously": manifest.distributes_output_autonomously,
            "grounding_includes_confidential_of_others": manifest.grounding_includes_confidential_of_others,
            "data_scope": manifest.data_scope,
            "last_updated": manifest.last_updated.isoformat() if manifest.last_updated else None,
        }
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO agent_manifests (agent_id, manifest_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(agent_id) DO UPDATE SET
                    manifest_json=excluded.manifest_json,
                    updated_at=excluded.updated_at
                """,
                (agent_id, json.dumps(manifest_dict), datetime.now().isoformat()),
            )

    def get_agent_manifest(self, agent_id: str) -> AgentManifest | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT manifest_json FROM agent_manifests WHERE agent_id = ?",
                (agent_id,),
            ).fetchone()
        if not row:
            return None
        d = json.loads(row["manifest_json"])
        return AgentManifest(
            tool_list=d.get("tool_list", []),
            mcp_servers=d.get("mcp_servers", []),
            data_sources=d.get("data_sources", []),
            sub_agent_ids=d.get("sub_agent_ids", []),
            external_targets=d.get("external_targets", []),
            execution_rights=d.get("execution_rights"),
            credentials_held=d.get("credentials_held"),
            can_modify_repos_or_pipelines=d.get("can_modify_repos_or_pipelines"),
            can_modify_other_agents=d.get("can_modify_other_agents"),
            writes_to_system_of_record=d.get("writes_to_system_of_record"),
            reaches_external_party=d.get("reaches_external_party"),
            distributes_output_autonomously=d.get("distributes_output_autonomously"),
            grounding_includes_confidential_of_others=d.get("grounding_includes_confidential_of_others"),
            data_scope=d.get("data_scope"),
            last_updated=datetime.fromisoformat(d["last_updated"]) if d.get("last_updated") else None,
        )

    # ------------------------------------------------------------------
    # Workflows
    # ------------------------------------------------------------------

    def upsert_workflow(self, workflow: Workflow) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO workflows
                    (workflow_id, name, orchestrator_agent_id, participating_agent_ids,
                     description, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(workflow_id) DO UPDATE SET
                    name=excluded.name,
                    orchestrator_agent_id=excluded.orchestrator_agent_id,
                    participating_agent_ids=excluded.participating_agent_ids,
                    description=excluded.description
                """,
                (
                    workflow.workflow_id, workflow.name, workflow.orchestrator_agent_id,
                    json.dumps(workflow.participating_agent_ids),
                    workflow.description, workflow.created_at.isoformat(),
                ),
            )

    def get_workflow(self, workflow_id: str) -> Workflow | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM workflows WHERE workflow_id = ?", (workflow_id,)
            ).fetchone()
        if not row:
            return None
        return Workflow(
            workflow_id=row["workflow_id"],
            name=row["name"],
            orchestrator_agent_id=row["orchestrator_agent_id"],
            participating_agent_ids=json.loads(row["participating_agent_ids"]),
            description=row["description"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    def get_workflows(self) -> list[Workflow]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM workflows").fetchall()
        return [
            Workflow(
                workflow_id=r["workflow_id"],
                name=r["name"],
                orchestrator_agent_id=r["orchestrator_agent_id"],
                participating_agent_ids=json.loads(r["participating_agent_ids"]),
                description=r["description"],
                created_at=datetime.fromisoformat(r["created_at"]),
            )
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Classification results
    # ------------------------------------------------------------------

    def insert_classification_result(self, result: ClassificationResult) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO classification_results
                    (agent_id, tier, functional_role, control_flow_type, cost_construct,
                     regulatory_flags_json, confidence, classified_at, classified_by, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result.agent_id, result.tier.value, result.functional_role.value,
                    result.control_flow_type.value,
                    result.cost_construct.value if result.cost_construct else None,
                    json.dumps([f.value for f in result.regulatory_flags]),
                    result.confidence, result.classified_at.isoformat(),
                    result.classified_by, result.notes,
                ),
            )

    def get_classification_results(
        self, agent_id: str | None = None
    ) -> list[ClassificationResult]:
        query = "SELECT * FROM classification_results WHERE 1=1"
        params: list = []
        if agent_id is not None:
            query += " AND agent_id = ?"
            params.append(agent_id)
        query += " ORDER BY classified_at DESC"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [
            ClassificationResult(
                agent_id=r["agent_id"],
                tier=AgentTier(r["tier"]),
                functional_role=FunctionalRole(r["functional_role"]),
                control_flow_type=ControlFlowType(r["control_flow_type"]),
                cost_construct=CostConstruct(r["cost_construct"]) if r["cost_construct"] else None,
                regulatory_flags=[RegulatoryFlag(f) for f in json.loads(r["regulatory_flags_json"] or "[]")],
                confidence=r["confidence"],
                classified_at=datetime.fromisoformat(r["classified_at"]),
                classified_by=r["classified_by"],
                notes=r["notes"],
            )
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Workflow items
    # ------------------------------------------------------------------

    def upsert_workflow_item(self, item: WorkflowItem) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO workflow_items
                    (item_id, agent_id, item_type, status, raised_at, due_at,
                     resolved_at, reminder_count, escalation_target_webhook,
                     missing_fields_json, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(item_id) DO UPDATE SET
                    status=excluded.status,
                    resolved_at=excluded.resolved_at,
                    reminder_count=excluded.reminder_count,
                    escalation_target_webhook=excluded.escalation_target_webhook,
                    missing_fields_json=excluded.missing_fields_json,
                    notes=excluded.notes
                """,
                (
                    item.item_id, item.agent_id, item.item_type.value, item.status.value,
                    item.raised_at.isoformat(), item.due_at.isoformat(),
                    item.resolved_at.isoformat() if item.resolved_at else None,
                    item.reminder_count, item.escalation_target_webhook,
                    json.dumps(item.missing_fields), item.notes,
                ),
            )

    def get_workflow_item(self, item_id: str) -> WorkflowItem | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM workflow_items WHERE item_id = ?", (item_id,)
            ).fetchone()
        return self._row_to_workflow_item(row) if row else None

    def _row_to_workflow_item(self, r: sqlite3.Row) -> WorkflowItem:
        return WorkflowItem(
            item_id=r["item_id"],
            agent_id=r["agent_id"],
            item_type=WorkflowItemType(r["item_type"]),
            status=WorkflowItemStatus(r["status"]),
            raised_at=datetime.fromisoformat(r["raised_at"]),
            due_at=datetime.fromisoformat(r["due_at"]),
            resolved_at=datetime.fromisoformat(r["resolved_at"]) if r["resolved_at"] else None,
            reminder_count=r["reminder_count"],
            escalation_target_webhook=r["escalation_target_webhook"],
            missing_fields=json.loads(r["missing_fields_json"] or "[]"),
            notes=r["notes"],
        )

    def get_workflow_items(
        self,
        agent_id: str | None = None,
        status: WorkflowItemStatus | None = None,
        item_type: WorkflowItemType | None = None,
    ) -> list[WorkflowItem]:
        query = "SELECT * FROM workflow_items WHERE 1=1"
        params: list = []
        if agent_id is not None:
            query += " AND agent_id = ?"
            params.append(agent_id)
        if status is not None:
            query += " AND status = ?"
            params.append(status.value)
        if item_type is not None:
            query += " AND item_type = ?"
            params.append(item_type.value)
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._row_to_workflow_item(r) for r in rows]

    # ------------------------------------------------------------------
    # Alert rules and history
    # ------------------------------------------------------------------

    def upsert_alert_rule(self, rule: AlertRule) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO alert_rules
                    (rule_id, webhook_url, event_types, min_severity, team_id, active, description)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(rule_id) DO UPDATE SET
                    webhook_url=excluded.webhook_url,
                    event_types=excluded.event_types,
                    min_severity=excluded.min_severity,
                    team_id=excluded.team_id,
                    active=excluded.active,
                    description=excluded.description
                """,
                (
                    rule.rule_id, rule.webhook_url,
                    json.dumps([e.value for e in rule.event_types]),
                    rule.min_severity.value, rule.team_id, int(rule.active), rule.description,
                ),
            )

    def get_alert_rules(
        self,
        team_id: str | None = None,
        event_type: AlertEventType | None = None,
        active_only: bool = True,
    ) -> list[AlertRule]:
        query = "SELECT * FROM alert_rules WHERE 1=1"
        params: list = []
        if active_only:
            query += " AND active = 1"
        if team_id is not None:
            # Include team-specific rules and the platform-wide fallback (team_id IS NULL)
            query += " AND (team_id = ? OR team_id IS NULL)"
            params.append(team_id)
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        rules = [
            AlertRule(
                rule_id=r["rule_id"],
                webhook_url=r["webhook_url"],
                event_types=[AlertEventType(e) for e in json.loads(r["event_types"] or "[]")],
                min_severity=Severity(r["min_severity"]),
                team_id=r["team_id"],
                active=bool(r["active"]),
                description=r["description"],
            )
            for r in rows
        ]
        # Filter by event_type after loading (simpler than JSON query)
        if event_type is not None:
            rules = [rule for rule in rules if event_type in rule.event_types]
        return rules

    def insert_alert_history(self, record: AlertHistory) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO alert_history
                    (history_id, rule_id, event_id, event_type, dispatched_at,
                     http_status, success, error_message)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.history_id, record.rule_id, record.event_id,
                    record.event_type.value, record.dispatched_at.isoformat(),
                    record.http_status, int(record.success), record.error_message,
                ),
            )

    def get_alert_history(
        self,
        rule_id: str | None = None,
        since: datetime | None = None,
    ) -> list[AlertHistory]:
        query = "SELECT * FROM alert_history WHERE 1=1"
        params: list = []
        if rule_id is not None:
            query += " AND rule_id = ?"
            params.append(rule_id)
        if since is not None:
            query += " AND dispatched_at >= ?"
            params.append(since.isoformat())
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [
            AlertHistory(
                history_id=r["history_id"],
                rule_id=r["rule_id"],
                event_id=r["event_id"],
                event_type=AlertEventType(r["event_type"]),
                dispatched_at=datetime.fromisoformat(r["dispatched_at"]),
                http_status=r["http_status"],
                success=bool(r["success"]),
                error_message=r["error_message"],
            )
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Trend snapshots
    # ------------------------------------------------------------------

    def upsert_trend_snapshot(self, snapshot: TrendSnapshot) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO trend_snapshots
                    (snapshot_id, snapshot_date, model, agent_id, team_id,
                     source_app, demand_driver, total_tokens, total_cost_usd,
                     event_count, active_actors, week_over_week_delta)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(snapshot_id) DO UPDATE SET
                    total_tokens=excluded.total_tokens,
                    total_cost_usd=excluded.total_cost_usd,
                    event_count=excluded.event_count,
                    active_actors=excluded.active_actors,
                    week_over_week_delta=excluded.week_over_week_delta
                """,
                (
                    snapshot.snapshot_id, snapshot.snapshot_date.isoformat(),
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
        query = "SELECT * FROM trend_snapshots WHERE 1=1"
        params: list = []
        if since is not None:
            query += " AND snapshot_date >= ?"
            params.append(since.isoformat())
        if model is not None:
            query += " AND model = ?"
            params.append(model)
        if agent_id is not None:
            query += " AND agent_id = ?"
            params.append(agent_id)
        if team_id is not None:
            query += " AND team_id = ?"
            params.append(team_id)
        if source_app is not None:
            query += " AND source_app = ?"
            params.append(source_app.value)
        query += " ORDER BY snapshot_date ASC"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [
            TrendSnapshot(
                snapshot_id=r["snapshot_id"],
                snapshot_date=datetime.fromisoformat(r["snapshot_date"]),
                model=r["model"],
                agent_id=r["agent_id"],
                team_id=r["team_id"],
                source_app=SourceApp(r["source_app"]) if r["source_app"] else None,
                demand_driver=DemandDriver(r["demand_driver"]) if r["demand_driver"] else None,
                total_tokens=r["total_tokens"],
                total_cost_usd=r["total_cost_usd"],
                event_count=r["event_count"],
                active_actors=r["active_actors"],
                week_over_week_delta=r["week_over_week_delta"],
            )
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Audit events (local queryable copy — Blob is authoritative)
    # ------------------------------------------------------------------

    def append_audit_event(self, event: AuditEvent) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO audit_events
                    (event_id, timestamp, event_type, agent_id, actor_id,
                     before_json, after_json, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id, event.timestamp.isoformat(), event.event_type,
                    event.agent_id, event.actor_id,
                    json.dumps(event.before_state), json.dumps(event.after_state),
                    event.source,
                ),
            )

    def get_audit_events(
        self,
        agent_id: str | None = None,
        since: datetime | None = None,
        event_type: str | None = None,
    ) -> list[AuditEvent]:
        query = "SELECT * FROM audit_events WHERE 1=1"
        params: list = []
        if agent_id is not None:
            query += " AND agent_id = ?"
            params.append(agent_id)
        if since is not None:
            query += " AND timestamp >= ?"
            params.append(since.isoformat())
        if event_type is not None:
            query += " AND event_type = ?"
            params.append(event_type)
        query += " ORDER BY timestamp ASC"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [
            AuditEvent(
                event_id=r["event_id"],
                timestamp=datetime.fromisoformat(r["timestamp"]),
                event_type=r["event_type"],
                agent_id=r["agent_id"],
                actor_id=r["actor_id"],
                before_state=json.loads(r["before_json"]),
                after_state=json.loads(r["after_json"]),
                source=r["source"],
            )
            for r in rows
        ]
