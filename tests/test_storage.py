import tempfile
from datetime import datetime
from pathlib import Path

import pytest

from core.models import (
    BudgetOverride,
    DemandDriver,
    ModelStatus,
    ResourceType,
    Role,
    SourceApp,
    User,
    UsageEvent,
)
from core.storage import Storage


@pytest.fixture
def storage():
    with tempfile.TemporaryDirectory() as tmp:
        yield Storage(db_path=Path(tmp) / "test.db")


def _event(actor_id="alice", month="2026-08", day="01", cost=1.0, model="gpt-4o"):
    return UsageEvent(
        timestamp=datetime.fromisoformat(f"{month}-{day}T10:00:00"),
        actor_id=actor_id,
        team_id="platform",
        source_app=SourceApp.SYNTHETIC,
        resource_type=ResourceType.TOKENS,
        quantity=1000,
        unit_cost_usd=cost / 1000,
        model=model,
    )


def test_insert_and_retrieve_usage_events(storage):
    events = [_event(), _event(day="02")]
    storage.insert_usage_events(events)
    retrieved = storage.get_usage_events()
    assert len(retrieved) == 2


def test_budget_falls_back_to_default_role_when_unassigned(storage):
    storage.upsert_role(Role("role-nontech", "non_tech", 50.0))
    storage.upsert_role(Role("role-tech", "tech", 200.0))
    # user with no role assigned at all
    storage.upsert_user(User("u1", "Alice", "platform", role_id=None))

    budget, source = storage.get_effective_budget("u1", "2026-08")
    assert budget == 50.0
    assert source == "default_role"


def test_budget_uses_role_when_assigned(storage):
    storage.upsert_role(Role("role-nontech", "non_tech", 50.0))
    storage.upsert_role(Role("role-tech", "tech", 200.0))
    storage.upsert_user(User("u1", "Alice", "platform", role_id="role-tech"))

    budget, source = storage.get_effective_budget("u1", "2026-08")
    assert budget == 200.0
    assert source == "role"


def test_override_takes_precedence_over_role(storage):
    storage.upsert_role(Role("role-nontech", "non_tech", 50.0))
    storage.upsert_role(Role("role-tech", "tech", 200.0))
    storage.upsert_user(User("u1", "Alice", "platform", role_id="role-nontech"))
    storage.add_budget_override(
        BudgetOverride("u1", "2026-08", 200.0, "mid-month role change to tech")
    )

    budget, source = storage.get_effective_budget("u1", "2026-08")
    assert budget == 200.0
    assert source == "override"

    # Override is month-scoped — next month falls back to role budget
    budget_next, source_next = storage.get_effective_budget("u1", "2026-09")
    assert budget_next == 50.0
    assert source_next == "role"


def test_aggregate_spend_is_model_blind(storage):
    storage.insert_usage_events(
        [
            _event(actor_id="u1", cost=10.0, model="gpt-4o"),
            _event(actor_id="u1", cost=15.0, model="claude-sonnet-4-6"),
            _event(actor_id="u2", cost=5.0, model="gpt-4o"),
        ]
    )
    total = storage.get_user_spend_for_month("u1", "2026-08")
    assert total == pytest.approx(25.0)

    breakdown = storage.get_spend_breakdown_by_model("u1", "2026-08")
    assert breakdown["gpt-4o"] == pytest.approx(10.0)
    assert breakdown["claude-sonnet-4-6"] == pytest.approx(15.0)
