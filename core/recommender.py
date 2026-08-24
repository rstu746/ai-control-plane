"""
Turns burn-rate analysis into human-facing recommendations.

Deliberately advisory, not automated: this is real money, so v1 always
surfaces a recommendation for a human to act on rather than triggering a
purchase directly.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from core.burn_rate import BurnRateSummary, should_reorder
from core.models import CapacityPool, ModelStatus, Recommendation


def recommend_for_pool(
    pool: CapacityPool,
    summary: BurnRateSummary,
    lead_time_days: float,
    service_level: float = 0.95,
) -> Recommendation:
    if pool.status == ModelStatus.DEPRECATED:
        return Recommendation(
            pool_id=pool.pool_id,
            action="hold",
            reason=(
                f"'{pool.model}' is deprecated (superseded within its model "
                f"family). No new capacity will be recommended regardless of "
                f"burn rate; historical usage remains visible in trend views."
            ),
            urgency="low",
        )

    triggered, rp_tokens = should_reorder(summary, lead_time_days, service_level)

    if not triggered:
        return Recommendation(
            pool_id=pool.pool_id,
            action="hold",
            reason=(
                f"{summary.tokens_remaining:,.0f} tokens remaining, "
                f"reorder point is {rp_tokens:,.0f}. "
                f"~{summary.days_of_supply_remaining:.1f} days of supply at "
                f"current burn rate."
            ),
            urgency="low",
        )

    projected_stockout = None
    if summary.avg_daily_tokens > 0:
        projected_stockout = datetime.now() + timedelta(
            days=summary.days_of_supply_remaining
        )

    # Urgency: how far past the reorder point are we, relative to lead time buffer
    urgency = "high" if summary.days_of_supply_remaining <= lead_time_days else "medium"

    return Recommendation(
        pool_id=pool.pool_id,
        action="increase",
        reason=(
            f"Burn rate has crossed the reorder point ({rp_tokens:,.0f} tokens). "
            f"Only {summary.days_of_supply_remaining:.1f} days of supply remain "
            f"against a {lead_time_days:.0f}-day procurement lead time for "
            f"'{pool.model}'."
        ),
        urgency=urgency,
        projected_stockout_date=projected_stockout,
    )
