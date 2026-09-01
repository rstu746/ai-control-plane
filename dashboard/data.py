"""
Dashboard data layer — cached queries over SqliteBackend.

All functions are decorated with @st.cache_data(ttl=60) so the dashboard
does not hit storage on every Streamlit interaction. Cache is invalidated
explicitly after write actions (resolve, approve/dismiss) via
st.cache_data.clear().

Each function converts dataclasses to plain dicts or DataFrames so
Streamlit's st.dataframe can display them directly.

DB path is read from the CONTROL_PLANE_DB environment variable. The default
is ~/.local/state/ai-control-plane/dashboard.db (XDG-style user state
directory), which is owner-readable only and survives reboots unlike /tmp.
The directory is created on import if it does not exist.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st

# Allow imports from repo root regardless of working directory
_REPO_ROOT = Path(__file__).parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from core.burn_rate import summarize_burn_rate
from core.models import AgentStatus, AgentTier, DemandDriver, SourceApp, WorkflowItemStatus
from core.recommender import recommend_for_pool
from storage.sqlite import SqliteBackend

def _default_db_path() -> str:
    """Return a user-owned, persistent default DB path.

    Preference order:
      1. CONTROL_PLANE_DB environment variable (always wins)
      2. ~/.local/state/ai-control-plane/dashboard.db  (XDG user state dir)

    The directory is created with mode 0o700 (owner-only) if it does not
    exist so the database file is not world-readable on a shared host.
    """
    env = os.environ.get("CONTROL_PLANE_DB")
    if env:
        return env
    state_dir = Path.home() / ".local" / "state" / "ai-control-plane"
    state_dir.mkdir(parents=True, exist_ok=True)
    # Ensure the directory itself is not world-accessible
    try:
        state_dir.chmod(0o700)
    except OSError:
        pass  # best-effort; may fail if already correct or on some filesystems
    return str(state_dir / "dashboard.db")


DB_PATH = _default_db_path()
PTU_LEAD_TIME_DAYS = 5


def get_db() -> SqliteBackend:
    """Return a SqliteBackend pointed at the configured DB path."""
    return SqliteBackend(db_path=DB_PATH)


# ---------------------------------------------------------------------------
# Base event fetch — single cached scan shared by all derived views.
#
# get_spend_summary, get_daily_tokens_df, get_spend_by_source_df, and
# get_demand_driver_df previously each called db.get_usage_events() independently,
# performing four full table scans and holding four copies in memory per render.
# All derived functions now call _get_raw_events_df() which is cached once per
# (days, ttl) combination, so subsequent calls within the same 60-second window
# are served from the Streamlit cache at zero DB cost.
# ---------------------------------------------------------------------------

@st.cache_data(ttl=60)
def _get_raw_events_df(days: int = 28) -> pd.DataFrame:
    """Fetch all usage events for the window and return a flat DataFrame.
    This is the single source of truth for all spend/token derived views."""
    db = get_db()
    since = datetime.now() - timedelta(days=days)
    events = db.get_usage_events(since=since)
    if not events:
        return pd.DataFrame()
    return pd.DataFrame([
        {
            "timestamp": e.timestamp,
            "date": e.timestamp.date(),
            "actor_id": e.actor_id,
            "team_id": e.team_id,
            "source_app": e.source_app.value,
            "model": e.model or "unknown",
            "quantity": e.quantity,
            "cost_usd": e.cost_usd,
            "agent_id": e.agent_id,
            "demand_driver": e.metadata.get("demand_driver", "human_driven"),
        }
        for e in events
    ])


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------

@st.cache_data(ttl=60)
def get_agents_df(
    tier: str | None = None,
    status: str | None = None,
    platform: str | None = None,
    team_id: str | None = None,
) -> pd.DataFrame:
    db = get_db()
    agents = db.get_agents(
        tier=AgentTier(tier) if tier else None,
        status=AgentStatus(status).value if status else None,
        source_platform=SourceApp(platform) if platform else None,
        team_id=team_id,
    )
    if not agents:
        return pd.DataFrame()

    rows = []
    for a in agents:
        rows.append({
            "agent_id": a.agent_id,
            "name": a.name,
            "tier": a.tier.value,
            "status": a.status.value,
            "platform": a.source_platform.value,
            "functional_role": a.functional_role.value,
            "control_flow_type": a.control_flow_type.value if a.control_flow_type else "—",
            "team_id": a.team_id,
            "owner_id": a.owner_id,
            "regulatory_flags": ", ".join(f.value for f in a.regulatory_flags) or "none",
            "last_seen_at": a.last_seen_at.strftime("%Y-%m-%d") if a.last_seen_at else "never",
            "last_classified_at": a.last_classified_at.strftime("%Y-%m-%d") if a.last_classified_at else "—",
            "monthly_token_cap": a.monthly_token_cap,
        })
    return pd.DataFrame(rows)


@st.cache_data(ttl=60)
def get_agent_detail(agent_id: str) -> dict:
    db = get_db()
    agent = db.get_agent(agent_id)
    if not agent:
        return {}

    manifest = db.get_agent_manifest(agent_id)
    classification_history = db.get_classification_results(agent_id=agent_id)[:3]
    workflow_items = db.get_workflow_items(agent_id=agent_id)

    return {
        "agent": agent,
        "manifest": manifest,
        "classification_history": classification_history,
        "workflow_items": workflow_items,
    }


@st.cache_data(ttl=60)
def get_tier_counts() -> dict[str, int]:
    db = get_db()
    agents = db.get_agents()
    counts = {"tier_1": 0, "tier_2": 0, "tier_3": 0, "unclassified": 0}
    for a in agents:
        counts[a.tier.value] = counts.get(a.tier.value, 0) + 1
    return counts


@st.cache_data(ttl=60)
def get_dormant_agents_df(days: int = 30) -> pd.DataFrame:
    db = get_db()
    cutoff = datetime.now() - timedelta(days=days)
    agents = db.get_agents()
    rows = []
    for a in agents:
        if a.last_seen_at and a.last_seen_at < cutoff:
            days_dormant = (datetime.now() - a.last_seen_at).days
            rows.append({
                "name": a.name,
                "tier": a.tier.value,
                "team_id": a.team_id,
                "last_seen": a.last_seen_at.strftime("%Y-%m-%d"),
                "days_dormant": days_dormant,
            })
        elif a.last_seen_at is None:
            rows.append({
                "name": a.name,
                "tier": a.tier.value,
                "team_id": a.team_id,
                "last_seen": "never",
                "days_dormant": None,
            })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Workflow items
# ---------------------------------------------------------------------------

@st.cache_data(ttl=30)
def get_workflow_items_df(status: str | None = None) -> pd.DataFrame:
    db = get_db()
    items = db.get_workflow_items(
        status=WorkflowItemStatus(status) if status else None
    )
    if not items:
        return pd.DataFrame()

    # Pre-fetch all agents in one query to avoid an N+1 db.get_agent() per item
    all_agents = {a.agent_id: a for a in db.get_agents()}

    rows = []
    for item in items:
        agent = all_agents.get(item.agent_id)
        age_days = (datetime.now() - item.raised_at).days
        days_until_cap = max(0, 21 - age_days)
        rows.append({
            "item_id": item.item_id,
            "agent_name": agent.name if agent else item.agent_id,
            "agent_id": item.agent_id,
            "tier": agent.tier.value if agent else "—",
            "item_type": item.item_type.value,
            "status": item.status.value,
            "days_open": age_days,
            "days_until_cap": days_until_cap,
            "due_at": item.due_at.strftime("%Y-%m-%d"),
            "missing_fields": ", ".join(item.missing_fields) if item.missing_fields else "—",
            "notes": item.notes,
        })
    return pd.DataFrame(rows)


@st.cache_data(ttl=30)
def get_workflow_summary() -> dict[str, int]:
    db = get_db()
    summary = {}
    for status in WorkflowItemStatus:
        summary[status.value] = len(db.get_workflow_items(status=status))
    return summary


# ---------------------------------------------------------------------------
# Usage events and spend
# ---------------------------------------------------------------------------

@st.cache_data(ttl=60)
def get_spend_summary(days: int = 28) -> dict:
    df = _get_raw_events_df(days=days)
    if df.empty:
        return {"total_cost_usd": 0.0, "total_tokens": 0.0, "event_count": 0, "by_source": {}}
    return {
        "total_cost_usd": float(df["cost_usd"].sum()),
        "total_tokens": float(df["quantity"].sum()),
        "event_count": len(df),
        "by_source": df.groupby("source_app")["cost_usd"].sum().to_dict(),
    }


@st.cache_data(ttl=60)
def get_daily_tokens_df(days: int = 28) -> pd.DataFrame:
    """Daily token usage per model — used for the burn rate and adoption charts."""
    df = _get_raw_events_df(days=days)
    if df.empty:
        return pd.DataFrame()
    return df.groupby(["date", "model"], as_index=False).agg(
        tokens=("quantity", "sum"),
        cost_usd=("cost_usd", "sum"),
    )


@st.cache_data(ttl=60)
def get_spend_by_source_df(days: int = 28) -> pd.DataFrame:
    df = _get_raw_events_df(days=days)
    if df.empty:
        return pd.DataFrame()
    return (
        df.groupby("source_app", as_index=False)
        .agg(cost_usd=("cost_usd", "sum"))
        .sort_values("cost_usd", ascending=True)
    )


@st.cache_data(ttl=60)
def get_demand_driver_df(days: int = 28) -> pd.DataFrame:
    df = _get_raw_events_df(days=days)
    if df.empty:
        return pd.DataFrame()
    grouped = df.groupby("demand_driver", as_index=False).agg(tokens=("quantity", "sum"))
    grouped.columns = ["driver", "tokens"]
    return grouped


# ---------------------------------------------------------------------------
# Trends snapshots (week-over-week)
# ---------------------------------------------------------------------------

@st.cache_data(ttl=60)
def get_wow_table_df(days: int = 28) -> pd.DataFrame:
    db = get_db()
    since = datetime.now() - timedelta(days=days)
    snaps = db.get_trend_snapshots(since=since)
    # Filter to per-model, non-agent, non-team aggregates
    model_snaps = [s for s in snaps if s.model and s.agent_id is None and s.team_id is None]
    if not model_snaps:
        return pd.DataFrame()

    # Sort descending by date so the first snapshot seen per model is the most recent.
    # This ensures week_over_week_delta comes from the latest snapshot, not an arbitrary one.
    model_snaps.sort(key=lambda s: s.snapshot_date, reverse=True)

    by_model: dict[str, dict] = {}
    for s in model_snaps:
        m = s.model
        if m not in by_model:
            by_model[m] = {"total_tokens": 0.0, "week_over_week_delta": None}
        by_model[m]["total_tokens"] += s.total_tokens
        # Only take the delta from the most recent snapshot (first seen due to sort)
        if by_model[m]["week_over_week_delta"] is None and s.week_over_week_delta is not None:
            by_model[m]["week_over_week_delta"] = s.week_over_week_delta

    rows = [{"model": k, **v} for k, v in by_model.items()]
    return pd.DataFrame(rows).sort_values("total_tokens", ascending=False)


# ---------------------------------------------------------------------------
# Supply chain / capacity pools
# ---------------------------------------------------------------------------

@st.cache_data(ttl=60)
def get_pools_df(days: int = 28) -> pd.DataFrame:
    db = get_db()
    pools = db.get_capacity_pools()
    if not pools:
        return pd.DataFrame()

    # Reuse the already-cached raw events DataFrame rather than hitting the DB again.
    # summarize_burn_rate expects list[UsageEvent]; reconstruct lightweight objects
    # from the DataFrame so we avoid a redundant full table scan.
    raw_df = _get_raw_events_df(days=days)
    if raw_df.empty:
        events_for_pools = []
    else:
        from core.models import ResourceType, UsageEvent as _UE, SourceApp as _SA
        events_for_pools = [
            _UE(
                timestamp=row["timestamp"],
                actor_id=row["actor_id"],
                team_id=row["team_id"],
                source_app=_SA(row["source_app"]),
                resource_type=ResourceType.TOKENS,
                quantity=row["quantity"],
                unit_cost_usd=row["cost_usd"] / row["quantity"] if row["quantity"] else 0.0,
                model=row["model"],
            )
            for _, row in raw_df.iterrows()
        ]

    rows = []
    for pool in pools:
        summary = summarize_burn_rate(pool, events_for_pools, window_days=14)
        rec = recommend_for_pool(pool, summary, lead_time_days=PTU_LEAD_TIME_DAYS)
        rec_action = db.get_recommendation_action(pool.pool_id)
        pct_consumed = (
            round(summary.total_consumed_tokens / summary.pool_capacity_tokens * 100, 1)
            if summary.pool_capacity_tokens > 0 else 0
        )
        rows.append({
            "pool_id": pool.pool_id,
            "model": pool.model,
            "region": pool.region,
            "ptu_quantity": pool.ptu_quantity,
            "avg_daily_tokens": int(summary.avg_daily_tokens),
            "days_of_supply": round(summary.days_of_supply_remaining, 1),
            "pct_consumed": pct_consumed,
            "action": rec.action,
            "urgency": rec.urgency,
            "reason": rec.reason,
            "rec_status": rec_action["status"] if rec_action else "pending",
            "status": pool.status.value,
        })
    return pd.DataFrame(rows)


@st.cache_data(ttl=60)
def get_burn_rate_df(days: int = 28) -> pd.DataFrame:
    """Daily token usage per model for burn rate chart."""
    return get_daily_tokens_df(days=days)


# ---------------------------------------------------------------------------
# Alert history
# ---------------------------------------------------------------------------

@st.cache_data(ttl=30)
def get_alert_history_df(limit: int = 10) -> pd.DataFrame:
    db = get_db()
    history = db.get_alert_history()
    if not history:
        return pd.DataFrame()
    rows = [
        {
            "event_type": h.event_type.value,
            "severity": "—",
            "dispatched_at": h.dispatched_at.strftime("%Y-%m-%d %H:%M"),
            "success": "✓" if h.success else "✗",
            "http_status": h.http_status or "—",
        }
        for h in sorted(history, key=lambda x: x.dispatched_at, reverse=True)[:limit]
    ]
    return pd.DataFrame(rows)
