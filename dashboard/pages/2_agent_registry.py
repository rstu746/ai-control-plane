"""Agent Registry — filterable table with detail panel."""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from dashboard.components.badges import (
    autonomy_badge,
    flag_badges,
    status_badge,
    tier_badge,
    esc,
)
from dashboard.components.cards import section_header
from dashboard.data import get_agent_detail, get_agents_df, get_tier_counts
from core.models import AgentStatus, AgentTier, SourceApp
from core.risk import assess_risk

st.title("Agent Registry")
st.caption("Every AI agent discovered or registered in your estate, classified and tracked.")

# ---------------------------------------------------------------------------
# Sidebar filters — options built from enum members so any mismatch is
# caught at startup rather than raising ValueError on filter selection
# ---------------------------------------------------------------------------
_ALL = "All"

_tier_options = [_ALL] + [e.value for e in AgentTier]
_tier_labels = {
    _ALL: "All tiers",
    AgentTier.TIER_1.value: "Tier 1 — Contained",
    AgentTier.TIER_2.value: "Tier 2 — Crossing",
    AgentTier.TIER_3.value: "Tier 3 — Executing",
    AgentTier.UNCLASSIFIED.value: "Unclassified",
}

_status_options = [_ALL] + [e.value for e in AgentStatus]
_platform_options = [_ALL] + [e.value for e in SourceApp]

with st.sidebar:
    st.header("Filters")
    tier_filter = st.selectbox(
        "Tier",
        options=_tier_options,
        format_func=lambda x: _tier_labels.get(x, x),
    )
    status_filter = st.selectbox(
        "Status",
        options=_status_options,
    )
    platform_filter = st.selectbox(
        "Platform",
        options=_platform_options,
    )

# ---------------------------------------------------------------------------
# Summary row
# ---------------------------------------------------------------------------
counts = get_tier_counts()
tier_cols = st.columns(4)
with tier_cols[0]:
    st.markdown(f"{tier_badge('tier_1')} **{counts.get('tier_1', 0)}**", unsafe_allow_html=True)
with tier_cols[1]:
    st.markdown(f"{tier_badge('tier_2')} **{counts.get('tier_2', 0)}**", unsafe_allow_html=True)
with tier_cols[2]:
    st.markdown(f"{tier_badge('tier_3')} **{counts.get('tier_3', 0)}**", unsafe_allow_html=True)
with tier_cols[3]:
    st.markdown(f"{tier_badge('unclassified')} **{counts.get('unclassified', 0)}**", unsafe_allow_html=True)

st.divider()

# ---------------------------------------------------------------------------
# Agent table
# ---------------------------------------------------------------------------
df = get_agents_df(
    tier=tier_filter if tier_filter != "All" else None,
    status=status_filter if status_filter != "All" else None,
    platform=platform_filter if platform_filter != "All" else None,
)

unclassified_count = counts.get("unclassified", 0)
if unclassified_count > 0:
    st.warning(
        f"**{unclassified_count} agent(s) are unclassified** — manifests are incomplete. "
        "Go to **Governance** to view and resolve the open classification requests.",
        icon="⚠️",
    )

if df.empty:
    st.info("No agents match the current filters.")
    st.stop()

# Display columns (hide raw IDs)
display_cols = ["name", "tier", "platform", "functional_role",
                "status", "regulatory_flags", "last_seen_at", "team_id"]
display_df = df[display_cols].copy()

section_header(f"Agents ({len(df)})", "Click a row to view agent details")

# Render table — badges via st.dataframe column config
event = st.dataframe(
    display_df,
    use_container_width=True,
    hide_index=True,
    on_select="rerun",
    selection_mode="single-row",
    column_config={
        "name": st.column_config.TextColumn("Agent", width="medium"),
        "tier": st.column_config.TextColumn("Tier"),
        "platform": st.column_config.TextColumn("Platform"),
        "functional_role": st.column_config.TextColumn("Role"),
        "status": st.column_config.TextColumn("Status"),
        "regulatory_flags": st.column_config.TextColumn("Regulatory Flags", width="large"),
        "last_seen_at": st.column_config.TextColumn("Last Seen"),
        "team_id": st.column_config.TextColumn("Team"),
    },
)

