"""Tests for the workflow engine (escalation, token cap, resolution)."""

import uuid
from datetime import datetime, timedelta

import pytest

from core.models import (
    Agent,
    AgentManifest,
    AgentStatus,
    AgentTier,
    ClassificationRequest,
    DiscoverySource,
    SourceApp,
    WorkflowItem,
    WorkflowItemStatus,
    WorkflowItemType,
)
from core.workflow import WorkflowEngine, CAP_DAYS, CAP_FRACTION, REMINDER_1_DAYS, REMINDER_2_DAYS, ESCALATION_DAYS
from storage.sqlite import SqliteBackend


def _make_db(tmp_path) -> SqliteBackend:
    return SqliteBackend(db_path=str(tmp_path / "test.db"))


def _make_agent(db: SqliteBackend) -> Agent:
    agent = Agent(
        agent_id="agent-workflow",
        name="Workflow Test Agent",
        owner_id="owner-1",
        team_id="team-a",
        source_platform=SourceApp.AI_GATEWAY,
        status=AgentStatus.REGISTERED,
        discovery_source=DiscoverySource.SELF_REGISTRATION,
        discovered_at=datetime.now(),
    )
    db.upsert_agent(agent)
    return agent


class TestWorkflowItemCreation:
    def test_open_classification_request(self, tmp_path):
        db = _make_db(tmp_path)
        engine = WorkflowEngine(storage=db)
        agent = _make_agent(db)

        cr = ClassificationRequest(
            request_id="req-1",
            agent_id=agent.agent_id,
            missing_fields=["data_scope", "owner_id"],
            raised_at=datetime.now(),
        )
        item = engine.open_classification_request(cr, agent)

        assert item.item_type == WorkflowItemType.CLASSIFICATION_REQUEST
        assert item.status == WorkflowItemStatus.OPEN
        assert "data_scope" in item.missing_fields

        # Agent status should be updated
        refreshed = db.get_agent(agent.agent_id)
        assert refreshed.status == AgentStatus.UNCLASSIFIED

    def test_open_holistic_review(self, tmp_path):
        db = _make_db(tmp_path)
        engine = WorkflowEngine(storage=db)
        agent = _make_agent(db)

        item = engine.open_holistic_review(agent)

        assert item.item_type == WorkflowItemType.HOLISTIC_REVIEW
        assert item.status == WorkflowItemStatus.OPEN

        refreshed = db.get_agent(agent.agent_id)
        assert refreshed.status == AgentStatus.PENDING_REVIEW


class TestEscalationTimeline:
    def _create_item_at_age(self, db: SqliteBackend, engine: WorkflowEngine, age_days: int):
        """Create a WorkflowItem with raised_at backdated to simulate age.
        Insert directly so raised_at is persisted correctly (upsert does not
        overwrite raised_at to preserve immutability in the engine)."""
        agent = _make_agent(db)
        raised_at = datetime.now() - timedelta(days=age_days)
        item = WorkflowItem(
            item_id=str(uuid.uuid4()),
            agent_id=agent.agent_id,
            item_type=WorkflowItemType.CLASSIFICATION_REQUEST,
            status=WorkflowItemStatus.OPEN,
            raised_at=raised_at,
            due_at=raised_at + timedelta(days=CAP_DAYS),
            missing_fields=["data_scope"],
        )
        db.upsert_workflow_item(item)
        # Also mark the agent as UNCLASSIFIED (as the engine would)
        agent.status = AgentStatus.UNCLASSIFIED
        db.upsert_agent(agent)
        return item, agent

    def test_reminder_fires_at_day3(self, tmp_path):
        db = _make_db(tmp_path)
        engine = WorkflowEngine(storage=db)
        item, agent = self._create_item_at_age(db, engine, age_days=REMINDER_1_DAYS)
        counts = engine.process_escalations(now=datetime.now())
        assert counts["reminded"] >= 1
        updated = db.get_workflow_item(item.item_id)
        assert updated.reminder_count >= 1

    def test_escalation_fires_at_day14(self, tmp_path):
        db = _make_db(tmp_path)
        engine = WorkflowEngine(storage=db)
        item, agent = self._create_item_at_age(db, engine, age_days=ESCALATION_DAYS)
        counts = engine.process_escalations(now=datetime.now())
        assert counts["escalated"] >= 1
        updated = db.get_workflow_item(item.item_id)
        assert updated.status == WorkflowItemStatus.ESCALATED

    def test_cap_fires_at_day21(self, tmp_path):
        db = _make_db(tmp_path)
        engine = WorkflowEngine(storage=db)
        item, agent = self._create_item_at_age(db, engine, age_days=CAP_DAYS)
        counts = engine.process_escalations(now=datetime.now())
        assert counts["capped"] >= 1
        updated = db.get_workflow_item(item.item_id)
        assert updated.status == WorkflowItemStatus.CAPPED

    def test_token_cap_writes_budget_override(self, tmp_path):
        db = _make_db(tmp_path)
        engine = WorkflowEngine(storage=db)
        item, agent = self._create_item_at_age(db, engine, age_days=CAP_DAYS)
        engine.process_escalations(now=datetime.now())
        year_month = datetime.now().strftime("%Y-%m")
        budget, source = db.get_effective_budget(agent.agent_id, year_month)
        assert source == "override"
        # Should be CAP_FRACTION of the fallback 100.0 = 25.0
        assert abs(budget - 25.0) < 1.0

    def test_agent_monthly_token_cap_set(self, tmp_path):
        db = _make_db(tmp_path)
        engine = WorkflowEngine(storage=db)
        item, agent = self._create_item_at_age(db, engine, age_days=CAP_DAYS)
        engine.process_escalations(now=datetime.now())
        updated_agent = db.get_agent(agent.agent_id)
        assert updated_agent.monthly_token_cap is not None
        assert updated_agent.monthly_token_cap < 100.0


class TestResolution:
    def test_resolve_sets_resolved_status(self, tmp_path):
        db = _make_db(tmp_path)
        engine = WorkflowEngine(storage=db)
        agent = _make_agent(db)
        cr = ClassificationRequest(
            request_id="req-1",
            agent_id=agent.agent_id,
            missing_fields=["data_scope"],
            raised_at=datetime.now(),
        )
        item = engine.open_classification_request(cr, agent)
        resolved = engine.resolve(item.item_id, resolved_by="owner@test.com")
        assert resolved.status == WorkflowItemStatus.RESOLVED
        assert resolved.resolved_at is not None

    def test_resolve_updates_agent_status(self, tmp_path):
        db = _make_db(tmp_path)
        engine = WorkflowEngine(storage=db)
        agent = _make_agent(db)
        cr = ClassificationRequest(
            request_id="req-1",
            agent_id=agent.agent_id,
            missing_fields=["data_scope"],
            raised_at=datetime.now(),
        )
        item = engine.open_classification_request(cr, agent)
        engine.resolve(item.item_id)
        refreshed = db.get_agent(agent.agent_id)
        assert refreshed.status == AgentStatus.REGISTERED

    def test_resolve_missing_item_raises(self, tmp_path):
        db = _make_db(tmp_path)
        engine = WorkflowEngine(storage=db)
        with pytest.raises(ValueError):
            engine.resolve("nonexistent-id")
