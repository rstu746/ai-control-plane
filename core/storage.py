"""
SQLite storage layer for the AI Control Plane.

SQLite is deliberately the storage choice for this phase: zero setup (no
server, single file), stdlib-only (sqlite3 ships with Python), and it
matches the "clone and run" bar the rest of this repo holds itself to.
Swapping to Postgres later is a connection-string change if callers keep
going through this module rather than writing raw SQL elsewhere — that's
the whole point of the module boundary.

IMPORTANT: this repo only ever writes synthetic data through this layer.
If you point a real connector at this storage layer, the resulting
database file contains real usage/identity data and should be treated
accordingly (kept private, access-controlled, in scope for your org's
data-protection review) — see README "Use this on your own org's data".

Schema overview:
    usage_events        — normalized UsageEvent rows
    capacity_pools       — pre-purchased model capacity, with lifecycle status
    roles                — budget tier definitions (tech, non_tech, ...)
    users                — synthetic AD-shaped identities, mapped to a role + team
    budget_overrides     — one-off monthly budget for a specific user
                             (e.g. mid-month role change, approved exception)

Budget resolution for a user in a given month:
    1. budget_overrides row for (user_id, year_month), if one exists
    2. else the monthly_budget_usd of the user's current role
    3. else the monthly_budget_usd of the default role (see DEFAULT_ROLE_NAME)
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from core.models import (
    BudgetOverride,
    CapacityPool,
    DemandDriver,
    ModelStatus,
    ResourceType,
    Role,
    SourceApp,
    UsageEvent,
    User,
)

DEFAULT_ROLE_NAME = "non_tech"

SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp        TEXT NOT NULL,
    actor_id         TEXT NOT NULL,
    team_id          TEXT NOT NULL,
    source_app       TEXT NOT NULL,
    resource_type    TEXT NOT NULL,
    quantity         REAL NOT NULL,
    unit_cost_usd    REAL NOT NULL,
    model            TEXT,
    metadata_json     TEXT
);
CREATE INDEX IF NOT EXISTS idx_usage_events_actor ON usage_events(actor_id);
CREATE INDEX IF NOT EXISTS idx_usage_events_model ON usage_events(model);
CREATE INDEX IF NOT EXISTS idx_usage_events_timestamp ON usage_events(timestamp);

CREATE TABLE IF NOT EXISTS capacity_pools (
    pool_id                                TEXT PRIMARY KEY,
    model                                   TEXT NOT NULL,
    model_family                            TEXT NOT NULL,
    version_number                          INTEGER NOT NULL,
    region                                  TEXT NOT NULL,
    ptu_quantity                            INTEGER NOT NULL,
    cost_usd                                REAL NOT NULL,
    start_date                              TEXT NOT NULL,
    end_date                                TEXT NOT NULL,
    demand_driver                           TEXT NOT NULL,
    status                                  TEXT NOT NULL,
    throughput_capacity_tokens_per_hour     REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS roles (
    role_id             TEXT PRIMARY KEY,
    role_name            TEXT NOT NULL UNIQUE,
    monthly_budget_usd   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    user_id        TEXT PRIMARY KEY,
    display_name    TEXT NOT NULL,
    team_id         TEXT NOT NULL,
    role_id         TEXT REFERENCES roles(role_id)
);
CREATE INDEX IF NOT EXISTS idx_users_team ON users(team_id);

CREATE TABLE IF NOT EXISTS budget_overrides (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id                 TEXT NOT NULL REFERENCES users(user_id),
    year_month               TEXT NOT NULL,
    override_budget_usd      REAL NOT NULL,
    reason                   TEXT,
    UNIQUE(user_id, year_month)
);
"""


