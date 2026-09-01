"""Overview — KPI cards, tier distribution, recent alerts, at-risk pools."""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from dashboard.components.badges import esc, status_badge, tier_badge, urgency_badge
from dashboard.components.cards import kpi_row, section_header
from dashboard.data import (
    get_agents_df,
    get_alert_history_df,
    get_pools_df,
    get_spend_summary,
    get_tier_counts,
    get_workflow_summary,
)

st.title("AI Control Plane")
st.caption("Monitoring and analytics for every AI agent, model, and workload in your estate.")

# ---------------------------------------------------------------------------
# KPI row
# ---------------------------------------------------------------------------
spend = get_spend_summary(days=28)
tier_counts = get_tier_counts()
wf_summary = get_workflow_summary()

total_agents = sum(tier_counts.values())
open_items = wf_summary.get("open", 0) + wf_summary.get("reminded", 0) + wf_summary.get("escalated", 0)

pools_df = get_pools_df(days=28)
at_risk = int((pools_df["action"] == "increase").sum()) if not pools_df.empty else 0

kpi_row([
    {"label": "Total Agents", "value": total_agents, "help": "All agents discovered or registered in the estate"},
    {"label": "Open Governance Items", "value": open_items, "delta": f"{wf_summary.get('escalated', 0)} escalated", "help": "Workflow items awaiting owner action"},
    {"label": "Pools at Risk", "value": at_risk, "help": "Capacity pools past their reorder point"},
    {"label": "28-Day Spend", "value": f"${spend['total_cost_usd']:,.2f}", "help": "Total USD spend across all sources in the last 28 days"},
])

st.divider()

# ---------------------------------------------------------------------------
# Tier distribution
# ---------------------------------------------------------------------------
col_left, col_right = st.columns([1, 1])

with col_left:
    section_header("Agent Estate", "Classification status across all registered agents")

    tier_labels = {
        "tier_1": "Tier 1 — Contained",
        "tier_2": "Tier 2 — Crossing",
        "tier_3": "Tier 3 — Executing",
        "unclassified": "Unclassified",
    }
    tier_colours = {
        "tier_1": "#28a745",
        "tier_2": "#ffc107",
        "tier_3": "#dc3545",
        "unclassified": "#6c757d",
    }

    if total_agents > 0:
        # Horizontal stacked bar using st.progress-style HTML
        bar_html = '<div style="display:flex;border-radius:6px;overflow:hidden;height:28px;margin-bottom:12px">'
        for tier, count in tier_counts.items():
            if count > 0:
                pct = count / total_agents * 100
                colour = tier_colours[tier]
                bar_html += (
                    f'<div style="width:{pct:.1f}%;background:{colour};'
                    f'display:flex;align-items:center;justify-content:center;'
                    f'color:white;font-size:0.75em;font-weight:600">'
                    f'{count}</div>'
                )
        bar_html += "</div>"
        st.markdown(bar_html, unsafe_allow_html=True)

        for tier, count in tier_counts.items():
            st.markdown(
                f"{tier_badge(tier)} &nbsp; **{count}** &nbsp; {tier_labels[tier]}",
                unsafe_allow_html=True,
            )
            st.write("")
    else:
        st.info("No agents registered yet.")

with col_right:
    section_header("Governance Status", "Open workflow items by escalation stage")

    status_order = ["open", "reminded", "escalated", "capped", "resolved"]
    for status in status_order:
        count = wf_summary.get(status, 0)
        if count > 0 or status in ("open", "escalated"):
            st.markdown(
                f"{status_badge(status)} &nbsp; **{count}**",
                unsafe_allow_html=True,
            )
            st.write("")

st.divider()

# ---------------------------------------------------------------------------
# At-risk pools callout
# ---------------------------------------------------------------------------
if not pools_df.empty:
    high_urgency = pools_df[pools_df["urgency"] == "high"]
    if not high_urgency.empty:
        st.error(f"**{len(high_urgency)} capacity pool(s) require immediate action**")
        for _, row in high_urgency.iterrows():
            st.markdown(
                    f"- **{esc(row['model'])}** ({esc(row['pool_id'])}) — "
                    f"{urgency_badge(row['urgency'])} &nbsp; {esc(row['reason'])}",
                    unsafe_allow_html=True,
                )

# ---------------------------------------------------------------------------
# Recent alerts
# ---------------------------------------------------------------------------
section_header("Recent Alerts", "Last 10 dispatched webhook events")
history_df = get_alert_history_df(limit=10)
if history_df.empty:
    st.info("No alerts dispatched yet. Configure a webhook rule to start receiving notifications.")
else:
    st.dataframe(history_df, use_container_width=True, hide_index=True)