# ---------------------------------------------------------------------------
# Agent detail panel
# ---------------------------------------------------------------------------
selected_rows = event.selection.rows if hasattr(event, "selection") else []
if selected_rows:
    selected_idx = selected_rows[0]
    agent_id = df.iloc[selected_idx]["agent_id"]
    detail = get_agent_detail(agent_id)

    if detail:
        agent = detail["agent"]
        manifest = detail["manifest"]
        history = detail["classification_history"]
        wf_items = detail["workflow_items"]

        st.divider()
        st.subheader(f"Detail — {agent.name}")

        d_col1, d_col2, d_col3 = st.columns(3)

        with d_col1:
            st.markdown("**Classification**")
            st.markdown(tier_badge(agent.tier.value), unsafe_allow_html=True)
            st.caption(f"Role: {agent.functional_role.value}")
            st.caption(f"Control flow: {agent.control_flow_type.value if agent.control_flow_type else '—'}")
            st.caption(f"Classified: {agent.last_classified_at.strftime('%Y-%m-%d') if agent.last_classified_at else 'never'}")

        with d_col2:
            st.markdown("**Status & Ownership**")
            st.markdown(status_badge(agent.status.value), unsafe_allow_html=True)
            st.caption(f"Owner: {agent.owner_id or '—'}")
            st.caption(f"Team: {agent.team_id or '—'}")
            st.caption(f"Platform: {agent.source_platform.value}")
            st.caption(f"Discovered via: {agent.discovery_source.value}")

        with d_col3:
            st.markdown("**Regulatory Flags**")
            flags_str = ", ".join(f.value for f in agent.regulatory_flags) or "none"
            st.markdown(flag_badges(flags_str), unsafe_allow_html=True)
            if agent.monthly_token_cap:
                st.caption(f"Token cap: ${agent.monthly_token_cap:.2f}/month (enforcement active)")

        # Manifest completeness
        if manifest:
            st.markdown("**Manifest completeness**")
            completeness = manifest.completeness
            st.progress(completeness, text=f"{completeness:.0%} of capability flags declared")
            missing = manifest.missing_capability_flags
            if missing:
                st.caption(f"Missing: {', '.join(missing)}")

            with st.expander("View full manifest"):
                manifest_dict = {
                    "tool_list": manifest.tool_list,
                    "mcp_servers": manifest.mcp_servers,
                    "data_sources": manifest.data_sources,
                    "sub_agent_ids": manifest.sub_agent_ids,
                    "data_scope": manifest.data_scope,
                    "execution_rights": manifest.execution_rights,
                    "credentials_held": manifest.credentials_held,
                    "can_modify_repos_or_pipelines": manifest.can_modify_repos_or_pipelines,
                    "can_modify_other_agents": manifest.can_modify_other_agents,
                    "writes_to_system_of_record": manifest.writes_to_system_of_record,
                    "reaches_external_party": manifest.reaches_external_party,
                    "distributes_output_autonomously": manifest.distributes_output_autonomously,
                    "grounding_includes_confidential_of_others": manifest.grounding_includes_confidential_of_others,
                }
                st.json(manifest_dict)

            # Risk assessment
            if agent.tier.value != "unclassified":
                risk = assess_risk(agent, manifest)
                st.markdown("**Risk assessment**")
                st.markdown(
                    f"Autonomy control: {autonomy_badge(risk.autonomy_control.value)} &nbsp; "
                    f"Blast radius: **{esc(risk.blast_radius)}** &nbsp; "
                    f"Reversibility: **{esc(risk.reversibility)}**",
                    unsafe_allow_html=True,
                )
                if risk.notes:
                    st.caption(risk.notes)

        # Classification history
        if history:
            with st.expander(f"Classification history ({len(history)} results)"):
                for r in history:
                    st.markdown(
                        f"- {r.classified_at.strftime('%Y-%m-%d')} — "
                        f"{tier_badge(r.tier.value)} &nbsp; "
                        f"confidence {r.confidence:.0%} &nbsp; by `{esc(r.classified_by)}`",
                        unsafe_allow_html=True,
                    )

        # Open workflow items for this agent
        if wf_items:
            open_wf = [i for i in wf_items if i.status.value not in ("resolved",)]
            if open_wf:
                st.markdown(f"**Open workflow items ({len(open_wf)})**")
                for item in open_wf:
                    age = (item.resolved_at or __import__("datetime").datetime.now()) - item.raised_at
                    st.markdown(
                        f"- {item.item_type.value.replace('_', ' ').title()} — "
                        f"status: {item.status.value} — "
                        f"{age.days} days open — due {item.due_at.strftime('%Y-%m-%d')}",
                    )
