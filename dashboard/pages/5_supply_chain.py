"""Supply Chain — capacity pools, burn rate chart, approve/dismiss recommendations."""

from __future__ import annotations

import sys
from pathlib import Path

import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from dashboard.components.badges import urgency_badge, status_badge, esc
from dashboard.components.cards import kpi_row, section_header
from dashboard.data import DB_PATH, get_burn_rate_df, get_pools_df
from storage.sqlite import SqliteBackend

st.title("Supply Chain Planner")
st.caption(
    "PTU inventory management — track burn rate and get reorder recommendations "
    "before provisioned capacity runs out. Only relevant if your org purchases "
    "model capacity ahead of time (Azure PTUs, reserved throughput)."
)

# ---------------------------------------------------------------------------
# Pool summary KPIs
# ---------------------------------------------------------------------------
pools_df = get_pools_df(days=28)

if pools_df.empty:
    st.info("No capacity pools configured. Add pools via `storage.upsert_capacity_pool()`.")
    st.stop()

at_risk = int((pools_df["urgency"] == "high").sum())
holding = int((pools_df["action"] == "hold").sum())
avg_days = pools_df["days_of_supply"].mean()

kpi_row([
    {"label": "Pools Monitored", "value": len(pools_df)},
    {"label": "Action Required", "value": at_risk, "help": "Pools past reorder point"},
    {"label": "Holding", "value": holding, "help": "Pools with adequate supply"},
    {"label": "Avg Days of Supply", "value": f"{avg_days:.1f}"},
])

st.divider()

# ---------------------------------------------------------------------------
# Pool inventory table
# ---------------------------------------------------------------------------
section_header("Pool Inventory", "Current burn rate and advisory recommendations")

# Colour urgency column
def _urgency_colour(val: str) -> str:
    return {"high": "background-color:#f8d7da", "medium": "background-color:#fff3cd", "low": ""}.get(val, "")

display_df = pools_df[[
    "pool_id", "model", "region", "ptu_quantity",
    "avg_daily_tokens", "days_of_supply", "pct_consumed",
    "action", "urgency", "rec_status",
]].copy()
display_df.columns = [
    "Pool", "Model", "Region", "PTUs",
    "Avg/day (tokens)", "Days of supply", "% consumed",
    "Action", "Urgency", "Decision",
]

st.dataframe(
    display_df.style.map(_urgency_colour, subset=["Urgency"]),
    use_container_width=True,
    hide_index=True,
)

st.divider()

# ---------------------------------------------------------------------------
# Burn rate chart — one line per model with reorder point overlay
# ---------------------------------------------------------------------------
section_header("Burn Rate — Last 28 Days", "Daily token consumption per model")

burn_df = get_burn_rate_df(days=28)
if burn_df.empty:
    st.info("No usage data in the last 28 days.")
else:
    fig = px.line(
        burn_df,
        x="date",
        y="tokens",
        color="model",
        labels={"tokens": "Tokens / day", "date": "Date", "model": "Model"},
        color_discrete_sequence=px.colors.qualitative.Set2,
    )
    fig.update_layout(
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        margin=dict(l=0, r=0, t=10, b=0),
        height=340,
    )
    st.plotly_chart(fig, use_container_width=True)

st.divider()

# ---------------------------------------------------------------------------
# Recommendations panel — Approve / Dismiss buttons
# ---------------------------------------------------------------------------
section_header("Recommendations", "Human review required — approve or dismiss each advisory")

increase_pools = pools_df[pools_df["action"] == "increase"]

if increase_pools.empty:
    st.success("All pools are within safe supply levels. No action required.")
else:
    for _, row in increase_pools.iterrows():
        with st.container(border=True):
            rec_col, btn_col = st.columns([4, 1])

            with rec_col:
                st.markdown(
                    f"**{esc(row['model'])}** — {esc(row['pool_id'])} &nbsp; "
                    f"{urgency_badge(row['urgency'])}",
                    unsafe_allow_html=True,
                )
                st.caption(row["reason"])
                current_decision = row["rec_status"]
                if current_decision != "pending":
                    st.markdown(
                        f"Decision: {status_badge(current_decision)}",
                        unsafe_allow_html=True,
                    )

            with btn_col:
                if row["rec_status"] == "pending":
                    if st.button("✓ Approve", key=f"approve_{row['pool_id']}", type="primary"):
                        db = SqliteBackend(db_path=DB_PATH)
                        db.upsert_recommendation_action(
                            pool_id=row["pool_id"],
                            action=row["action"],
                            status="approved",
                            acted_by="dashboard",
                        )
                        st.cache_data.clear()
                        st.rerun()

                    if st.button("✗ Dismiss", key=f"dismiss_{row['pool_id']}"):
                        db = SqliteBackend(db_path=DB_PATH)
                        db.upsert_recommendation_action(
                            pool_id=row["pool_id"],
                            action=row["action"],
                            status="dismissed",
                            acted_by="dashboard",
                        )
                        st.cache_data.clear()
                        st.rerun()
                elif row["rec_status"] == "approved":
                    if st.button("↩ Undo", key=f"undo_{row['pool_id']}"):
                        db = SqliteBackend(db_path=DB_PATH)
                        db.upsert_recommendation_action(
                            pool_id=row["pool_id"],
                            action=row["action"],
                            status="pending",
                        )
                        st.cache_data.clear()
                        st.rerun()
