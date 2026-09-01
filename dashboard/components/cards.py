"""
Reusable KPI card component.

kpi_card(label, value, delta, delta_suffix, colour)
  Renders a metric card using st.metric — a thin wrapper that applies
  consistent formatting and optional colour overrides.
"""

from __future__ import annotations

import streamlit as st


def kpi_card(
    label: str,
    value: str | int | float,
    delta: str | None = None,
    help_text: str | None = None,
) -> None:
    """Render a single KPI metric using st.metric."""
    st.metric(label=label, value=value, delta=delta, help=help_text)


def kpi_row(metrics: list[dict]) -> None:
    """Render a row of KPI cards.

    Each dict: {"label": str, "value": ..., "delta": str|None, "help": str|None}
    """
    cols = st.columns(len(metrics))
    for col, m in zip(cols, metrics):
        with col:
            kpi_card(
                label=m["label"],
                value=m["value"],
                delta=m.get("delta"),
                help_text=m.get("help"),
            )


def section_header(title: str, subtitle: str | None = None) -> None:
    """Consistent section heading with optional subtitle."""
    st.markdown(f"### {title}")
    if subtitle:
        st.caption(subtitle)
    st.divider()
