"""
Reusable badge components — coloured HTML spans for tier, severity,
autonomy control, and status values.

All functions return an HTML string suitable for st.markdown(..., unsafe_allow_html=True).

Security: every value interpolated into HTML is passed through html.escape() so
that database-sourced strings (agent names, model names, pool IDs, reasons)
cannot inject markup. Enum-derived labels (tier_badge, status_badge, etc.) are
also escaped for defence-in-depth, even though those values are controlled.
"""

from __future__ import annotations

import html as _html

_TIER_COLOURS = {
    "tier_1": ("🟢", "#1a7a4a", "#d4edda"),
    "tier_2": ("🟡", "#856404", "#fff3cd"),
    "tier_3": ("🔴", "#721c24", "#f8d7da"),
    "unclassified": ("⚪", "#495057", "#e9ecef"),
}

_URGENCY_COLOURS = {
    "low":    ("#155724", "#d4edda"),
    "medium": ("#856404", "#fff3cd"),
    "high":   ("#721c24", "#f8d7da"),
}

_STATUS_COLOURS = {
    "open":        ("#004085", "#cce5ff"),
    "reminded":    ("#856404", "#fff3cd"),
    "escalated":   ("#721c24", "#f8d7da"),
    "capped":      ("#491217", "#f5c6cb"),
    "resolved":    ("#155724", "#d4edda"),
    "active":      ("#155724", "#d4edda"),
    "dormant":     ("#495057", "#e9ecef"),
    "discovered":  ("#004085", "#cce5ff"),
    "unclassified":("#495057", "#e9ecef"),
    "pending_review": ("#856404", "#fff3cd"),
    "pending":     ("#856404", "#fff3cd"),
    "approved":    ("#155724", "#d4edda"),
    "dismissed":   ("#495057", "#e9ecef"),
}

_AUTONOMY_COLOURS = {
    "let_run":      ("#155724", "#d4edda"),
    "detect_fast":  ("#856404", "#fff3cd"),
    "rate_limit":   ("#7d4e00", "#ffe5b4"),
    "human_gate":   ("#721c24", "#f8d7da"),
}


def _badge(text: str, fg: str, bg: str) -> str:
    """Render a coloured pill badge. `text` is HTML-escaped before insertion."""
    safe_text = _html.escape(text)
    return (
        f'<span style="background:{bg};color:{fg};padding:2px 8px;'
        f'border-radius:10px;font-size:0.8em;font-weight:600;'
        f'white-space:nowrap">{safe_text}</span>'
    )


def esc(value: str) -> str:
    """Escape a database-sourced string for safe embedding in an HTML context.

    Use this whenever interpolating agent names, model names, pool IDs, reasons,
    or any other value that originates from external data into an
    st.markdown(..., unsafe_allow_html=True) call.

    Example:
        st.markdown(f"**{esc(agent.name)}** — {tier_badge(agent.tier.value)}",
                    unsafe_allow_html=True)
    """
    return _html.escape(str(value))


def tier_badge(tier: str) -> str:
    icon, fg, bg = _TIER_COLOURS.get(tier, ("⚪", "#495057", "#e9ecef"))
    label = tier.replace("_", " ").title()
    return _badge(f"{icon} {label}", fg, bg)


def urgency_badge(urgency: str) -> str:
    fg, bg = _URGENCY_COLOURS.get(urgency, ("#495057", "#e9ecef"))
    return _badge(urgency.upper(), fg, bg)


def status_badge(status: str) -> str:
    fg, bg = _STATUS_COLOURS.get(status, ("#495057", "#e9ecef"))
    return _badge(status.replace("_", " ").title(), fg, bg)


def autonomy_badge(control: str) -> str:
    fg, bg = _AUTONOMY_COLOURS.get(control, ("#495057", "#e9ecef"))
    label = control.replace("_", " ").title()
    return _badge(label, fg, bg)


def flag_badges(flags_str: str) -> str:
    """Render a comma-separated flags string as individual badges."""
    if not flags_str or flags_str == "none":
        return '<span style="color:#6c757d;font-size:0.85em">none</span>'
    flags = [f.strip() for f in flags_str.split(",")]
    colours = {
        "personal_data": ("#004085", "#cce5ff"),
        "external_facing": ("#7d4e00", "#ffe5b4"),
        "financially_material": ("#856404", "#fff3cd"),
        "market_facing": ("#721c24", "#f8d7da"),
    }
    parts = []
    for flag in flags:
        fg, bg = colours.get(flag, ("#495057", "#e9ecef"))
        parts.append(_badge(flag.replace("_", " "), fg, bg))
    return " ".join(parts)
