"""Analytics & Trends — model adoption, spend by source, demand driver split, WoW table."""

from __future__ import annotations

import sys
from pathlib import Path

import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from dashboard.components.cards import kpi_row, section_header
from dashboard.data import (
    get_daily_tokens_df,
    get_demand_driver_df,
    get_dormant_agents_df,
    get_spend_by_source_df,
    get_spend_summary,
    get_wow_table_df,
)

st.set_page_config(page_title="Analytics — AI Control Plane", layout="wide")
st.title("Analytics & Trends")
st.caption("Usage patterns, model adoption, spend attribution, and dormancy across your AI estate.")

# Sidebar controls
with st.sidebar:
    st.header("Time window")
    days = st.slider("Days", min_value=7, max_value=90, value=28, step=7)
    dormancy_days = st.slider("Dormancy threshold (days)", min_value=7, max_value=90, value=30, step=7)

# ---------------------------------------------------------------------------
# KPI row
# ---------------------------------------------------------------------------
spend = get_spend_summary(days=days)
kpi_row([
    {"label": f"{days}-Day Spend", "value": f"${spend['total_cost_usd']:,.2f}"},
    {"label": f"{days}-Day Tokens", "value": f"{spend['total_tokens']:,.0f}"},
    {"label": "Usage Events", "value": f"{spend['event_count']:,}"},
    {"label": "Sources Active", "value": len(spend["by_source"])},
])

st.divider()

# ---------------------------------------------------------------------------
# Model adoption — stacked area chart
# ---------------------------------------------------------------------------
section_header("Model Adoption", f"Daily token consumption per model — last {days} days")

daily_df = get_daily_tokens_df(days=days)
if daily_df.empty:
    st.info("No usage data available for the selected window.")
else:
    fig = px.area(
        daily_df,
        x="date",
        y="tokens",
        color="model",
        title=None,
        labels={"tokens": "Tokens", "date": "Date", "model": "Model"},
        color_discrete_sequence=px.colors.qualitative.Set2,
    )
    fig.update_layout(
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        margin=dict(l=0, r=0, t=10, b=0),
        height=320,
    )
    st.plotly_chart(fig, use_container_width=True)

st.divider()

# ---------------------------------------------------------------------------
# Spend by source + demand driver split — side by side
# ---------------------------------------------------------------------------
left_col, right_col = st.columns(2)

with left_col:
    section_header("Spend by Source", f"USD — last {days} days")
    source_df = get_spend_by_source_df(days=days)
    if source_df.empty:
        st.info("No spend data available.")
    else:
        fig2 = px.bar(
            source_df,
            x="cost_usd",
            y="source_app",
            orientation="h",
            labels={"cost_usd": "Cost (USD)", "source_app": "Source"},
            color="cost_usd",
            color_continuous_scale="Blues",
        )
        fig2.update_layout(
            showlegend=False,
            coloraxis_showscale=False,
            margin=dict(l=0, r=0, t=10, b=0),
            height=280,
        )
        st.plotly_chart(fig2, use_container_width=True)

with right_col:
    section_header("Human vs Agent Demand", f"Token split by demand driver — last {days} days")
    driver_df = get_demand_driver_df(days=days)
    if driver_df.empty or driver_df["tokens"].sum() == 0:
        st.info("No demand driver data available.")
    else:
        fig3 = px.pie(
            driver_df,
            values="tokens",
            names="driver",
            hole=0.5,
            color_discrete_map={
                "human_driven": "#4e79a7",
                "agent_driven": "#f28e2b",
            },
        )
        fig3.update_layout(
            showlegend=True,
            margin=dict(l=0, r=0, t=10, b=0),
            height=280,
        )
        st.plotly_chart(fig3, use_container_width=True)

st.divider()

# ---------------------------------------------------------------------------
# Week-over-week table
# ---------------------------------------------------------------------------
section_header("Week-over-Week Model Trends")

wow_df = get_wow_table_df(days=days)
if wow_df.empty:
    st.info("No trend snapshot data available. Trend snapshots are computed daily.")
else:
    def _colour_delta(val):
        if val is None:
            return ""
        if val > 0.1:
            return "color: #28a745; font-weight: 600"
        if val < -0.1:
            return "color: #dc3545; font-weight: 600"
        return "color: #6c757d"

    display_df = wow_df.copy()
    display_df["total_tokens"] = display_df["total_tokens"].apply(lambda x: f"{x:,.0f}")
    display_df["week_over_week_delta"] = display_df["week_over_week_delta"].apply(
        lambda x: f"{x:+.1%}" if x is not None else "—"
    )
    display_df.columns = ["Model", "Tokens (period)", "Week-over-Week"]
    st.dataframe(display_df, use_container_width=True, hide_index=True)

st.divider()

# ---------------------------------------------------------------------------
# Dormant agents
# ---------------------------------------------------------------------------
section_header(
    "Dormant Agents",
    f"Agents with no usage events in the last {dormancy_days} days",
)

dormant_df = get_dormant_agents_df(days=dormancy_days)
if dormant_df.empty:
    st.success(f"No dormant agents detected (threshold: {dormancy_days} days).")
else:
    st.warning(f"**{len(dormant_df)} agent(s) inactive** for more than {dormancy_days} days.")
    st.dataframe(
        dormant_df,
        use_container_width=True,
        hide_index=True,
        column_config={
            "name": "Agent",
            "tier": "Tier",
            "team_id": "Team",
            "last_seen": "Last Seen",
            "days_dormant": st.column_config.NumberColumn("Days Dormant", format="%d"),
        },
    )
