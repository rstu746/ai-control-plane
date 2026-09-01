"""Governance — workflow items board with live Resolve button."""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from dashboard.components.badges import status_badge, tier_badge
from dashboard.components.cards import kpi_row, section_header
from dashboard.data import DB_PATH, get_workflow_items_df, get_workflow_summary
from core.models import RegulatoryFlag
from storage.sqlite import SqliteBackend

st.set_page_config(page_title="Governance — AI Control Plane", layout="wide")
st.title("Governance")
st.caption("Classification workflow items, escalation tracking, and regulatory flag overview.")

# ---------------------------------------------------------------------------
# Summary KPIs
# ---------------------------------------------------------------------------
wf_summary = get_workflow_summary()
kpi_row([
    {"label": "Open", "value": wf_summary.get("open", 0),
     "help": "Awaiting owner action"},
    {"label": "Reminded", "value": wf_summary.get("reminded", 0),
     "help": "At least one reminder sent"},
    {"label": "Escalated", "value": wf_summary.get("escalated", 0),
     "help": "Manager / platform owner notified (Day +14)"},
    {"label": "Capped", "value": wf_summary.get("capped", 0),
     "help": "Token cap applied (Day +21) — pending resolution"},
    {"label": "Resolved", "value": wf_summary.get("resolved", 0),
     "help": "Resolved by owner"},
])

st.divider()

# ---------------------------------------------------------------------------
# Workflow board — columns per status
# ---------------------------------------------------------------------------
section_header("Workflow Items", "Resolve a classification request or holistic review to lift the token cap")

all_items_df = get_workflow_items_df()

active_statuses = ["open", "reminded", "escalated", "capped"]
board_cols = st.columns(len(active_statuses))

for col, status in zip(board_cols, active_statuses):
    with col:
        status_items = all_items_df[all_items_df["status"] == status] if not all_items_df.empty else all_items_df
        st.markdown(
            f"{status_badge(status)} &nbsp; **{len(status_items)}**",
            unsafe_allow_html=True,
        )
        st.write("")

        if status_items.empty:
            st.caption("None")
            continue

        for _, row in status_items.iterrows():
            with st.container(border=True):
                st.markdown(
                    f"**{row['agent_name']}** &nbsp; {tier_badge(row['tier'])}",
                    unsafe_allow_html=True,
                )
                st.caption(f"Type: {row['item_type'].replace('_', ' ').title()}")
                st.caption(f"Open {row['days_open']} days · cap in {row['days_until_cap']} days")

                if row["missing_fields"] and row["missing_fields"] != "—":
                    st.caption(f"Missing: {row['missing_fields']}")

                if row["notes"] and row["notes"] != "—":
                    with st.expander("Notes"):
                        st.write(row["notes"])

                # Live Resolve button
                if st.button(
                    "✓ Resolve",
                    key=f"resolve_{row['item_id']}",
                    type="primary",
                    use_container_width=True,
                ):
                    from core.workflow import WorkflowEngine
                    db = SqliteBackend(db_path=DB_PATH)
                    engine = WorkflowEngine(storage=db)
                    try:
                        engine.resolve(
                            row["item_id"],
                            resolved_by="dashboard",
                            notes="Resolved via dashboard",
                        )
                        st.cache_data.clear()
                        st.success(f"Resolved: {row['agent_name']}")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Failed to resolve: {exc}")

st.divider()

# ---------------------------------------------------------------------------
# Regulatory flag heatmap (count by flag + tier)
# ---------------------------------------------------------------------------
section_header("Regulatory Flags", "Agent count by flag and tier")

from dashboard.data import get_agents_df
agents_df = get_agents_df()

if agents_df.empty:
    st.info("No agent data available.")
else:
    flag_values = [f.value for f in RegulatoryFlag]
    tier_values = ["tier_1", "tier_2", "tier_3"]

    heatmap_data = {tier: {} for tier in tier_values}
    for _, row in agents_df.iterrows():
        tier = row["tier"]
        if tier not in tier_values:
            continue
        for flag in flag_values:
            if flag in (row["regulatory_flags"] or ""):
                heatmap_data[tier][flag] = heatmap_data[tier].get(flag, 0) + 1

    import plotly.graph_objects as go
    z_vals = [[heatmap_data[tier].get(flag, 0) for flag in flag_values] for tier in tier_values]

    fig = go.Figure(data=go.Heatmap(
        z=z_vals,
        x=[f.replace("_", " ").title() for f in flag_values],
        y=["Tier 1", "Tier 2", "Tier 3"],
        colorscale="Reds",
        text=z_vals,
        texttemplate="%{text}",
        showscale=False,
    ))
    fig.update_layout(
        margin=dict(l=0, r=0, t=10, b=0),
        height=200,
    )
    st.plotly_chart(fig, use_container_width=True)
