"""
Trends engine — daily aggregation snapshots, adoption curves, and dormant detection.

Responsibilities:
  1. Build daily TrendSnapshot aggregations per dimension (model, agent, team,
     source_app, demand_driver) from raw UsageEvents.
  2. Compute week-over-week deltas on the weekly roll-up.
  3. Detect model adoption shifts (significant week-over-week traffic movement
     between models) and emit reclassification signals.
  4. Run dormant agent detection on a configurable schedule.
  5. Fire automatic quarterly sweeps — all dormant agents get an alert.

The trends engine is designed to be called on a schedule (daily / weekly).
It writes TrendSnapshot rows to storage; dashboards read from those rather
than hitting raw usage_events directly.
"""

from __future__ import annotations

import hashlib
import logging
import statistics
from collections import defaultdict
from datetime import datetime, timedelta

from core.models import (
    AgentStatus,
    AlertEventType,
    DemandDriver,
    Severity,
    SourceApp,
    TrendSnapshot,
    UsageEvent,
)
from storage.sqlite import SqliteBackend

logger = logging.getLogger(__name__)

# A model adoption shift is flagged when week-over-week change exceeds this
# fraction of the prior week's total (e.g. 0.5 = 50% swing).
ADOPTION_SHIFT_THRESHOLD = 0.5

# Minimum weekly token volume before we bother computing adoption shifts
# (filters out noise from very low-traffic models)
ADOPTION_SHIFT_MIN_TOKENS = 10_000


