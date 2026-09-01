"""
AI Control Plane Dashboard — entry point.

Run with:
    streamlit run dashboard/app.py

On first launch (empty DB) the app seeds the database with synthetic demo
data (~3 seconds). Set CONTROL_PLANE_DB to point at a real database.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import streamlit as st

from dashboard.data import DB_PATH, get_db
from dashboard.seed import is_seeded, run_seed

# ---------------------------------------------------------------------------
# Page config — must be first Streamlit call in the entry point
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="AI Control Plane",
    page_icon="🛩️",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------------------------
# Auto-seed on first launch
# ---------------------------------------------------------------------------
db = get_db()
if not is_seeded(db):
    with st.spinner("Seeding demo data — this takes about 3 seconds..."):
        run_seed(db)
    st.success("Demo data loaded. Welcome to the AI Control Plane.")
    st.rerun()

# ---------------------------------------------------------------------------
# Navigation
# ---------------------------------------------------------------------------
overview = st.Page("pages/1_overview.py", title="Overview", icon="🏠", default=True)
registry = st.Page("pages/2_agent_registry.py", title="Agent Registry", icon="🤖")
analytics = st.Page("pages/3_analytics.py", title="Analytics & Trends", icon="📊")
governance = st.Page("pages/4_governance.py", title="Governance", icon="🛡️")
supply_chain = st.Page("pages/5_supply_chain.py", title="Supply Chain", icon="📦")

pg = st.navigation(
    {
        "": [overview],
        "Core": [registry, analytics, governance],
        "Optional": [supply_chain],
    }
)

# Sidebar footer
with st.sidebar:
    st.divider()
    st.caption(f"DB: `{DB_PATH}`")
    st.caption("Set `CONTROL_PLANE_DB` env var to use a real database.")
    if st.button("🔄 Refresh data", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

pg.run()
