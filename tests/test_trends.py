"""Tests for the trend engine."""

from datetime import datetime, timedelta

from core.models import (
    AgentStatus,
    DiscoverySource,
    Agent,
    AgentTier,
    ResourceType,
    SourceApp,
    UsageEvent,
)
from core.trends import TrendsEngine, _snapshot_id
from storage.sqlite import SqliteBackend


def _make_db(tmp_path) -> SqliteBackend:
    return SqliteBackend(db_path=str(tmp_path / "test.db"))


def _make_events(base: datetime, count: int = 5, model: str = "gpt-4o", agent_id: str | None = None) -> list[UsageEvent]:
    return [
        UsageEvent(
            timestamp=base + timedelta(hours=i),
            actor_id=f"user-{i % 3}",
            team_id="team-a",
            source_app=SourceApp.AI_GATEWAY,
            resource_type=ResourceType.TOKENS,
            quantity=1000.0,
            unit_cost_usd=0.000005,
            model=model,
            agent_id=agent_id,
        )
        for i in range(count)
    ]


class TestDailySnapshots:
    def test_computes_snapshots_for_events(self, tmp_path):
        db = _make_db(tmp_path)
        engine = TrendsEngine(storage=db)
        base = datetime(2026, 1, 15)
        events = _make_events(base)
        snaps = engine.compute_daily_snapshots(events, date=base)
        assert len(snaps) > 0

    def test_total_snapshot_has_correct_tokens(self, tmp_path):
        db = _make_db(tmp_path)
        engine = TrendsEngine(storage=db)
        base = datetime(2026, 1, 15)
        events = _make_events(base, count=5, model="gpt-4o")
        snaps = engine.compute_daily_snapshots(events, date=base)
        total_snap = next((s for s in snaps if s.model is None and s.agent_id is None and s.team_id is None), None)
        assert total_snap is not None
        assert total_snap.total_tokens == 5000.0

    def test_per_model_snapshot_created(self, tmp_path):
        db = _make_db(tmp_path)
        engine = TrendsEngine(storage=db)
        base = datetime(2026, 1, 15)
        events = _make_events(base, model="gpt-4o")
        snaps = engine.compute_daily_snapshots(events, date=base)
        model_snap = next((s for s in snaps if s.model == "gpt-4o"), None)
        assert model_snap is not None

    def test_per_agent_snapshot_created(self, tmp_path):
        db = _make_db(tmp_path)
        engine = TrendsEngine(storage=db)
        base = datetime(2026, 1, 15)
        events = _make_events(base, agent_id="agent-abc")
        snaps = engine.compute_daily_snapshots(events, date=base)
        agent_snap = next((s for s in snaps if s.agent_id == "agent-abc"), None)
        assert agent_snap is not None

    def test_snapshots_written_to_storage(self, tmp_path):
        db = _make_db(tmp_path)
        engine = TrendsEngine(storage=db)
        base = datetime(2026, 1, 15)
        events = _make_events(base)
        engine.compute_daily_snapshots(events, date=base)
        stored = db.get_trend_snapshots(since=base - timedelta(days=1))
        assert len(stored) > 0

    def test_active_actors_counted(self, tmp_path):
        db = _make_db(tmp_path)
        engine = TrendsEngine(storage=db)
        base = datetime(2026, 1, 15)
        events = _make_events(base, count=9)  # 3 distinct actors (user-0,1,2)
        snaps = engine.compute_daily_snapshots(events, date=base)
        total_snap = next((s for s in snaps if s.model is None and s.agent_id is None and s.team_id is None), None)
        assert total_snap.active_actors == 3


class TestDormantDetection:
    def _register_agent(self, db: SqliteBackend, last_seen: datetime | None) -> Agent:
        agent = Agent(
            agent_id="agent-dormant-test",
            name="Dormant Test",
            owner_id="owner-1",
            team_id="team-a",
            source_platform=SourceApp.AI_GATEWAY,
            status=AgentStatus.ACTIVE,
            discovery_source=DiscoverySource.SELF_REGISTRATION,
            discovered_at=datetime(2026, 1, 1),
            last_seen_at=last_seen,
        )
        db.upsert_agent(agent)
        return agent

    def test_agent_without_recent_usage_marked_dormant(self, tmp_path):
        db = _make_db(tmp_path)
        engine = TrendsEngine(storage=db)
        old_date = datetime(2026, 1, 1)
        self._register_agent(db, last_seen=old_date)

        dormant = engine.run_dormant_detection(
            dormancy_days=30, now=datetime(2026, 3, 1)
        )
        assert "agent-dormant-test" in dormant
        refreshed = db.get_agent("agent-dormant-test")
        assert refreshed.status == AgentStatus.DORMANT

    def test_recently_active_agent_not_marked_dormant(self, tmp_path):
        db = _make_db(tmp_path)
        engine = TrendsEngine(storage=db)
        recent = datetime(2026, 2, 25)
        self._register_agent(db, last_seen=recent)

        dormant = engine.run_dormant_detection(
            dormancy_days=30, now=datetime(2026, 3, 1)
        )
        assert "agent-dormant-test" not in dormant


class TestSnapshotId:
    def test_same_dimensions_produce_same_id(self):
        date = datetime(2026, 1, 15)
        id1 = _snapshot_id(date, "gpt-4o", None, "team-a", None, None)
        id2 = _snapshot_id(date, "gpt-4o", None, "team-a", None, None)
        assert id1 == id2

    def test_different_dimensions_produce_different_id(self):
        date = datetime(2026, 1, 15)
        id1 = _snapshot_id(date, "gpt-4o", None, None, None, None)
        id2 = _snapshot_id(date, "claude-sonnet", None, None, None, None)
        assert id1 != id2