class Storage:
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

    # ---------------------------------------------------------------
    # Usage events
    # ---------------------------------------------------------------

    def insert_usage_events(self, events: list[UsageEvent]) -> int:
        import json

        with self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO usage_events
                    (timestamp, actor_id, team_id, source_app, resource_type,
                     quantity, unit_cost_usd, model, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
    ) -> list[UsageEvent]:
        import json

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
                metadata=json.loads(r["metadata_json"]) if r["metadata_json"] else {},
            )
            for r in rows
        ]

    # ---------------------------------------------------------------
    # Capacity pools
    # ---------------------------------------------------------------

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
                    pool.pool_id,
                    pool.model,
                    pool.model_family,
                    pool.version_number,
                    pool.region,
                    pool.ptu_quantity,
                    pool.cost_usd,
                    pool.start_date.isoformat(),
                    pool.end_date.isoformat(),
                    pool.demand_driver.value,
                    pool.status.value,
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
                throughput_capacity_tokens_per_hour=r[
                    "throughput_capacity_tokens_per_hour"
                ],
            )
            for r in rows
        ]

    # ---------------------------------------------------------------
    # Roles / users / budget overrides
    # ---------------------------------------------------------------

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
            Role(
                role_id=r["role_id"],
                role_name=r["role_name"],
                monthly_budget_usd=r["monthly_budget_usd"],
            )
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
            User(
                user_id=r["user_id"],
                display_name=r["display_name"],
                team_id=r["team_id"],
                role_id=r["role_id"],
            )
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
                (
                    override.user_id,
                    override.year_month,
                    override.override_budget_usd,
                    override.reason,
                ),
            )

    def get_effective_budget(self, user_id: str, year_month: str) -> tuple[float, str]:
        """Resolve a user's budget for a given month.

        Returns (budget_usd, source) where source is one of
        "override" | "role" | "default_role" | "unconfigured".
        """
        with self._connect() as conn:
            override_row = conn.execute(
                "SELECT override_budget_usd FROM budget_overrides "
                "WHERE user_id = ? AND year_month = ?",
                (user_id, year_month),
            ).fetchone()
            if override_row is not None:
                return override_row["override_budget_usd"], "override"

            user_row = conn.execute(
                "SELECT role_id FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()

            if user_row is not None and user_row["role_id"] is not None:
                role_row = conn.execute(
                    "SELECT monthly_budget_usd FROM roles WHERE role_id = ?",
                    (user_row["role_id"],),
                ).fetchone()
                if role_row is not None:
                    return role_row["monthly_budget_usd"], "role"

            # Unassigned user (no row, or no role_id) -> fall back to default role
            default_row = conn.execute(
                "SELECT monthly_budget_usd FROM roles WHERE role_name = ?",
                (DEFAULT_ROLE_NAME,),
            ).fetchone()
            if default_row is not None:
                return default_row["monthly_budget_usd"], "default_role"

        return 0.0, "unconfigured"

    # ---------------------------------------------------------------
    # Budget rollups (aggregate spend check — deliberately model-blind)
    # ---------------------------------------------------------------

    def get_user_spend_for_month(self, user_id: str, year_month: str) -> float:
        """Aggregate USD spend for a user in a given month, across all
        source_apps and models. This is the ONLY query the budget-limit
        trigger should read from — model breakdown is a separate,
        analytics-only query (see get_spend_breakdown_by_model) and must
        never gate the budget check itself."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT SUM(quantity * unit_cost_usd) as total
                FROM usage_events
                WHERE actor_id = ? AND substr(timestamp, 1, 7) = ?
                """,
                (user_id, year_month),
            ).fetchone()
        return row["total"] or 0.0

    def get_spend_breakdown_by_model(
        self, user_id: str, year_month: str
    ) -> dict[str, float]:
        """Analytics-only: spend by model for a user in a month. Explains
        the 'why' behind an aggregate total — never used to gate budget."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT model, SUM(quantity * unit_cost_usd) as total
                FROM usage_events
                WHERE actor_id = ? AND substr(timestamp, 1, 7) = ?
                GROUP BY model
                """,
                (user_id, year_month),
            ).fetchall()
        return {r["model"] or "unknown": r["total"] for r in rows}

    def get_team_spend_for_month(self, team_id: str, year_month: str) -> float:
        """Team-level rollup — sums usage_events directly by team_id, which
        is populated on every UsageEvent regardless of the user/role system.
        Independent of the per-user budget check above."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT SUM(quantity * unit_cost_usd) as total
                FROM usage_events
                WHERE team_id = ? AND substr(timestamp, 1, 7) = ?
                """,
                (team_id, year_month),
            ).fetchone()
        return row["total"] or 0.0
