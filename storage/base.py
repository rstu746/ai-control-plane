"""
StorageBackend protocol — the interface every backend implements.

Callers depend on this protocol, not on any concrete backend. Swapping
SQLite for Snowflake (or adding a second backend) is a configuration change,
not a code change in any caller.

The protocol is deliberately split into functional groups so partial
implementations are easy to reason about. All groups are required for a
complete backend; audit_event storage is the exception — it is handled by
the separate AuditBackend protocol and always routes to Azure Blob in
production regardless of which primary backend is chosen.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from core.models import (
    Agent,
    AgentManifest,
    AgentTier,
    AlertEventType,
    AlertHistory,
    AlertRule,
    AuditEvent,
    BudgetOverride,
    CapacityPool,
    ClassificationResult,
    DemandDriver,
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


@runtime_checkable
class StorageBackend(Protocol):
    """Full storage interface. Implement all methods for a production backend."""

    # ------------------------------------------------------------------
    # Usage events
    # ------------------------------------------------------------------

    def insert_usage_events(self, events: list[UsageEvent]) -> int:
        """Persist events; return count inserted."""
        ...

    def get_usage_events(
        self,
        since: datetime | None = None,
        model: str | None = None,
        actor_id: str | None = None,
        agent_id: str | None = None,
        team_id: str | None = None,
        source_app: SourceApp | None = None,
    ) -> list[UsageEvent]:
        ...

    # ------------------------------------------------------------------
    # Capacity pools
    # ------------------------------------------------------------------

    def upsert_capacity_pool(self, pool: CapacityPool) -> None:
        ...

    def get_capacity_pools(
        self, demand_driver: DemandDriver | None = None
    ) -> list[CapacityPool]:
        ...

    # ------------------------------------------------------------------
    # Roles / users / budget overrides
    # ------------------------------------------------------------------

    def upsert_role(self, role: Role) -> None:
        ...

    def get_roles(self) -> list[Role]:
        ...

    def upsert_user(self, user: User) -> None:
        ...

    def get_users(self, team_id: str | None = None) -> list[User]:
        ...

    def add_budget_override(self, override: BudgetOverride) -> None:
        ...

    def get_effective_budget(self, user_id: str, year_month: str) -> tuple[float, str]:
        """Returns (budget_usd, source) where source is one of
        'override' | 'role' | 'default_role' | 'unconfigured'."""
        ...

    def get_user_spend_for_month(self, user_id: str, year_month: str) -> float:
        ...

    def get_team_spend_for_month(self, team_id: str, year_month: str) -> float:
        ...

    def get_spend_breakdown_by_model(
        self, user_id: str, year_month: str
    ) -> dict[str, float]:
        ...

    # ------------------------------------------------------------------
    # Agents
    # ------------------------------------------------------------------

    def upsert_agent(self, agent: Agent) -> None:
        ...

    def get_agent(self, agent_id: str) -> Agent | None:
        ...

    def get_agents(
        self,
        team_id: str | None = None,
        tier: AgentTier | None = None,
        status: str | None = None,
        source_platform: SourceApp | None = None,
    ) -> list[Agent]:
        ...

    def upsert_agent_manifest(self, agent_id: str, manifest: AgentManifest) -> None:
        ...

    def get_agent_manifest(self, agent_id: str) -> AgentManifest | None:
        ...

    # ------------------------------------------------------------------
    # Workflows
    # ------------------------------------------------------------------

    def upsert_workflow(self, workflow: Workflow) -> None:
        ...

    def get_workflow(self, workflow_id: str) -> Workflow | None:
        ...

    def get_workflows(self) -> list[Workflow]:
        ...

    # ------------------------------------------------------------------
    # Classification results
    # ------------------------------------------------------------------

    def insert_classification_result(self, result: ClassificationResult) -> None:
        ...

    def get_classification_results(
        self, agent_id: str | None = None
    ) -> list[ClassificationResult]:
        ...

    # ------------------------------------------------------------------
    # Workflow items
    # ------------------------------------------------------------------

    def upsert_workflow_item(self, item: WorkflowItem) -> None:
        ...

    def get_workflow_item(self, item_id: str) -> WorkflowItem | None:
        ...

    def get_workflow_items(
        self,
        agent_id: str | None = None,
        status: WorkflowItemStatus | None = None,
        item_type: WorkflowItemType | None = None,
    ) -> list[WorkflowItem]:
        ...

    # ------------------------------------------------------------------
    # Alert rules
    # ------------------------------------------------------------------

    def upsert_alert_rule(self, rule: AlertRule) -> None:
        ...

    def get_alert_rules(
        self,
        team_id: str | None = None,
        event_type: AlertEventType | None = None,
        active_only: bool = True,
    ) -> list[AlertRule]:
        ...

    def insert_alert_history(self, record: AlertHistory) -> None:
        ...

    def get_alert_history(
        self,
        rule_id: str | None = None,
        since: datetime | None = None,
    ) -> list[AlertHistory]:
        ...

    # ------------------------------------------------------------------
    # Trend snapshots
    # ------------------------------------------------------------------

    def upsert_trend_snapshot(self, snapshot: TrendSnapshot) -> None:
        ...

    def get_trend_snapshots(
        self,
        since: datetime | None = None,
        model: str | None = None,
        agent_id: str | None = None,
        team_id: str | None = None,
        source_app: SourceApp | None = None,
    ) -> list[TrendSnapshot]:
        ...


@runtime_checkable
class AuditBackend(Protocol):
    """Separate protocol for the immutable audit trail.

    In production this routes exclusively to Azure Blob Storage. The primary
    StorageBackend also persists audit events for queryability, but Blob is
    authoritative."""

    def append_audit_event(self, event: AuditEvent) -> None:
        """Append an audit event. Must never mutate or delete existing events."""
        ...

    def get_audit_events(
        self,
        agent_id: str | None = None,
        since: datetime | None = None,
        event_type: str | None = None,
    ) -> list[AuditEvent]:
        ...
