from datetime import datetime, timedelta

from connectors.synthetic_coding_assistant import (
    SyntheticCodingAssistantConnector,
    WEEKEND_ACTIVE_SHARE,
)


def _generate(days=42, seed=7):
    conn = SyntheticCodingAssistantConnector(days=days, seed=seed)
    since = datetime(2026, 7, 1)  # a Wednesday
    return conn, since, conn.pull(since=since)


def test_weekend_volume_lands_in_target_range():
    conn, since, events = _generate()
    weekday_tokens = sum(e.quantity for e in events if e.timestamp.weekday() < 5)
    weekend_tokens = sum(e.quantity for e in events if e.timestamp.weekday() >= 5)
    n_weekdays = sum(1 for i in range(42) if (since + timedelta(days=i)).weekday() < 5)
    n_weekends = 42 - n_weekdays

    avg_weekday_daily = weekday_tokens / n_weekdays
    avg_weekend_daily = weekend_tokens / n_weekends
    ratio = avg_weekend_daily / avg_weekday_daily

    assert 0.08 <= ratio <= 0.18  # target ~10-15%, generous tolerance for RNG


def test_weekday_band_distribution_matches_design():
    conn, since, events = _generate()
    weekday_events = [e for e in events if e.timestamp.weekday() < 5]
    total = sum(e.quantity for e in weekday_events)

    def band_pct(lo, hi):
        return sum(e.quantity for e in weekday_events if lo <= e.timestamp.hour < hi) / total

    assert abs(band_pct(7, 9) - 0.08) < 0.03
    assert abs(band_pct(9, 17) - 0.70) < 0.05
    assert abs(band_pct(17, 21) - 0.17) < 0.05
    overnight = band_pct(21, 24) + band_pct(0, 7)
    assert abs(overnight - 0.05) < 0.03


def test_weekend_active_developers_are_fixed_not_resampled():
    conn, since, events = _generate()
    by_week_actors: dict[int, set[str]] = {}
    for e in events:
        if e.timestamp.weekday() >= 5:
            week_num = (e.timestamp - since).days // 7
            by_week_actors.setdefault(week_num, set()).add(e.actor_id)

    distinct_sets = {frozenset(v) for v in by_week_actors.values()}
    assert len(distinct_sets) == 1, "weekend-active set changed across weeks"


def test_weekend_active_share_is_roughly_target():
    conn, since, events = _generate()
    expected = round(len(conn._actors) * WEEKEND_ACTIVE_SHARE)
    assert len(conn._weekend_active) == expected


def test_no_events_for_non_weekend_active_devs_on_weekends():
    conn, since, events = _generate()
    weekend_actors = {e.actor_id for e in events if e.timestamp.weekday() >= 5}
    assert weekend_actors.issubset(conn._weekend_active)


def test_events_conform_to_usage_event_schema():
    conn, since, events = _generate(days=3)
    assert len(events) > 0
    for e in events:
        assert e.quantity > 0
        assert e.unit_cost_usd > 0
        assert e.model is not None
        assert e.cost_usd == round(e.quantity * e.unit_cost_usd, 6)
