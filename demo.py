"""
Runs the full pipeline end-to-end against synthetic data:

    synthetic usage events -> burn rate summary -> reorder logic -> recommendation

This is the fastest way to see the system work with zero setup:

    python demo.py
"""

from __future__ import annotations

from datetime import datetime, timedelta

from connectors.synthetic import SyntheticConnector
from core.burn_rate import summarize_burn_rate
from core.models import CapacityPool, DemandDriver, ModelStatus
from core.recommender import recommend_for_pool

# Realistic-ish lead time for provisioning new PTU capacity: procurement +
# setup, not just an API call. Adjust to match your org's actual process.
PTU_LEAD_TIME_DAYS = 5


def build_demo_pools(start: datetime, days: int) -> list[CapacityPool]:
    """One pool per model, sized deliberately tight against the synthetic
    generator's growth trend + spike so the reorder logic actually fires.
    All pools here are human-driven (coding-assistant style usage) — see
    Roadmap for the agent-driven synthetic generator."""
    end = start + timedelta(days=days)
    return [
        CapacityPool(
            pool_id="pool-gpt4o-eastus",
            model="gpt-4o",
            model_family="gpt-4o",
            version_number=1,
            region="eastus",
            ptu_quantity=20,
            cost_usd=16_000,
            start_date=start,
            end_date=end,
            demand_driver=DemandDriver.HUMAN_DRIVEN,
            status=ModelStatus.ESTABLISHED,
            throughput_capacity_tokens_per_hour=4_500,
        ),
        CapacityPool(
            pool_id="pool-sonnet-eastus",
            model="claude-sonnet-4-6",
            model_family="claude-sonnet",
            version_number=6,
            region="eastus",
            ptu_quantity=18,
            cost_usd=15_000,
            start_date=start,
            end_date=end,
            demand_driver=DemandDriver.HUMAN_DRIVEN,
            status=ModelStatus.ESTABLISHED,
            throughput_capacity_tokens_per_hour=3_800,
        ),
        CapacityPool(
            pool_id="pool-haiku-eastus",
            model="claude-haiku-4-5",
            model_family="claude-haiku",
            version_number=5,
            region="eastus",
            # Deliberately undersized: the synthetic generator ramps traffic
            # onto this model after the "new model release" event, so this
            # pool is the one that should trip the reorder point in the demo.
            ptu_quantity=6,
            cost_usd=3_600,
            start_date=start,
            end_date=end,
            demand_driver=DemandDriver.HUMAN_DRIVEN,
            status=ModelStatus.ESTABLISHED,
            throughput_capacity_tokens_per_hour=1_100,
        ),
    ]


def main() -> None:
    days = 28
    since = datetime.now() - timedelta(days=days)

    print(f"Generating {days} days of synthetic usage across "
          f"{len(['platform', 'data-science', 'customer-ops', 'trading-desk'])} teams...\n")

    connector = SyntheticConnector(days=days)
    events = connector.pull(since=since)
    print(f"Generated {len(events):,} usage events "
          f"totalling {sum(e.cost_usd for e in events):,.2f} USD\n")

    pools = build_demo_pools(start=since, days=days)

    print(f"{'Pool':<25} {'Model':<20} {'Avg/day':>12} {'Remaining':>12} "
          f"{'Days left':>10} {'Action':>10} {'Urgency':>8}")
    print("-" * 100)

    for pool in pools:
        summary = summarize_burn_rate(pool, events, window_days=14)
        rec = recommend_for_pool(
            pool, summary, lead_time_days=PTU_LEAD_TIME_DAYS
        )
        print(
            f"{pool.pool_id:<25} {pool.model:<20} "
            f"{summary.avg_daily_tokens:>12,.0f} "
            f"{summary.tokens_remaining:>12,.0f} "
            f"{summary.days_of_supply_remaining:>10.1f} "
            f"{rec.action:>10} {rec.urgency:>8}"
        )
        print(f"   -> {rec.reason}")
        if rec.projected_stockout_date:
            print(f"   -> Projected stockout: {rec.projected_stockout_date:%Y-%m-%d}")
        print()


if __name__ == "__main__":
    main()
