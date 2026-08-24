from datetime import datetime, timedelta

from core.burn_rate import (
    daily_demand_for_model,
    reorder_point,
    should_reorder,
    summarize_burn_rate,
)
from core.models import CapacityPool, DemandDriver, ResourceType, SourceApp, UsageEvent


def _make_event(day_offset: int, tokens: float, model: str = "test-model") -> UsageEvent:
    base = datetime(2026, 1, 1)
    return UsageEvent(
        timestamp=base + timedelta(days=day_offset),
        actor_id="user-1",
        team_id="team-a",
        source_app=SourceApp.SYNTHETIC,
        resource_type=ResourceType.TOKENS,
        quantity=tokens,
        unit_cost_usd=0.000005,
        model=model,
    )


def test_daily_demand_buckets_by_day():
    events = [_make_event(0, 100), _make_event(0, 50), _make_event(1, 200)]
    daily = daily_demand_for_model(events, "test-model")
    assert len(daily) == 2
    assert daily[0].tokens == 150
    assert daily[1].tokens == 200


def test_reorder_point_increases_with_lead_time():
    rp_short = reorder_point(avg_daily_demand=1000, demand_std_dev=100, lead_time_days=2)
    rp_long = reorder_point(avg_daily_demand=1000, demand_std_dev=100, lead_time_days=10)
    assert rp_long > rp_short


def test_reorder_point_increases_with_service_level():
    rp_low = reorder_point(
        avg_daily_demand=1000, demand_std_dev=100, lead_time_days=5, service_level=0.90
    )
    rp_high = reorder_point(
        avg_daily_demand=1000, demand_std_dev=100, lead_time_days=5, service_level=0.99
    )
    assert rp_high > rp_low


def test_summarize_burn_rate_and_should_reorder_trigger():
    pool = CapacityPool(
        pool_id="pool-1",
        model="test-model",
        model_family="test-family",
        version_number=1,
        region="eastus",
        ptu_quantity=1,
        cost_usd=100,
        start_date=datetime(2026, 1, 1),
        end_date=datetime(2026, 1, 3),  # tiny 2-day pool, easy to exhaust
        demand_driver=DemandDriver.HUMAN_DRIVEN,
        throughput_capacity_tokens_per_hour=100,  # 100*24*2 = 4800 total capacity
    )
    events = [_make_event(0, 2000), _make_event(1, 2500)]  # consumes 4500 of 4800

    summary = summarize_burn_rate(pool, events, window_days=14)
    assert summary.tokens_remaining == 300  # 4800 - 4500

    triggered, rp = should_reorder(summary, lead_time_days=5)
    assert triggered is True  # 300 remaining is well under any reasonable reorder point
