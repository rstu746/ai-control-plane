"""
Workflow engine — tracks resolution of ClassificationRequests and holistic reviews.

Every agent that cannot be classified (manifest too sparse) or that uses
agent-type control flow (runtime tool selection) gets a WorkflowItem. The
engine runs the escalation timeline, fires reminder and escalation webhooks,
and applies the token cap when the resolution deadline passes.

Escalation timeline (all durations configurable):

  Day 0   WorkflowItem created → webhook: classification_request or holistic_review_required
  Day +3  First reminder        → webhook: workflow_reminder (medium)
  Day +7  Second reminder       → webhook: workflow_reminder (medium) + escalation target
  Day +14 Escalation            → webhook: workflow_escalated (high)
  Day +21 Token cap applied     → webhook: token_cap_applied (high)
            BudgetOverride written for the agent's actor_id at a reduced ceiling.
            Agent status updated to CAPPED.

Phase A enforcement: writes a BudgetOverride to the primary storage backend.
Any routing through the control plane's budget resolution logic picks this up
automatically. Platform API enforcement (Phase B) is a roadmap item.

Escalation webhook routing:
  The engine looks up the agent's team_id in alert_rules to find that team's
  registered escalation webhook. If none is found, it falls back to the
  platform-wide fallback rule (team_id IS NULL in alert_rules).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta

from core.models import (
    Agent,
    AgentStatus,
    AlertEventType,
    AuditEvent,
    BudgetOverride,
    ClassificationRequest,
    ControlFlowType,
    Severity,
    WorkflowItem,
    WorkflowItemStatus,
    WorkflowItemType,
)
from storage.sqlite import SqliteBackend

logger = logging.getLogger(__name__)

# Escalation timeline in days (all relative to raised_at)
REMINDER_1_DAYS = 3
REMINDER_2_DAYS = 7
ESCALATION_DAYS = 14
CAP_DAYS = 21

# Token cap fraction applied at T+CAP_DAYS: reduce the agent's monthly budget
# to this fraction of the role-based budget as a signal.
CAP_FRACTION = 0.25

# The "due" date shown in the WorkflowItem is the cap deadline.
DUE_DAYS = CAP_DAYS


class WorkflowEngine:
    """Manages WorkflowItem lifecycle: creation, reminders, escalation, and capping."""

    def __init__(self, storage: SqliteBackend, alerting=None):
        self._storage = storage
        self._alerting = alerting  # core.alerting.AlertDispatcher — injected to avoid circular import

    # ------------------------------------------------------------------
    # WorkflowItem creation
    # ------------------------------------------------------------------

    def open_classification_request(
        self,
        classification_request: ClassificationRequest,
        agent: Agent,
    ) -> WorkflowItem:
        """Create a WorkflowItem for a missing-manifest classification request."""
        now = datetime.now()
        item = WorkflowItem(
            item_id=str(uuid.uuid4()),
            agent_id=agent.agent_id,
            item_type=WorkflowItemType.CLASSIFICATION_REQUEST,
            status=WorkflowItemStatus.OPEN,
            raised_at=now,
            due_at=now + timedelta(days=DUE_DAYS),
            escalation_target_webhook=self._resolve_escalation_webhook(agent.team_id),
            missing_fields=classification_request.missing_fields,
        )
        self._storage.upsert_workflow_item(item)

        # Update agent status
        agent.status = AgentStatus.UNCLASSIFIED
        self._storage.upsert_agent(agent)

        self._write_audit(
            agent_id=agent.agent_id,
            event_type="classification_request_opened",
            before={},
            after={"item_id": item.item_id, "missing_fields": item.missing_fields},
        )

        if self._alerting:
            self._alerting.dispatch(
                event_type=AlertEventType.CLASSIFICATION_REQUEST,
                severity=Severity.MEDIUM,
                team_id=agent.team_id,
                payload={
                    "item_id": item.item_id,
                    "agent_id": agent.agent_id,
                    "agent_name": agent.name,
                    "team_id": agent.team_id,
                    "missing_fields": item.missing_fields,
                    "due_at": item.due_at.isoformat(),
                },
            )

        logger.info(
            "Classification request opened for agent %s (item %s)",
            agent.agent_id, item.item_id,
        )
        return item

    def open_holistic_review(self, agent: Agent) -> WorkflowItem:
        """Create a WorkflowItem for an agent-type build requiring holistic review."""
        now = datetime.now()
        item = WorkflowItem(
            item_id=str(uuid.uuid4()),
            agent_id=agent.agent_id,
            item_type=WorkflowItemType.HOLISTIC_REVIEW,
            status=WorkflowItemStatus.OPEN,
            raised_at=now,
            due_at=now + timedelta(days=DUE_DAYS),
            escalation_target_webhook=self._resolve_escalation_webhook(agent.team_id),
            notes=(
                "Agent-type control flow detected. A holistic review is required because "
                "runtime tool/MCP selection means individually safe capabilities may combine "
                "into something none of them exposes alone. The fast path is not available."
            ),
        )
        self._storage.upsert_workflow_item(item)

        agent.status = AgentStatus.PENDING_REVIEW
        self._storage.upsert_agent(agent)

        self._write_audit(
            agent_id=agent.agent_id,
            event_type="holistic_review_opened",
            before={},
            after={"item_id": item.item_id},
        )

        if self._alerting:
            self._alerting.dispatch(
                event_type=AlertEventType.HOLISTIC_REVIEW_REQUIRED,
                severity=Severity.MEDIUM,
                team_id=agent.team_id,
                payload={
                    "item_id": item.item_id,
                    "agent_id": agent.agent_id,
                    "agent_name": agent.name,
                    "team_id": agent.team_id,
                    "due_at": item.due_at.isoformat(),
                },
            )

        logger.info(
            "Holistic review opened for agent %s (item %s)",
            agent.agent_id, item.item_id,
        )
        return item

    # ------------------------------------------------------------------
    # Escalation processing — call this on a schedule (e.g. daily)
    # ------------------------------------------------------------------

    def process_escalations(self, now: datetime | None = None) -> dict[str, int]:
        """Run the escalation timeline for all open WorkflowItems.
        Returns a summary dict: {reminded: N, escalated: N, capped: N}."""
        if now is None:
            now = datetime.now()

        counts = {"reminded": 0, "escalated": 0, "capped": 0}

        open_items = self._storage.get_workflow_items(
            status=WorkflowItemStatus.OPEN,
        ) + self._storage.get_workflow_items(
            status=WorkflowItemStatus.REMINDED,
        ) + self._storage.get_workflow_items(
            status=WorkflowItemStatus.ESCALATED,
        )

        for item in open_items:
            age_days = (now - item.raised_at).days
            agent = self._storage.get_agent(item.agent_id)
            if agent is None:
                continue

            if age_days >= CAP_DAYS and item.status != WorkflowItemStatus.CAPPED:
                self._apply_token_cap(item, agent, now)
                counts["capped"] += 1

            elif age_days >= ESCALATION_DAYS and item.status not in (
                WorkflowItemStatus.ESCALATED, WorkflowItemStatus.CAPPED
            ):
                self._escalate(item, agent, now)
                counts["escalated"] += 1

            elif age_days >= REMINDER_2_DAYS and item.reminder_count < 2:
                self._send_reminder(item, agent, now, reminder_number=2)
                counts["reminded"] += 1

            elif age_days >= REMINDER_1_DAYS and item.reminder_count < 1:
                self._send_reminder(item, agent, now, reminder_number=1)
                counts["reminded"] += 1

        return counts

    # ------------------------------------------------------------------
    # Resolution
    # ------------------------------------------------------------------

    def resolve(self, item_id: str, resolved_by: str = "manual", notes: str = "") -> WorkflowItem:
        """Mark a WorkflowItem resolved. Call this after the owner has
        submitted the missing information or completed the holistic review."""
        item = self._storage.get_workflow_item(item_id)
        if item is None:
            raise ValueError(f"WorkflowItem not found: {item_id}")

        prev_status = item.status.value
        item.status = WorkflowItemStatus.RESOLVED
        item.resolved_at = datetime.now()
        item.notes = notes or item.notes
        self._storage.upsert_workflow_item(item)

        agent = self._storage.get_agent(item.agent_id)
        if agent and agent.status in (
            AgentStatus.UNCLASSIFIED, AgentStatus.PENDING_REVIEW
        ):
            agent.status = AgentStatus.REGISTERED  # ready for re-classification
            self._storage.upsert_agent(agent)

        self._write_audit(
            agent_id=item.agent_id,
            event_type="workflow_item_resolved",
            before={"status": prev_status},
            after={"status": "resolved", "resolved_by": resolved_by, "notes": notes},
        )

        logger.info(
            "WorkflowItem %s resolved by %s for agent %s",
            item_id, resolved_by, item.agent_id,
        )
        return item

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _send_reminder(
        self, item: WorkflowItem, agent: Agent, now: datetime, reminder_number: int
    ) -> None:
        item.reminder_count = reminder_number
        item.status = WorkflowItemStatus.REMINDED
        self._storage.upsert_workflow_item(item)

        if self._alerting:
            self._alerting.dispatch(
                event_type=AlertEventType.WORKFLOW_REMINDER,
                severity=Severity.MEDIUM,
                team_id=agent.team_id,
                payload={
                    "item_id": item.item_id,
                    "agent_id": agent.agent_id,
                    "agent_name": agent.name,
                    "reminder_number": reminder_number,
                    "days_open": (now - item.raised_at).days,
                    "due_at": item.due_at.isoformat(),
                    "missing_fields": item.missing_fields,
                },
            )
        logger.info(
            "Reminder %d sent for WorkflowItem %s (agent %s)",
            reminder_number, item.item_id, agent.agent_id,
        )

    def _escalate(self, item: WorkflowItem, agent: Agent, now: datetime) -> None:
        item.status = WorkflowItemStatus.ESCALATED
        self._storage.upsert_workflow_item(item)

        # Use team's registered escalation webhook (already stored on item at creation)
        if self._alerting:
            self._alerting.dispatch(
                event_type=AlertEventType.WORKFLOW_ESCALATED,
                severity=Severity.HIGH,
                team_id=agent.team_id,
                webhook_override=item.escalation_target_webhook,
                payload={
                    "item_id": item.item_id,
                    "agent_id": agent.agent_id,
                    "agent_name": agent.name,
                    "team_id": agent.team_id,
                    "days_open": (now - item.raised_at).days,
                    "days_until_cap": max(0, CAP_DAYS - (now - item.raised_at).days),
                    "due_at": item.due_at.isoformat(),
                    "missing_fields": item.missing_fields,
                },
            )

        self._write_audit(
            agent_id=agent.agent_id,
            event_type="workflow_item_escalated",
            before={"status": "reminded"},
            after={"status": "escalated"},
        )
        logger.warning(
            "WorkflowItem %s escalated for agent %s",
            item.item_id, agent.agent_id,
        )

    def _apply_token_cap(
        self, item: WorkflowItem, agent: Agent, now: datetime
    ) -> None:
        """Phase A enforcement: write a BudgetOverride that caps the agent's
        actor_id spend to CAP_FRACTION of the default budget."""
        item.status = WorkflowItemStatus.CAPPED
        self._storage.upsert_workflow_item(item)

        # Resolve the current effective budget to compute the capped value
        year_month = now.strftime("%Y-%m")
        current_budget, _ = self._storage.get_effective_budget(agent.agent_id, year_month)
        if current_budget == 0.0:
            # No budget configured; use a nominal sentinel value
            current_budget = 100.0

        capped_budget = round(current_budget * CAP_FRACTION, 2)

        override = BudgetOverride(
            user_id=agent.agent_id,
            year_month=year_month,
            override_budget_usd=capped_budget,
            reason=(
                f"Automatic token cap applied by workflow engine: WorkflowItem "
                f"{item.item_id} not resolved within {CAP_DAYS} days. "
                f"Resolve the classification request to lift the cap."
            ),
        )
        self._storage.add_budget_override(override)

        # Also update the agent entity
        agent.monthly_token_cap = capped_budget
        self._storage.upsert_agent(agent)

        if self._alerting:
            self._alerting.dispatch(
                event_type=AlertEventType.TOKEN_CAP_APPLIED,
                severity=Severity.HIGH,
                team_id=agent.team_id,
                webhook_override=item.escalation_target_webhook,
                payload={
                    "item_id": item.item_id,
                    "agent_id": agent.agent_id,
                    "agent_name": agent.name,
                    "team_id": agent.team_id,
                    "capped_budget_usd": capped_budget,
                    "previous_budget_usd": current_budget,
                    "cap_fraction": CAP_FRACTION,
                    "reason": "WorkflowItem not resolved within deadline",
                    "resolution_url": f"/agents/{agent.agent_id}/workflow/{item.item_id}",
                },
            )

        self._write_audit(
            agent_id=agent.agent_id,
            event_type="token_cap_applied",
            before={"budget_usd": current_budget},
            after={"capped_budget_usd": capped_budget, "item_id": item.item_id},
        )
        logger.warning(
            "Token cap applied for agent %s: %.2f USD (was %.2f)",
            agent.agent_id, capped_budget, current_budget,
        )

    def _resolve_escalation_webhook(self, team_id: str) -> str | None:
        """Look up the team's registered escalation webhook. Falls back to the
        platform-wide rule (team_id IS NULL) if none found for this team."""
        rules = self._storage.get_alert_rules(
            team_id=team_id,
            event_type=AlertEventType.WORKFLOW_ESCALATED,
            active_only=True,
        )
        if rules:
            # Prefer team-specific over platform-wide
            team_rules = [r for r in rules if r.team_id == team_id]
            if team_rules:
                return team_rules[0].webhook_url
            return rules[0].webhook_url
        return None

    def _write_audit(
        self,
        agent_id: str,
        event_type: str,
        before: dict,
        after: dict,
    ) -> None:
        event = AuditEvent(
            event_id=str(uuid.uuid4()),
            timestamp=datetime.now(),
            event_type=event_type,
            agent_id=agent_id,
            actor_id="workflow-engine",
            before_state=before,
            after_state=after,
        )
        self._storage.append_audit_event(event)
