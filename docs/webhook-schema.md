# Webhook Schema — AI Control Plane

All outbound webhooks from the AI Control Plane use the same typed envelope.
Consumers should always check `schema_version` before parsing `payload`.

## Envelope

```json
{
  "schema_version": "1.0",
  "event_id": "uuid-v4",
  "event_type": "<AlertEventType>",
  "severity": "low | medium | high | critical",
  "timestamp": "2026-01-15T09:00:00",
  "source": "ai-control-plane",
  "payload": { }
}
```

`event_id` is a UUID and can be used as an idempotency key.
`schema_version` follows semver conventions:
- Minor additions (new optional payload fields) increment the minor version: `1.1`
- Breaking changes (field renames, removals, type changes) increment the major version: `2.0`

---

## Event Types and Payload Schemas

### `classification_request`
Severity: `medium`
Fired when an agent is discovered or registered but the manifest is too sparse to classify.

```json
{
  "item_id": "uuid",
  "agent_id": "string",
  "agent_name": "string",
  "team_id": "string",
  "missing_fields": ["data_scope", "owner_id"],
  "due_at": "2026-02-05T09:00:00"
}
```

---

### `holistic_review_required`
Severity: `medium`
Fired when an agent-type build (runtime tool/MCP selection) requires a holistic review. The fast path is not available.

```json
{
  "item_id": "uuid",
  "agent_id": "string",
  "agent_name": "string",
  "team_id": "string",
  "due_at": "2026-02-05T09:00:00"
}
```

---

### `workflow_reminder`
Severity: `medium`
Fired at Day +3 and Day +7 after a WorkflowItem is opened.

```json
{
  "item_id": "uuid",
  "agent_id": "string",
  "agent_name": "string",
  "reminder_number": 1,
  "days_open": 3,
  "due_at": "2026-02-05T09:00:00",
  "missing_fields": ["data_scope"]
}
```

---

### `workflow_escalated`
Severity: `high`
Fired at Day +14. Routed to the team's registered escalation webhook.

```json
{
  "item_id": "uuid",
  "agent_id": "string",
  "agent_name": "string",
  "team_id": "string",
  "days_open": 14,
  "days_until_cap": 7,
  "due_at": "2026-02-05T09:00:00",
  "missing_fields": ["data_scope"]
}
```

---

### `token_cap_applied`
Severity: `high`
Fired at Day +21 when the WorkflowItem was not resolved. A BudgetOverride has been written.

```json
{
  "item_id": "uuid",
  "agent_id": "string",
  "agent_name": "string",
  "team_id": "string",
  "capped_budget_usd": 25.0,
  "previous_budget_usd": 100.0,
  "cap_fraction": 0.25,
  "reason": "WorkflowItem not resolved within deadline",
  "resolution_url": "/agents/{agent_id}/workflow/{item_id}"
}
```

---

### `risk_threshold_breach`
Severity: `high`
Fired when an agent's risk assessment produces `human_gate` or `rate_limit` autonomy control.

```json
{
  "agent_id": "string",
  "agent_name": "string",
  "team_id": "string",
  "autonomy_control": "human_gate | rate_limit | detect_fast | let_run",
  "blast_radius": "high | low",
  "reversibility": "reversible | irreversible",
  "regulatory_flags": ["external_facing", "personal_data"],
  "notes": "string"
}
```

---

### `reclassification_triggered`
Severity: `medium`
Fired when a manifest change, new sub-agent, or burn-rate spike is detected.

```json
{
  "agent_id": "string",
  "agent_name": "string",
  "team_id": "string",
  "reason": "Manifest change detected: new tools: email; data_scope: invoker_only → beyond_invoker",
  "triggered_at": "2026-01-15T09:00:00"
}
```

---

### `budget_breach`
Severity: `high`
Fired when a user or team's spend crosses their budget threshold for the month.

```json
{
  "actor_id": "string",
  "team_id": "string",
  "year_month": "2026-01",
  "spend_usd": 120.50,
  "budget_usd": 100.0,
  "overage_usd": 20.50,
  "budget_source": "role | override | default_role"
}
```

---

### `capacity_reorder`
Severity: `medium | high`
Fired when a capacity pool's burn rate crosses its reorder point.

```json
{
  "pool_id": "string",
  "model": "string",
  "action": "increase | hold",
  "urgency": "low | medium | high",
  "tokens_remaining": 520600,
  "avg_daily_tokens": 75000,
  "days_of_supply_remaining": 6.9,
  "projected_stockout_date": "2026-01-22",
  "reason": "string"
}
```

---

### `unknown_agent_detected`
Severity: `medium`
Fired when a `UsageEvent` arrives with an `actor_id` not in the agent registry.

```json
{
  "actor_id": "string",
  "team_id": "string",
  "source_app": "ai_gateway | github_copilot | ...",
  "first_seen_at": "2026-01-15T09:00:00",
  "action": "Register this agent at /agents/register to begin classification."
}
```

---

### `dormant_agent_detected`
Severity: `low`
Fired when an agent has had no usage events within the dormancy window. Also fired in the quarterly sweep for all dormant agents.

```json
{
  "agent_id": "string",
  "agent_name": "string",
  "team_id": "string",
  "last_seen_at": "2025-11-01T09:00:00",
  "dormancy_days": 30,
  "tier": "tier_1 | tier_2 | tier_3",
  "sweep_type": "daily | quarterly",
  "recommendation": "string"
}
```

---

### `model_adoption_shift`
Severity: `low`
Fired when week-over-week token volume for a model changes by more than 50%.

```json
{
  "model": "string",
  "direction": "increase | decrease",
  "this_week_tokens": 500000,
  "last_week_tokens": 200000,
  "change_pct": 150.0
}
```

---

### `regulatory_flag_raised`
Severity: `medium`
Fired when classification assigns a regulatory flag to an agent for the first time.

```json
{
  "agent_id": "string",
  "agent_name": "string",
  "team_id": "string",
  "flag": "external_facing | personal_data | financially_material | market_facing",
  "tier": "tier_1 | tier_2 | tier_3",
  "note": "string"
}
```

---

## Alert Rule Configuration

Register a webhook destination via the storage API:

```python
from core.models import AlertRule, AlertEventType, Severity

rule = AlertRule(
    rule_id="team-a-slack",
    webhook_url="https://hooks.slack.com/services/...",
    event_types=[
        AlertEventType.CLASSIFICATION_REQUEST,
        AlertEventType.WORKFLOW_ESCALATED,
        AlertEventType.TOKEN_CAP_APPLIED,
    ],
    min_severity=Severity.MEDIUM,
    team_id="team-a",
    description="Team A Slack channel for AI governance alerts",
)
storage.upsert_alert_rule(rule)
```

A rule with `team_id=None` is the platform-wide fallback — it receives events for any team that has no registered rule for that event type.

---

## Escalation Webhook Routing

Escalation events (`workflow_escalated`, `token_cap_applied`) at Day +14/+21 are routed to the team's registered escalation webhook, not the general alert rule. The escalation webhook URL is resolved at WorkflowItem creation time:

1. Look up active `AlertRule` for `(team_id, event_type=workflow_escalated)`
2. If found: use that URL
3. If not: use the platform-wide fallback rule's URL
4. If neither: escalation is written to audit log only