class TrendsEngine:
    """Computes and stores trend snapshots; fires alerts for notable shifts."""

    def __init__(self, storage: SqliteBackend, alerting=None):
        self._storage = storage
        self._alerting = alerting  # core.alerting.AlertDispatcher — optional

    # ------------------------------------------------------------------
    # Daily snapshot computation
    # ------------------------------------------------------------------

    def compute_daily_snapshots(
        self,
        events: list[UsageEvent],
        date: datetime | None = None,
    ) -> list[TrendSnapshot]:
        """Aggregate a day's UsageEvents into TrendSnapshot rows.
        Writes snapshots to storage and returns them.

        Dimensions aggregated:
          - Per model (across all agents/teams)
          - Per agent_id (across all models)
          - Per team_id (across all models)
          - Per source_app
          - Per demand_driver (human vs agent)
          - Total across everything (all dimensions None)
        """
        if date is None:
            if events:
                date = min(e.timestamp for e in events)
                date = datetime(date.year, date.month, date.day)
            else:
                date = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

        snapshots: list[TrendSnapshot] = []

        # Helper: build one snapshot from a filtered subset
        def _snap(
            subset: list[UsageEvent],
            model: str | None = None,
            agent_id: str | None = None,
            team_id: str | None = None,
            source_app: SourceApp | None = None,
            demand_driver: DemandDriver | None = None,
        ) -> TrendSnapshot:
            snapshot_id = _snapshot_id(
                date, model, agent_id, team_id,
                source_app.value if source_app else None,
                demand_driver.value if demand_driver else None,
            )
            actors = {e.actor_id for e in subset}
            return TrendSnapshot(
                snapshot_id=snapshot_id,
                snapshot_date=date,
                model=model,
                agent_id=agent_id,
                team_id=team_id,
                source_app=source_app,
                demand_driver=demand_driver,
                total_tokens=sum(e.quantity for e in subset),
                total_cost_usd=sum(e.cost_usd for e in subset),
                event_count=len(subset),
                active_actors=len(actors),
            )

        # 1. Total (all None)
        snapshots.append(_snap(events))

        # 2. Per model
        by_model: dict[str, list[UsageEvent]] = defaultdict(list)
        for e in events:
            if e.model:
                by_model[e.model].append(e)
        for model, evts in by_model.items():
            snapshots.append(_snap(evts, model=model))

        # 3. Per agent_id
        by_agent: dict[str, list[UsageEvent]] = defaultdict(list)
        for e in events:
            if e.agent_id:
                by_agent[e.agent_id].append(e)
        for agent_id, evts in by_agent.items():
            snapshots.append(_snap(evts, agent_id=agent_id))

        # 4. Per team_id
        by_team: dict[str, list[UsageEvent]] = defaultdict(list)
        for e in events:
            if e.team_id:
                by_team[e.team_id].append(e)
        for team_id, evts in by_team.items():
            snapshots.append(_snap(evts, team_id=team_id))

        # 5. Per source_app
        by_source: dict[SourceApp, list[UsageEvent]] = defaultdict(list)
        for e in events:
            by_source[e.source_app].append(e)
        for source_app, evts in by_source.items():
            snapshots.append(_snap(evts, source_app=source_app))

        # 6. Per demand_driver (inferred from agent registry)
        by_driver: dict[str, list[UsageEvent]] = defaultdict(list)
        for e in events:
            driver_val = e.metadata.get("demand_driver")
            if driver_val:
                by_driver[driver_val].append(e)
        for driver_val, evts in by_driver.items():
            try:
                driver = DemandDriver(driver_val)
                snapshots.append(_snap(evts, demand_driver=driver))
            except ValueError:
                pass

        # Persist
        for snap in snapshots:
            self._storage.upsert_trend_snapshot(snap)

        logger.info(
            "Computed %d trend snapshots for %s",
            len(snapshots), date.strftime("%Y-%m-%d"),
        )
        return snapshots

    # ------------------------------------------------------------------
    # Week-over-week delta computation
    # ------------------------------------------------------------------

    def compute_week_over_week(
        self, reference_date: datetime | None = None
    ) -> list[TrendSnapshot]:
        """Compute week-over-week deltas for all per-model snapshots.
        Updates existing snapshots with the week_over_week_delta field.
        Returns the updated snapshots."""
        if reference_date is None:
            reference_date = datetime.now()

        this_week_start = reference_date - timedelta(days=7)
        last_week_start = reference_date - timedelta(days=14)

        this_week = self._storage.get_trend_snapshots(since=this_week_start)
        last_week = self._storage.get_trend_snapshots(since=last_week_start)
        last_week = [s for s in last_week if s.snapshot_date < this_week_start]

        # Group by model for comparison
        def _sum_tokens(snaps: list[TrendSnapshot], model: str) -> float:
            return sum(s.total_tokens for s in snaps if s.model == model and s.agent_id is None)

        models = {s.model for s in this_week + last_week if s.model}
        updated: list[TrendSnapshot] = []
        adoption_shifts: list[tuple[str, float, float]] = []  # (model, this, last)

        for model in models:
            this_tokens = _sum_tokens(this_week, model)
            last_tokens = _sum_tokens(last_week, model)

            if last_tokens == 0:
                delta = None
            else:
                delta = (this_tokens - last_tokens) / last_tokens

            # Flag adoption shifts
            if (
                delta is not None
                and abs(delta) >= ADOPTION_SHIFT_THRESHOLD
                and max(this_tokens, last_tokens) >= ADOPTION_SHIFT_MIN_TOKENS
            ):
                adoption_shifts.append((model, this_tokens, last_tokens))

            # Update snapshots for this model with the delta
            model_snaps = [s for s in this_week if s.model == model]
            for snap in model_snaps:
                snap.week_over_week_delta = delta
                self._storage.upsert_trend_snapshot(snap)
                updated.append(snap)

        # Fire adoption shift alerts
        for model, this_tokens, last_tokens in adoption_shifts:
            direction = "increase" if this_tokens > last_tokens else "decrease"
            change_pct = abs((this_tokens - last_tokens) / max(last_tokens, 1)) * 100
            logger.info(
                "Model adoption shift detected: %s %.0f%% %s",
                model, change_pct, direction,
            )
            if self._alerting:
                self._alerting.dispatch(
                    event_type=AlertEventType.MODEL_ADOPTION_SHIFT,
                    severity=Severity.LOW,
                    payload={
                        "model": model,
                        "direction": direction,
                        "this_week_tokens": this_tokens,
                        "last_week_tokens": last_tokens,
                        "change_pct": round(change_pct, 1),
                    },
                )

        return updated

    # ------------------------------------------------------------------
    # Dormant agent detection — automatic, configurable cadence
    # ------------------------------------------------------------------

    def run_dormant_detection(
        self,
        dormancy_days: int = 30,
        now: datetime | None = None,
    ) -> list[str]:
        """Detect dormant agents and fire alerts. Returns list of agent_ids
        marked dormant. Call this daily; the dormancy threshold filters out
        agents that have simply not been used recently enough."""
        if now is None:
            now = datetime.now()
        cutoff = now - timedelta(days=dormancy_days)

        active_agents = self._storage.get_agents(status=AgentStatus.ACTIVE.value)
        dormant_ids: list[str] = []

        for agent in active_agents:
            last_seen = agent.last_seen_at
            if last_seen is None or last_seen < cutoff:
                agent.status = AgentStatus.DORMANT
                self._storage.upsert_agent(agent)
                dormant_ids.append(agent.agent_id)

                if self._alerting:
                    self._alerting.dispatch(
                        event_type=AlertEventType.DORMANT_AGENT_DETECTED,
                        severity=Severity.LOW,
                        team_id=agent.team_id,
                        payload={
                            "agent_id": agent.agent_id,
                            "agent_name": agent.name,
                            "team_id": agent.team_id,
                            "last_seen_at": last_seen.isoformat() if last_seen else None,
                            "dormancy_days": dormancy_days,
                            "tier": agent.tier.value,
                            "recommendation": (
                                "Review whether this agent is still needed. "
                                "Dormant agents at Tier 2+ should be decommissioned "
                                "or reconfirmed on the quarterly review cadence."
                            ),
                        },
                    )

        if dormant_ids:
            logger.info(
                "Dormant detection: %d agents marked dormant (threshold: %d days)",
                len(dormant_ids), dormancy_days,
            )

        return dormant_ids

    def run_quarterly_sweep(self, now: datetime | None = None) -> list[str]:
        """Fire dormant_agent_detected for ALL currently dormant agents,
        regardless of when they became dormant. Designed to run quarterly
        (e.g. first Monday of each quarter) to prompt the quarterly review
        cadence specified in the governance framework."""
        if now is None:
            now = datetime.now()

        dormant_agents = self._storage.get_agents(status=AgentStatus.DORMANT.value)
        swept_ids: list[str] = []

        for agent in dormant_agents:
            swept_ids.append(agent.agent_id)
            if self._alerting:
                self._alerting.dispatch(
                    event_type=AlertEventType.DORMANT_AGENT_DETECTED,
                    severity=Severity.LOW,
                    team_id=agent.team_id,
                    payload={
                        "agent_id": agent.agent_id,
                        "agent_name": agent.name,
                        "team_id": agent.team_id,
                        "last_seen_at": (
                            agent.last_seen_at.isoformat() if agent.last_seen_at else None
                        ),
                        "tier": agent.tier.value,
                        "sweep_type": "quarterly",
                        "recommendation": (
                            "Quarterly review: confirm this agent is still needed, "
                            "decommission if not, or update last_seen via re-registration."
                        ),
                    },
                )

        logger.info(
            "Quarterly sweep: %d dormant agents notified", len(swept_ids)
        )
        return swept_ids

    # ------------------------------------------------------------------
    # Convenience: model adoption summary for dashboards
    # ------------------------------------------------------------------

    def model_adoption_summary(
        self, days: int = 28
    ) -> list[dict]:
        """Return a list of model adoption records sorted by total_tokens
        descending, covering the past N days. Suitable for a dashboard table."""
        since = datetime.now() - timedelta(days=days)
        snaps = self._storage.get_trend_snapshots(since=since)

        # Aggregate per model (agent_id=None snapshots to avoid double-counting)
        by_model: dict[str, dict] = defaultdict(
            lambda: {"total_tokens": 0.0, "total_cost_usd": 0.0, "days_active": 0}
        )
        for s in snaps:
            if s.model and s.agent_id is None and s.team_id is None:
                m = by_model[s.model]
                m["total_tokens"] += s.total_tokens
                m["total_cost_usd"] += s.total_cost_usd
                m["days_active"] += 1 if s.total_tokens > 0 else 0
                m["week_over_week_delta"] = s.week_over_week_delta  # last one wins

        return sorted(
            [{"model": k, **v} for k, v in by_model.items()],
            key=lambda x: x["total_tokens"],
            reverse=True,
        )


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _snapshot_id(
    date: datetime,
    model: str | None,
    agent_id: str | None,
    team_id: str | None,
    source_app: str | None,
    demand_driver: str | None,
) -> str:
    """Deterministic snapshot_id from dimension combination.
    Same dimensions on the same date always produce the same ID,
    making upsert idempotent."""
    key = "|".join([
        date.strftime("%Y-%m-%d"),
        model or "",
        agent_id or "",
        team_id or "",
        source_app or "",
        demand_driver or "",
    ])
    return hashlib.sha256(key.encode()).hexdigest()[:32]
