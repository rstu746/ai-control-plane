"""
SyntheticCodingAssistantConnector: generates human-paced usage matching how
coding assistants actually get used — concentrated in working hours,
weekday-heavy, with a small fixed set of developers who also work weekends.

Design (agreed in project discussion, not arbitrary defaults):

  - Aggregation that matters is DAILY, not hourly. The reorder trigger for
    these pools is meant to fire ~3 weeks out from exhaustion, which is a
    trend read off daily totals — see CODING_ASSISTANT_LEAD_TIME_DAYS below
    and core/burn_rate.py's daily_demand_for_model(). Hourly timestamps
    still get generated (for dashboard-level realism and in case anyone
    wants an intraday view), but no logic in this project reasons at
    hourly granularity.

  - Weekday shape is not a flat 9-5 block. Volume is distributed across
    four bands with a soft mid-morning peak and a smaller post-lunch bump,
    which is closer to how usage actually looks than a uniform block:
        07:00-09:00  ~8%   (early)
        09:00-17:00  ~70%  (core, itself weighted toward mid-morning)
        17:00-21:00  ~17%  (late)
        21:00-07:00  ~5%   (overnight)

  - Weekend usage is participation-based, not volume-scaled. Rather than
    dimming every developer's usage uniformly on weekends (unrealistic —
    most people are fully off), a FIXED subset of developers (~15-20%,
    chosen once and stable across the whole run) are "weekend-active."
    Only they generate weekend events, at a lighter volume and a flatter
    intraday shape than their own weekday pattern. This produces overall
    weekend volume in the ~10-15% of a weekday's range, but arrived at
    from a realistic mechanism rather than an arbitrary global scalar.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta

from connectors.base import UsageConnector
from core.models import ResourceType, SourceApp, UsageEvent

TEAMS = ["platform", "data-science", "customer-ops", "trading-desk"]
DEVS_PER_TEAM = 5
MODELS = ["gpt-4o", "claude-sonnet-4-6", "claude-haiku-4-5"]
MODEL_UNIT_COST_USD_PER_1K_TOKENS = {
    "gpt-4o": 0.005,
    "claude-sonnet-4-6": 0.006,
    "claude-haiku-4-5": 0.0015,
}

WEEKEND_ACTIVE_SHARE = 0.175  # ~15-20%, fixed per developer for the whole run

# Recommended reorder lead time for coding-assistant pools: the trigger is
# meant to fire on a trend read ~3 weeks out from exhaustion, not react to
# a short-window spike. See core/burn_rate.py should_reorder(lead_time_days=...).
CODING_ASSISTANT_LEAD_TIME_DAYS = 21
CODING_ASSISTANT_ROLLING_WINDOW_DAYS = 21  # keep window >= lead time, see README

# (start_hour, end_hour, share_of_weekday_volume) — must sum to 1.0
WEEKDAY_BANDS = [
    (7, 9, 0.08),    # early
    (9, 11, 0.28),    # core: mid-morning peak
    (11, 13, 0.12),   # core: pre-lunch taper
    (13, 15, 0.20),   # core: post-lunch bump
    (15, 17, 0.10),   # core: afternoon taper
    (17, 21, 0.17),   # late
    (21, 24, 0.03),   # overnight (pre-midnight portion)
    (0, 7, 0.02),      # overnight (post-midnight portion)
]

# Weekend-active developers show a flatter spread, no 9-5 concentration —
# closer to "checked in a few times across the day" than a working block.
WEEKEND_BANDS = [
    (8, 12, 0.30),
    (12, 16, 0.30),
    (16, 20, 0.25),
    (20, 23, 0.15),
]


class SyntheticCodingAssistantConnector(UsageConnector):
    def __init__(self, days: int = 42, seed: int = 7):
        self.days = days
        self._rng = random.Random(seed)
        self._actors = self._build_actors()
        self._weekend_active = self._assign_weekend_active(self._actors)

    def source_name(self) -> str:
        return "synthetic_coding_assistant"

    def _build_actors(self) -> list[tuple[str, str]]:
        """Returns [(actor_id, team_id), ...]."""
        actors = []
        for team in TEAMS:
            for i in range(DEVS_PER_TEAM):
                actors.append((f"{team}-dev-{i}", team))
        return actors

    def _assign_weekend_active(self, actors: list[tuple[str, str]]) -> set[str]:
        """Fixed per developer for the whole run — the same people are
        'weekend types' every week, not resampled."""
        n_weekend_active = max(1, round(len(actors) * WEEKEND_ACTIVE_SHARE))
        chosen = self._rng.sample(actors, k=n_weekend_active)
        return {actor_id for actor_id, _ in chosen}

    def pull(self, since: datetime) -> list[UsageEvent]:
        events: list[UsageEvent] = []

        for day_offset in range(self.days):
            day = since + timedelta(days=day_offset)
            is_weekend = day.weekday() >= 5
            growth_factor = 1.0 + (day_offset / self.days) * 0.4  # +40% by period end

            for actor_id, team_id in self._actors:
                if is_weekend and actor_id not in self._weekend_active:
                    continue

                if is_weekend:
                    # Weekend-active devs are still meaningfully engaged —
                    # fewer, lighter sessions than a full weekday, but not
                    # a token gesture. Tuned so total weekend volume across
                    # the org lands ~10-15% of a weekday's, given only
                    # WEEKEND_ACTIVE_SHARE of devs participate at all.
                    n_calls = self._rng.randint(3, 7)
                    bands = WEEKEND_BANDS
                    volume_factor = 0.65 * growth_factor
                else:
                    n_calls = self._rng.randint(4, 9)
                    bands = WEEKDAY_BANDS
                    volume_factor = growth_factor

                for _ in range(n_calls):
                    model = self._rng.choices(MODELS, weights=[0.5, 0.4, 0.1], k=1)[0]
                    base_tokens = self._rng.uniform(400, 3000)
                    tokens = base_tokens * volume_factor

                    timestamp = self._sample_timestamp(day, bands)

                    events.append(
                        UsageEvent(
                            timestamp=timestamp,
                            actor_id=actor_id,
                            team_id=team_id,
                            source_app=SourceApp.GITHUB_COPILOT,
                            resource_type=ResourceType.TOKENS,
                            quantity=round(tokens, 2),
                            unit_cost_usd=MODEL_UNIT_COST_USD_PER_1K_TOKENS[model]
                            / 1000,
                            model=model,
                            metadata={"demand_driver": "human_driven"},
                        )
                    )

        return events

    def _sample_timestamp(
        self, day: datetime, bands: list[tuple[int, int, float]]
    ) -> datetime:
        band_starts = [b[0] for b in bands]
        band_ends = [b[1] for b in bands]
        weights = [b[2] for b in bands]
        idx = self._rng.choices(range(len(bands)), weights=weights, k=1)[0]
        start_hour, end_hour = band_starts[idx], band_ends[idx]

        hour_fraction = self._rng.uniform(start_hour, end_hour)
        hours = int(hour_fraction)
        minutes = int((hour_fraction - hours) * 60)

        # Bands like (21, 24) and (0, 7) represent a single overnight window
        # split across midnight; hours >= 24 need to roll into the next day.
        day_offset_hours = timedelta(hours=hours % 24, minutes=minutes)
        extra_day = timedelta(days=hours // 24)
        return datetime(day.year, day.month, day.day) + day_offset_hours + extra_day
