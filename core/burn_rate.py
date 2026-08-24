"""
Burn rate tracking and reorder-point logic for the Token Supply Chain Planner.

This adapts classic inventory-management math (reorder point, safety stock)
to token capacity pools:

    reorder_point = (avg_daily_demand * lead_time_days) + safety_stock
    safety_stock   = z_score * demand_std_dev * sqrt(lead_time_days)

Where "demand" is tokens consumed per day against a given pool, and
"lead time" is however long it takes to actually provision new capacity
(procurement + setup time for a PTU purchase, not just a click).
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta

from core.models import CapacityPool, UsageEvent

# z-scores for common service-level targets (one-tailed normal distribution)
Z_SCORE_BY_SERVICE_LEVEL = {
    0.90: 1.28,
    0.95: 1.65,
    0.975: 1.96,
    0.99: 2.33,
}


@dataclass
class DailyDemand:
    date: datetime
    tokens: float


@dataclass
class BurnRateSummary:
    pool_id: str
    avg_daily_tokens: float
    std_dev_daily_tokens: float
    total_consumed_tokens: float
    pool_capacity_tokens: float
    tokens_remaining: float
    days_of_supply_remaining: float


def daily_demand_for_model(
    events: list[UsageEvent], model: str, window_days: int | None = None
) -> list[DailyDemand]:
    """Bucket token consumption by day for a given model. If window_days is
    set, only the most recent N days are returned (for a rolling window)."""
    by_day: dict[datetime, float] = {}
    for e in events:
        if e.model != model:
            continue
        day = datetime(e.timestamp.year, e.timestamp.month, e.timestamp.day)
        by_day[day] = by_day.get(day, 0.0) + e.quantity

    daily = [DailyDemand(date=d, tokens=t) for d, t in sorted(by_day.items())]
    if window_days:
        daily = daily[-window_days:]
    return daily


def summarize_burn_rate(
    pool: CapacityPool, events: list[UsageEvent], window_days: int = 14
) -> BurnRateSummary:
    daily = daily_demand_for_model(events, pool.model, window_days=window_days)
    tokens_by_day = [d.tokens for d in daily] or [0.0]

    avg_daily = statistics.mean(tokens_by_day)
    std_dev = statistics.pstdev(tokens_by_day) if len(tokens_by_day) > 1 else 0.0
    total_consumed = sum(tokens_by_day)

    pool_capacity_tokens = pool.throughput_capacity_tokens_per_hour * 24 * (
        (pool.end_date - pool.start_date).days
    )
    remaining = max(pool_capacity_tokens - total_consumed, 0.0)
    days_of_supply = remaining / avg_daily if avg_daily > 0 else float("inf")

    return BurnRateSummary(
        pool_id=pool.pool_id,
        avg_daily_tokens=avg_daily,
        std_dev_daily_tokens=std_dev,
        total_consumed_tokens=total_consumed,
        pool_capacity_tokens=pool_capacity_tokens,
        tokens_remaining=remaining,
        days_of_supply_remaining=days_of_supply,
    )


def reorder_point(
    avg_daily_demand: float,
    demand_std_dev: float,
    lead_time_days: float,
    service_level: float = 0.95,
) -> float:
    """Tokens remaining at which point a new purchase should be triggered."""
    z = Z_SCORE_BY_SERVICE_LEVEL.get(service_level, 1.65)
    safety_stock = z * demand_std_dev * math.sqrt(lead_time_days)
    return (avg_daily_demand * lead_time_days) + safety_stock


def should_reorder(
    summary: BurnRateSummary,
    lead_time_days: float,
    service_level: float = 0.95,
) -> tuple[bool, float]:
    """Returns (should_reorder, reorder_point_tokens)."""
    rp = reorder_point(
        avg_daily_demand=summary.avg_daily_tokens,
        demand_std_dev=summary.std_dev_daily_tokens,
        lead_time_days=lead_time_days,
        service_level=service_level,
    )
    return summary.tokens_remaining <= rp, rp
