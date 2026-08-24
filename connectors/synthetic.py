"""
SyntheticConnector: generates plausible multi-team token usage so the whole
system is demoable with zero real credentials.

Baked in on purpose, so the forecasting/reorder logic has something
interesting to catch:
  - weekday seasonality (usage drops ~70% on weekends)
  - a gradual growth trend (teams ramping up AI usage over time)
  - one demand spike (e.g. a launch or a batch job)
  - one new-model-release event (partial migration of traffic to a new model)
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta

from connectors.base import UsageConnector
from core.models import ResourceType, SourceApp, UsageEvent

TEAMS = ["platform", "data-science", "customer-ops", "trading-desk"]
ACTORS_PER_TEAM = 4
MODELS = ["gpt-4o", "claude-sonnet-4-6", "claude-haiku-4-5"]
MODEL_UNIT_COST_USD_PER_1K_TOKENS = {
    "gpt-4o": 0.005,
    "claude-sonnet-4-6": 0.006,
    "claude-haiku-4-5": 0.0015,
}

# Day offset (from `since`) on which a demand spike occurs, and its multiplier.
SPIKE_DAY_OFFSET = 10
SPIKE_MULTIPLIER = 4.0

# Day offset on which a new, cheaper model becomes available and starts
# absorbing a growing share of traffic from the incumbent.
NEW_MODEL_RELEASE_DAY_OFFSET = 18
NEW_MODEL_NAME = "claude-haiku-4-5"  # already in MODELS; release just shifts share


class SyntheticConnector(UsageConnector):
    def __init__(self, days: int = 28, seed: int = 42):
        self.days = days
        self._rng = random.Random(seed)

    def source_name(self) -> str:
        return SourceApp.SYNTHETIC.value

    def pull(self, since: datetime) -> list[UsageEvent]:
        events: list[UsageEvent] = []

        for day_offset in range(self.days):
            day = since + timedelta(days=day_offset)
            is_weekend = day.weekday() >= 5
            weekend_factor = 0.3 if is_weekend else 1.0
            growth_factor = 1.0 + (day_offset / self.days) * 0.5  # +50% by period end
            spike_factor = SPIKE_MULTIPLIER if day_offset == SPIKE_DAY_OFFSET else 1.0

            for team in TEAMS:
                for actor_idx in range(ACTORS_PER_TEAM):
                    actor_id = f"{team}-user-{actor_idx}"

                    # 3-8 calls per actor per active day
                    n_calls = self._rng.randint(3, 8)
                    for _ in range(n_calls):
                        model = self._pick_model(day_offset)
                        base_tokens = self._rng.uniform(500, 4000)
                        tokens = (
                            base_tokens * weekend_factor * growth_factor * spike_factor
                        )

                        events.append(
                            UsageEvent(
                                timestamp=day
                                + timedelta(hours=self._rng.uniform(8, 19)),
                                actor_id=actor_id,
                                team_id=team,
                                source_app=SourceApp.SYNTHETIC,
                                resource_type=ResourceType.TOKENS,
                                quantity=round(tokens, 2),
                                unit_cost_usd=MODEL_UNIT_COST_USD_PER_1K_TOKENS[model]
                                / 1000,
                                model=model,
                            )
                        )

        return events

    def _pick_model(self, day_offset: int) -> str:
        """Before the release day, traffic favors gpt-4o/sonnet. After it,
        an increasing share shifts to the new (cheaper) model."""
        if day_offset < NEW_MODEL_RELEASE_DAY_OFFSET:
            return self._rng.choices(
                MODELS, weights=[0.5, 0.4, 0.1], k=1
            )[0]

        days_since_release = day_offset - NEW_MODEL_RELEASE_DAY_OFFSET
        new_model_share = min(0.1 + days_since_release * 0.08, 0.6)
        remaining = 1 - new_model_share
        weights = []
        for m in MODELS:
            if m == NEW_MODEL_NAME:
                weights.append(new_model_share)
            else:
                weights.append(remaining / (len(MODELS) - 1))
        return self._rng.choices(MODELS, weights=weights, k=1)[0]
