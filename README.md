# AI Control Plane

> **Status:** active prototype. The supply chain planner and agent governance
> pipeline both work end-to-end against the included synthetic test fixture.
> Run `python3 demo.py` to see both pipelines with zero setup.
> See [Roadmap](#roadmap) for what's next.

> ## Automated Code Review
>
> This repository uses [PR Agent](https://github.com/The-PR-Agent/pr-agent) to automatically review pull requests:
> - **Auto-review:** Comprehensive code review on every PR
> - **Auto-describe:** Generates PR title, summary, and labels
> - **Model:** Claude 3.5 Sonnet via Anthropic API
>
> All PRs receive an automated review comment within 1-2 minutes of opening. Use `/review` to trigger manually or `/disable` to skip.

A lightweight control plane for organisations running AI workloads across
multiple platforms — with three purposes:

1. **Budget aggregation**: one number for how much a person or team is
   spending across every AI tool (gateway, GitHub Copilot, M365 Copilot,
   Copilot Studio, Snowflake, Databricks, ...), not five separate dashboards.
2. **Token supply chain planning**: treat pre-purchased model capacity
   (e.g. Azure PTUs) as inventory — track burn rate, calculate reorder
   points, and get advisory recommendations before you run out.
3. **Agent governance**: discover every AI agent running in your estate,
   classify it by tier and risk, track compliance workflow items, and fire
   typed webhook alerts — before a gap in visibility becomes a control failure.

---

## Quick start (demo — no credentials needed)

```bash
git clone <this-repo>
cd ai-control-plane
python3 demo.py
```

No dependencies, no credentials, no setup. The demo generates 28 days of
synthetic multi-team usage and runs it through both pipelines:

**Supply chain planner:**
```
Pool                      Model                  Avg/day    Remaining  Days left   Action  Urgency
pool-gpt4o-eastus         gpt-4o                  79,382    1,912,651       24.1     hold      low
pool-sonnet-eastus        claude-sonnet-4-6        76,227    1,486,415       19.5     hold      low
pool-haiku-eastus         claude-haiku-4-5         66,549            0        0.0 increase     high
   -> Burn rate has crossed the reorder point. Only 0.0 days of supply remain.
```

**Agent governance pipeline:**
```
Agent                          Expected     Result     Role    Confidence
HR Knowledge Agent               tier_1     tier_1  watcher         100%
DPIA Automation Agent            tier_2     tier_2  striker         100%
Claude Code Agent                tier_3     tier_3 artificer        100%
  Autonomy control: human_gate  (blast=high, reversibility=irreversible)
  [WORKFLOW] Holistic review opened — agent-type control flow

Incomplete Agent (missing manifest) unclassified  unclassified
  [WORKFLOW] Classification request: missing ['data_scope']
```

---

## Use this on your own org's data

The point of this project is to plug it into your own AI tools and get real,
cross-tool dashboards — not to run it against demo data.

**For usage events**, implement `UsageConnector.pull()`:

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

**For agent manifest discovery**, also implement `pull_manifest_fragments()`:

```python
    def pull_manifest_fragments(self, since: datetime) -> list[AgentManifestFragment]:
        # return partial manifest data inferred from your platform's metadata API
        # the registry merges fragments from multiple connectors automatically
        ...
```

Nothing in `core/` needs to change to add a new source. Reference connectors
for GitHub Copilot, Copilot Studio, Azure AI Foundry, Snowflake Cortex,
Databricks, LangSmith, and Dynatrace are in `connectors/` — they show the
pattern and can be adapted for your auth and org mappings.

**Your connectors, and the real data they touch, should live in your own
private repo.** They embed org-specific auth and cost-center mappings, and
real usage data is typically personal data under GDPR once tied to a real
person. This repo gives you the engine and the interface; it never needs to
see your real data to do that.

---

## Why this exists

Most organisations adopting LLMs end up with usage scattered across a gateway,
SaaS AI features, and data platforms — each billed differently, each with its
own dashboard, and no single view of spend or risk per person or team.

Separately, once you're purchasing model capacity ahead of time (PTUs,
reserved throughput), you have an inventory problem: burn rate varies, running
out is an operational failure, and new models land with zero usage history
right when planning is hardest.

And separately again, once you have more than a handful of AI agents running,
you need to know what each one can reach, whether it's been reviewed, and
whether it's behaving within its approved scope — before an audit asks.

This project treats each as what it is: an aggregation problem, an
inventory-management problem with a cold-start case, and a governance problem
with a mechanical rule engine at its core.

---

## How it works

### Supply chain planner

```
UsageConnector.pull()
    │
    ▼
UsageEvent (actor, team, model, tokens, cost_usd)
    │
    ▼
burn_rate.summarize_burn_rate()   →  avg/std daily demand, days of supply remaining
    │
    ▼
recommender.recommend_for_pool()  →  hold | increase | reallocate + urgency + reason
```

Reorder point math (standard inventory theory):

```
reorder_point = (avg_daily_demand × lead_time_days) + safety_stock
safety_stock   = z_score × demand_std_dev × sqrt(lead_time_days)
```

`lead_time_days` is how long it actually takes to provision new capacity —
procurement + setup, not just an API call. `z_score` is set by your target
service level (95% by default).

### Agent governance pipeline

```
UsageConnector.pull()  +  UsageConnector.pull_manifest_fragments()
    │                              │
    ▼                              ▼
agent_registry.process_usage_events()   →  discover unknown agents
agent_registry.merge_manifest_fragment() →  assemble AgentManifest
    │
    ▼
classifier.classify()
    ├─ manifest complete?  →  ClassificationResult  (tier, role, flags, confidence)
    └─ manifest sparse?    →  ClassificationRequest  →  WorkflowItem raised
    │
    ▼
risk.assess_risk()          →  AutonomyControl (let_run | detect_fast | rate_limit | human_gate)
risk.detect_manifest_change() →  ReclassificationTrigger on tooling/scope change
risk.detect_burn_rate_spike() →  ReclassificationTrigger on >2σ usage jump
    │
    ▼
workflow.WorkflowEngine     →  reminder (Day +3, +7) → escalation (Day +14)
                               → token cap BudgetOverride (Day +21)
    │
    ▼
alerting.AlertDispatcher    →  typed WebhookEnvelope  (schema_version: "1.0")
                               routed per team + platform-wide fallback
    │
    ▼
trends.TrendsEngine         →  daily snapshots, week-over-week delta,
                               adoption shift alerts, dormant detection,
                               quarterly sweep
```

### Tier classification rules

Tier is assigned mechanically from an agent's manifest — the same rules run
regardless of platform, team, or build tool:

| Tier | Name | Triggers (any one) |
|---|---|---|
| **3** | Executing | `execution_rights`, `credentials_held`, `can_modify_repos_or_pipelines`, `can_modify_other_agents` |
| **2** | Crossing | `data_scope = beyond_invoker`, `writes_to_system_of_record`, `reaches_external_party`, `distributes_output_autonomously`, `grounding_includes_confidential_of_others` |
| **1** | Contained | All contained conditions hold (default) |

Summoner floor rule: an orchestrator's effective tier = `max(own_tier, max(sub_agent_tiers))`.

Agent-type control flow (runtime tool/MCP selection) always triggers a holistic
review WorkflowItem regardless of tier — individually safe capabilities can
combine into something none of them exposes alone.

---

## Project structure

```
core/
  models.py            # canonical schema — UsageEvent, Agent, CapacityPool, WorkflowItem, ...
  burn_rate.py          # demand bucketing, reorder point calculation
  recommender.py        # burn-rate → advisory recommendation
  model_lifecycle.py    # auto-deprecation by model family + version
  agent_registry.py     # discovery from gateway logs, manifest assembly, dormancy
  classifier.py         # rule-based tier/role/flag inference
  workflow.py           # WorkflowItem lifecycle, escalation timeline, token cap
  risk.py               # autonomy matrix, manifest change detection, spike detection
  alerting.py           # typed webhook dispatch with versioned envelope
  trends.py             # daily snapshots, adoption curves, dormant detection
  observability.py      # normalisation utilities for LangSmith and Dynatrace

storage/
  base.py               # StorageBackend protocol
  sqlite.py             # zero-setup, stdlib-only (dev / demo)
  snowflake.py          # production analytics and trend queries
  azure_blob.py         # append-only audit trail (AuditBackend protocol)

connectors/
  base.py               # UsageConnector interface (pull + pull_manifest_fragments)
  synthetic.py          # test fixture — realistic demo data, not for production
  langsmith.py          # LangSmith run traces
  dynatrace.py          # Dynatrace AI observability spans
  github_copilot.py     # GitHub Copilot seat + usage API
  copilot_studio.py     # Power Platform bot usage + connector manifest
  azure_ai_foundry.py   # Azure Monitor token metrics + endpoint manifest
  snowflake_cortex.py   # QUERY_HISTORY + execution rights detection
  databricks.py         # cluster job usage (always Tier 3 — executes code)

tests/
  test_burn_rate.py
  test_model_lifecycle.py
  test_storage.py
  test_synthetic_coding_assistant.py
  test_classifier.py
  test_risk.py
  test_workflow.py
  test_trends.py

docs/
  architecture.md       # full pipeline diagram and design notes
  webhook-schema.md     # typed payload schemas for all 13 alert event types

demo.py                 # runs both pipelines end-to-end, zero credentials
```

---

## Storage backends

| Backend | When to use |
|---|---|
| `SqliteBackend` | Local dev, demo, tests. Zero setup — stdlib only. |
| `SnowflakeBackend` | Production analytics, trend queries, budget rollups at scale. |
| `AzureBlobAuditBackend` | Append-only immutable audit trail. Always use alongside the primary backend in production. |

The `StorageBackend` protocol (`storage/base.py`) means swapping backends is
a one-line configuration change in callers; no other code changes.

---

## Webhook alerts

All outbound alerts use a typed, versioned envelope:

```json
{
  "schema_version": "1.0",
  "event_id": "uuid-v4",
  "event_type": "classification_request",
  "severity": "medium",
  "timestamp": "2026-01-15T09:00:00",
  "source": "ai-control-plane",
  "payload": { ... }
}
```

Thirteen event types are defined — see [`docs/webhook-schema.md`](docs/webhook-schema.md)
for the full payload schema per event type.

Alert rules are registered per team with a platform-wide fallback:

```python
from core.models import AlertRule, AlertEventType, Severity

# Team-specific rule
storage.upsert_alert_rule(AlertRule(
    rule_id="team-a-slack",
    webhook_url="https://hooks.slack.com/services/...",
    event_types=[AlertEventType.CLASSIFICATION_REQUEST, AlertEventType.TOKEN_CAP_APPLIED],
    min_severity=Severity.MEDIUM,
    team_id="team-a",
))

# Platform-wide fallback (team_id=None) — catches agents with no team rule
storage.upsert_alert_rule(AlertRule(
    rule_id="platform-fallback",
    webhook_url="https://your-ops-channel/webhook",
    event_types=list(AlertEventType),
    min_severity=Severity.LOW,
    team_id=None,
))
```

Escalation webhooks (WorkflowItem Day +7 / +14) are routed to the team's
registered escalation webhook, not the general alert rule.

---

## Workflow escalation timeline

When an agent cannot be classified (manifest too sparse) or requires holistic
review (agent-type control flow), a `WorkflowItem` is opened and the following
timeline runs automatically:

| Day | Action | Severity |
|---|---|---|
| 0 | WorkflowItem opened, webhook fired | `medium` |
| +3 | First reminder | `medium` |
| +7 | Second reminder + escalation target notified | `medium` |
| +14 | Escalation — manager / platform owner | `high` |
| +21 | Token cap applied (25% of budget ceiling via `BudgetOverride`) | `high` |

All thresholds are configurable. The cap is Phase A enforcement (internal
`BudgetOverride`). Platform API enforcement — Azure APIM rate limits, Copilot
Studio quotas, Snowflake resource monitors — is the next roadmap item.

---

## Roadmap

**Supply chain planner**
- [x] Canonical usage event schema + connector interface
- [x] Synthetic data generator (seasonality, growth trend, spike, model release)
- [x] Burn rate tracking + reorder-point / safety-stock calculation
- [x] Advisory recommendation engine
- [x] SQLite storage layer — usage events, capacity pools, roles, budget overrides
- [x] Model lifecycle + auto-deprecation (`model_lifecycle.py`)
- [x] `demand_driver` on capacity pools — `human_driven` vs `agent_driven`
- [x] Coding-assistant shaped synthetic generator (`synthetic_coding_assistant.py`)
- [ ] `synthetic_agent.py` — machine-paced demand generator
- [ ] Holt-Winters demand forecasting — replaces simple rolling-average burn rate
- [ ] Cold-start benchmarking — projected demand for models with no usage history
- [ ] Model release watcher — advisory signal when a newer/cheaper model is available

**Agent governance**
- [x] Agent registry — discovery from gateway logs, manifest assembly, dormancy
- [x] Rule-based tier classifier — Tier 1/2/3, Summoner floor rule, functional roles
- [x] Risk engine — autonomy matrix, manifest change detection, burn rate spike detection
- [x] Workflow engine — classification request and holistic review tracking, escalation timeline, token cap (Phase A)
- [x] Typed webhook alerting — 13 event types, versioned envelope, per-team routing
- [x] Trends engine — daily snapshots, week-over-week delta, dormant detection, quarterly sweep
- [x] Platform connectors — LangSmith, Dynatrace, GitHub Copilot, Copilot Studio, Azure AI Foundry, Snowflake Cortex, Databricks
- [ ] Token cap Phase B — platform API enforcement (Azure APIM, Copilot Studio quota, Snowflake resource monitors, Databricks cluster policy)

**Storage**
- [x] SQLite backend (dev / demo)
- [x] Snowflake backend (production analytics)
- [x] Azure Blob append-only audit trail
- [ ] Budget aggregation dashboard (Streamlit)
- [ ] Supply chain + governance dashboard (Streamlit)
- [ ] FastAPI layer
- [ ] MCP agent-facing layer — read tools open; write actions always two-step (propose → human-confirm)

---

## Design notes

- **USD is the common unit for budget aggregation; tokens are the unit for
  supply planning.** Snowflake and Databricks don't bill in tokens, and
  GitHub Copilot / M365 Copilot are seat-based — USD is the only unit all
  sources share. Tokens only exist natively at the gateway.
- **"Copilot" is always fully qualified.** GitHub Copilot, M365 Copilot,
  Copilot Studio, and M365 Cowork are distinct products with distinct billing
  models and usage APIs. `SourceApp` values reflect this (`GITHUB_COPILOT`,
  `M365_COPILOT`, `COPILOT_STUDIO`, `M365_COWORK`).
- **Tier is assigned from configuration, not from platform.** The same
  classifier rules run whether the agent is built in Copilot Studio, Azure AI
  Foundry, or a custom gateway. Platform is context, not a classification input.
- **Budget triggers are strictly aggregate; model breakdown is analytics-only.**
  Nothing about which model was used should gate whether someone is over budget.
- **Budget resolution order is override → role → default role.** An unassigned
  user falls back to `non_tech` explicitly — nobody's spend goes unmonitored
  because their role mapping is missing.
- **Recommendations, not automated purchases or enforcement.** The supply chain
  planner surfaces recommendations for a human to act on. The governance engine
  applies an internal budget cap at Day +21; platform API enforcement is a
  roadmap item, not the default.
- **Public/private split is intentional.** Core engine and interface are fully
  generic. Connectors (org-specific auth, cost-center mappings, real usage data
  requiring data-protection review) live in your private repo.
- **Audit events are never silently lost.** `append_audit_event` in both the
  Snowflake and Azure Blob backends logs at ERROR and re-raises on failure.
  The SQLite copy is queryable; the Blob copy is authoritative and immutable.

---

## License

MIT — see [LICENSE](LICENSE).
