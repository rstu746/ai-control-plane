# AI Control Plane

> **Status:** active prototype, built as a hackathon proof-of-concept. Core
> supply chain planning engine works end-to-end against the included
> synthetic test fixture (see Quick start) — write a connector against your
> own data to get real dashboards. Budget aggregation and cold-start
> benchmarking are designed but not yet built — see [Roadmap](#roadmap).
>
> ## Automated Code Review

This repository uses [PR Agent](https://github.com/The-PR-Agent/pr-agent) to automatically review pull requests:

- **Auto-review:** Comprehensive code review on every PR
- **Auto-describe:** Generates PR title, summary, and labels
- **Model:** Claude 3.5 Sonnet via Anthropic API

All PRs will receive an automated review comment within 1-2 minutes of opening. Developers can also use `/review` command to manually trigger reviews or `/disable` to skip specific PRs.

A lightweight control plane for organizations running AI workloads across
multiple platforms — with two purposes:

1. **Budget aggregation**: one number for how much a person or team is
   spending across every AI tool (gateway, GitHub Copilot, M365 Copilot,
   Copilot Studio, M365 Cowork, Snowflake, Databricks, ...), not five
   separate dashboards.
2. **Token supply chain planning**: treat pre-purchased model capacity
   (e.g. Azure PTUs) as inventory — track burn rate, calculate reorder
   points, and get advisory recommendations before you run out.

This repo currently implements **(2), the supply chain planner**, end to end
against a synthetic demo connector. Budget aggregation is architected for
but not yet built — see [Roadmap](#roadmap).

## Use this on your own org's data

The point of this project is to plug it into your own AI tools and get real,
cross-tool dashboards — not to run it against demo data. That means writing
one small connector per source you want visibility into:

```python
from connectors.base import UsageConnector
from core.models import UsageEvent

class MyGatewayConnector(UsageConnector):
    def source_name(self) -> str:
        return "my_gateway"

    def pull(self, since: datetime) -> list[UsageEvent]:
        # call your gateway's usage API, normalize into UsageEvent
        ...
```

Nothing in `core/` needs to change to add a new source — `core/burn_rate.py`,
`core/recommender.py`, and (once built) the budget rollup all operate purely
on `UsageEvent`/`CapacityPool`, regardless of where the data came from.

**Your connectors, and the real data they touch, should live in your own
private repo**, not here. They'll embed org-specific auth and cost-center
mappings, and — since real usage data is typically personal data under
GDPR/similar regimes once tied to a real person — pulling it is your org's
call to make, with whatever review that requires internally. This repo
gives you the engine and the interface; it deliberately never needs to see
your real data to do that.

**A note on the synthetic connector included here:** `connectors/synthetic.py`
is a *test fixture*, not a feature you're meant to use in production. It
exists so you (or anyone evaluating this repo) can clone it and see the
whole pipeline — burn rate, reorder points, recommendations — run
end-to-end with zero setup, zero credentials, and zero data-governance
questions, before writing a single line of real-connector code. See
[Quick start](#quick-start) below.

## Why this exists

Most organizations adopting LLMs end up with usage scattered across a gateway,
a few SaaS AI features, and a couple of data platforms with their own AI
add-ons — each billed differently (tokens, seats, compute credits), each with
its own dashboard, and no single view of spend per person or team.

Separately, once you're purchasing model capacity ahead of time (PTUs,
reserved throughput) rather than paying per-call, you have an actual
inventory problem: burn rate varies, running out of provisioned capacity is
a real operational failure (not just a billing surprise), and new models
land on the gateway with zero usage history — which is exactly when
capacity planning is hardest, since the statistics this planner relies on
don't exist yet for a model nobody has used.

This project treats these as what they are: an aggregation/attribution
problem, and an inventory-management problem with a cold-start case. It
borrows directly from classic supply chain math (reorder point, safety
stock) rather than reinventing something bespoke, and — where a new model
has no burn history to plan against — from benchmark-driven demand
projection instead (see [Roadmap](#roadmap)).

## Quick start (demo / test fixture — no real data needed)

```bash
git clone <this-repo>
cd ai-control-plane
python3 demo.py
```

No dependencies, no credentials, no setup. This generates 28 days of
synthetic multi-team usage — with realistic weekday seasonality, a growth
trend, one demand spike, and one simulated "new model release" that shifts
traffic toward a cheaper model — and runs it through the full burn-rate →
reorder-point → recommendation pipeline.

Example output:

```
Pool                      Model                     Avg/day    Remaining  Days left     Action  Urgency
----------------------------------------------------------------------------------------------------
pool-gpt4o-eastus         gpt-4o                     75,494    1,967,086       26.1       hold      low
pool-sonnet-eastus        claude-sonnet-4-6          74,148    1,515,522       20.4       hold      low
pool-haiku-eastus         claude-haiku-4-5           64,757            0        0.0   increase     high
   -> Burn rate has crossed the reorder point (520,600 tokens). Only 0.0 days
      of supply remain against a 5-day procurement lead time for 'claude-haiku-4-5'.
```

The haiku pool is deliberately undersized in the demo — it's the one that
should trip the reorder point once the synthetic "model release" event shifts
traffic onto it, which is exactly the scenario this tool is meant to catch.

## How it works

For the full picture — including the hackathon-vs-go-to-market split and
what specifically changes between them — see
[docs/architecture.md](docs/architecture.md). Summary below.

```
UsageConnector (pluggable)
    │
    ▼
UsageEvent (canonical schema: actor, team, model, tokens, cost_usd)
    │
    ▼
burn_rate.summarize_burn_rate()   →  avg/std daily demand, days of supply remaining
    │
    ▼
recommender.recommend_for_pool()  →  hold | increase | reallocate + urgency + reason
```

**Reorder point math**, adapted from standard inventory theory:

```
reorder_point = (avg_daily_demand × lead_time_days) + safety_stock
safety_stock   = z_score × demand_std_dev × sqrt(lead_time_days)
```

`lead_time_days` is how long it actually takes to provision new capacity —
procurement + setup, not just an API call — and is the key input that makes
this useful in practice rather than theoretical. `z_score` is set by your
target service level (95% by default: `Z_SCORE_BY_SERVICE_LEVEL` in
`core/burn_rate.py`).

Recommendations are **advisory, not automated** — this is real budget, so v1
always surfaces a recommendation for a human to act on rather than
triggering a purchase directly.

## Project structure

```
core/
  models.py        # UsageEvent, CapacityPool, Recommendation — canonical schema
  burn_rate.py      # demand bucketing, reorder point calculation
  recommender.py    # turns burn-rate analysis into actionable recommendations
connectors/
  base.py           # UsageConnector interface — implement this for a new source
  synthetic.py      # test fixture — realistic demo data, not for production use
tests/
  test_burn_rate.py
demo.py             # runs the full pipeline end to end against the synthetic fixture
```

Real connectors you write against `UsageConnector` (see
[Use this on your own org's data](#use-this-on-your-own-orgs-data) above)
are expected to live in a separate, private repo that imports this one as a
dependency — nothing here needs to change to add one.

## Roadmap

- [x] Canonical usage event schema + connector interface
- [x] Synthetic data generator (seasonality, growth trend, spike, model release)
- [x] Burn rate tracking + reorder-point / safety-stock calculation
- [x] Advisory recommendation engine
- [x] **SQLite storage layer** (`core/storage.py`) — usage events, capacity
      pools, roles/users/budget overrides. Budget resolution per user/month:
      override → role → default role, in that order.
- [x] **Model lifecycle + auto-deprecation** (`core/model_lifecycle.py`) —
      models tracked as `(model_family, version_number)`; a version is
      auto-deprecated once a version 2+ releases ahead of it within the
      same family (one version of buffer stays active). Deprecated pools
      never get "increase" recommendations but stay visible in trend views.
- [x] **`demand_driver` on capacity pools** — `human_driven` (coding
      assistants: headcount-bound, working-hours-shaped) vs. `agent_driven`
      (can run continuously/on schedule, decoupled from headcount).
      Burn-rate shape and forecasting assumptions differ between the two.
- [x] **`synthetic_coding_assistant.py`** — weekday/working-hours shaped
      generator: soft mid-morning peak, post-lunch bump, and a fixed
      per-developer weekend-active subset (~15-20% of devs, same people
      every week, not resampled) producing overall weekend volume ~10-15%
      of a weekday's. Daily aggregation is what matters here — see
      `CODING_ASSISTANT_LEAD_TIME_DAYS` (21 days) in that module, matching
      the 3-weeks-out reorder trigger design.
- [ ] `synthetic_agent.py` — machine-paced generator (no natural weekday
      drop-off, can be continuous/scheduled/event-triggered), same
      `UsageEvent` schema, `agent_driven` demand shape.
- [ ] **Cold-start benchmarking** — new models land on the gateway with no
      usage history, which the reorder-point math above depends on. Planned
      fix: a small preset task suite run against a candidate model
      (`core/benchmark.py`), producing tokens-per-task and pass/fail, which
      feeds a projected-demand estimate instead of a trailing average until
      real usage history accrues.
- [ ] Demand forecasting via Holt-Winters (`statsmodels`), replacing the
      simple rolling-average burn rate for established models
- [ ] Model release watcher — advisory signal when a new/cheaper model
      becomes available for a model you already hold a pool for
- [ ] Streamlit dashboard (pools, burn rate vs. forecast, recommendations;
      budget dashboards by user/team/role, model breakdown analytics-only)
- [ ] Budget aggregation trigger wired to the dashboard (alerting on
      threshold breach) — storage/resolution logic above is in place,
      alerting on top of it is not yet built
- [ ] FastAPI layer exposing both engines
- [ ] Agent-facing MCP layer — read tools (spend/recommendation queries)
      fully open to an agent; write actions (budget limit changes, capacity
      purchase approval) always two-step (`propose_*` → human-confirmed
      `confirm_*`), never agent-autonomous. See
      [docs/architecture.md](docs/architecture.md#agent-facing-layer-future-not-yet-built).

## Next Steps: Implementation Roadmap (Post-Hackathon)

The core engine works end-to-end. What's next depends on your deployment path:

### Phase 1: Connectors + Real Data (Weeks 1-3)

**Goal:** Pull real usage data from your org's AI tools into the engine.

1. **Write connectors** for your sources — one per tool you want visibility into:
   - Implement `UsageConnector.pull()` for each SourceApp (AI Gateway, GitHub Copilot, Snowflake, etc.)
   - Handle org-specific auth, cost-center/team mapping, and data privacy review
   - Live in your **private repo**; this public one stays generic
2. **Backfill historical usage** (if available) to train the burn-rate models
3. **Wire up `core/storage.py`** to your database (Postgres in prod; SQLite works for pilot)

### Phase 2a: Budget Dashboard (Weeks 3-5) — **Recommended Priority**

**Why first:** Every org using AI tools needs spend tracking and budget enforcement. More widely applicable than PTU inventory planning.

**Mockup & design:** See `Budget Dashboard` artifact linked in the repo — shows the layout, metrics, and data structure.

**Implementation:**
1. Build a **Streamlit app** that queries `core/storage.py` for usage aggregates:
   - **Top metrics:** Total spend this period, budget remaining, forecast to EOM, top consumer
   - **Yearly trends:** 3-year bar chart showing YoY growth rates and adoption acceleration
   - **Tool adoption:** Active users per SourceApp (last 30 days), penetration %, adoption curves
   - **Spend by source app** (AI Gateway, GitHub Copilot, Snowflake, Databricks, M365 Copilot, Copilot Studio)
   - **Budget vs. actual by role** (from Role + User + BudgetOverride tables)
   - **Top consumers** (aggregated spend per user with team/role context)
   - **Spend trends** (daily/weekly over last 30 days, stacked by source)
   - **Model breakdown** (which models are driving cost, cost-per-use analytics)
2. Add filtering: date range, department/team, optional role picker, year picker
3. Wire alerts: **threshold breach** when a team/user approaches budget (storage logic is in place; alerting layer is not)

**Data queries you'll need:**
- `Storage.get_spend_by_source(start_date, end_date, team_id=None)` — aggregated UsageEvent by SourceApp
- `Storage.get_spend_by_year()` — annual totals for 3-year trend view
- `Storage.get_active_users_by_source(days=30)` — count(distinct actor_id) per SourceApp for adoption metrics
- `Storage.get_user_spend_for_month(user_id, year_month)` — check budget limits
- `Storage.get_spend_breakdown_by_model(start_date, end_date)` — analytics view by model + cost-per-use

### Phase 2b: Supply Chain Dashboard (Weeks 5-7) — **Optional / Only if PTU Purchasing**

**Why optional:** Only applies to orgs buying pre-purchased capacity (e.g., Azure PTUs). Many orgs use pay-as-you-go and skip this entirely.

**Mockup & design:** See `Supply Chain Dashboard` artifact — shows pools, burn rates, reorder recommendations, model lifecycle.

**Implementation:**
1. Build a **Streamlit app** for capacity pool monitoring:
   - Pool inventory: model, region, capacity, tokens remaining, % consumed, days of supply
   - Burn rate trends: daily/weekly consumption over last 28 days
   - Recommendations panel: increase/hold actions by urgency, human approval buttons
   - Model lifecycle: candidate → benchmarked → piloted → established → deprecated states
2. Wire **human approval workflow** (approve/dismiss buttons persist action intent to storage)
3. Feed recommendations to an advisory channel (Slack, email) for ops team

**Data queries you'll need:**
- `Storage.get_capacity_pools()` — all CapacityPool records
- `Storage.get_burn_rate_summary(pool_id)` — from `core/burn_rate.summarize_burn_rate()`
- `Storage.get_recommendations()` — from `core/recommender.recommend_for_pool()`

### Phase 3: Forecasting & Cold-Start Benchmarking (Weeks 8+)

Once you have real usage history and PTU purchasing:
- **Holt-Winters forecast** (statsmodels) for established models — replaces simple rolling average
- **Benchmark suite** for new models → projected demand before real usage accrues
- **Model release watcher** — alert when a newer/cheaper model becomes available

### Data Flow Recap

```
Your Connectors (private repo, auth + mapping)
    ↓
UsageEvent + CapacityPool (canonical schema)
    ↓
core/storage.py (SQLite/Postgres)
    ↓
Budget Dashboard ← core/storage queries (spend by source, budget vs actual)
Supply Chain Dashboard ← core/burn_rate.py + core/recommender.py (inventory planning)
```

**Key principle:** `core/` stays generic and public. Everything org-specific (connectors, private data, alerting policy) lives in your private repo.

## Design notes

- **USD is the common unit for budget aggregation; tokens are the unit for
  supply planning.** Snowflake and Databricks don't bill in tokens, and
  GitHub Copilot / M365 Copilot are seat-based — USD is the only unit all
  sources share. Tokens only exist natively at the gateway, which is where
  the supply chain problem actually lives.
- **"Copilot" is always fully qualified, never used alone.** GitHub Copilot,
  M365 Copilot, Copilot Studio, and M365 Cowork are distinct products with
  distinct billing models (seat-based vs. usage-based) and distinct usage
  APIs — treating them as one "Copilot" source would break attribution.
  `SourceApp` values reflect this (`GITHUB_COPILOT`, `M365_COPILOT`,
  `COPILOT_STUDIO`, `M365_COWORK`), and any connector or doc referencing
  one should too.
- **Budget triggers are strictly aggregate; model breakdown is
  analytics-only.** `Storage.get_user_spend_for_month()` (what a budget
  check reads) and `Storage.get_spend_breakdown_by_model()` (what a
  dashboard's "why" view reads) are deliberately separate queries, not one
  query two ways — nothing about *which* model was used should ever gate
  whether someone is over budget.
- **Budget resolution order is override → role → default role**, never
  silently defaulting an *assigned* user to a different role's budget. An
  unassigned user falls back to the default role explicitly (`non_tech` —
  the more conservative of the two starting roles) rather than being
  skipped, so nobody's spend goes unmonitored just because their role
  mapping is missing.
- **Recommendations, not automated purchases.** Keeping a human in the loop
  is the right trust boundary for something spending real money, even once
  the forecasting matures.
- **Public/private split is intentional, not a limitation.** The core engine
  and interface are fully generic; only the connectors (which carry auth and
  internal mappings, and — for a real deployment — real usage data requiring
  its own data-protection review) are expected to be private.

## License

MIT — see [LICENSE](LICENSE).
# Testing Claude PR Agent with proper permissions
