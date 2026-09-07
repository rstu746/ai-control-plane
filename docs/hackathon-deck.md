# AI Control Plane

Hackathon presentation deck. The deck is intentionally short: tell one complete governance story, then use the remaining screens to show operational depth.

## Slide 1 — AI adoption has an accountability gap

### On slide

**AI is spreading faster than organisations can govern it.**

Teams now run agents across gateways, coding assistants, SaaS tools, and data platforms. The result is fragmented visibility:

- What agents are running?
- What can each one reach or change?
- Who owns the unresolved risk?

### Speaker notes

This is not primarily a dashboard problem. It is an accountability problem. Existing tools show activity inside individual platforms, but they do not provide one control loop across the estate.

Do not open with spend or the supply-chain planner. Those are useful consequences of visibility, but governance is the central story.

## Slide 2 — The control loop

### On slide

```text
Discover  →  Classify  →  Assess risk  →  Route action  →  Resolve
   │            │              │               │             │
 usage       capability     blast radius     owner +       audit trail
 signals      manifest      × reversibility  escalation
```

**AI Control Plane turns unknown AI activity into owned work.**

### Speaker notes

The product does not assume builders will self-register perfectly. It can discover agents from usage, assemble a manifest, apply the same classification rules across platforms, and create a workflow item when information is missing or dynamic behaviour needs review.

## Slide 3 — Classification is based on capability, not vendor

### On slide

| Example agent | Capability signal | Resulting control |
|---|---|---|
| HR Knowledge Agent | Read-only, invoker-scoped | Tier 1 — Let run |
| DPIA Automation Agent | Writes to a system of record | Tier 2 — Controlled crossing |
| Claude Code Agent | Executes code, holds credentials, modifies repos | Tier 3 — Human gate |

**Same rules. Different capabilities. Consistent decisions.**

### Speaker notes

Use the Agent Registry for this slide. Select the three agents and expand the Claude Code manifest. Point to execution rights, credentials, and repository modification.

The important design choice is that platform is context, not classification. Copilot Studio, Azure AI Foundry, and a custom gateway build should not receive different governance merely because they came from different vendors.

## Slide 4 — Unknown is a workflow, not a blind spot

### On slide

**Incomplete manifest → classification request → reminders → escalation → human resolution**

The system refuses to invent a low-risk answer when required capability data is missing.

### Speaker notes

Go to Governance and show the incomplete data pipeline agent. Highlight the missing `data_scope`, owner, due date, escalation state, and Resolve action.

Resolve the item live if time allows. This demonstrates state change and makes the product feel operational rather than static.

## Slide 5 — Risk becomes an explicit control decision

### On slide

**Blast radius × reversibility**

```text
                         Higher blast radius
                                │
                    RATE LIMIT  │  HUMAN GATE
                                │
                    LET RUN     │  DETECT FAST
                                │
                         Lower blast radius
                 Reversible ────┴──── Irreversible
```

Risk output is actionable: let run, detect fast, rate limit, or require a human gate.

### Speaker notes

Use the Claude Code detail panel to show `human_gate`. The point is not that the system has produced a magic risk score. The point is that capabilities are translated into a control recommendation that an owner can understand and act on.

## Slide 6 — One control plane, two operational lenses

### On slide

**Governance first. Operations alongside it.**

- Agent estate and unresolved governance work
- Spend and model adoption across sources
- Human versus agent demand
- Provisioned-capacity burn rate and reorder recommendations

All demo data is synthetic and runs locally without production credentials.

### Speaker notes

Return to Overview, then briefly show Analytics or Supply Chain. Do not walk through every chart. Use this slide to establish that governance decisions sit in an operational context: usage, cost, model changes, and capacity pressure.

Be explicit that the current dashboard is a safe synthetic-data demonstration, not a live enterprise deployment.

## Slide 7 — What is built, and what comes next

### On slide

**Built for the hackathon**

- Discovery from usage events
- Manifest-based Tier 1/2/3 classification
- Risk matrix and regulatory flags
- Governance workflow with escalation and resolution
- Streamlit dashboard, SQLite persistence, synthetic end-to-end data

**Next production step**

Connect real sources and identity systems behind the existing connector/storage seams, then complete the required security and data-protection review before any real usage data is pulled.

### Speaker notes

Close with honesty. The hackathon proves the control loop locally. Productionization is not being presented as solved: it requires real connector deployment, identity mapping, access control, data governance, and platform-edge enforcement.

Final line:

> “The goal is not another AI dashboard. It is to make AI adoption governable without asking every team to maintain its own invisible inventory.”

## Suggested 5-minute run of show

1. Slide 1: Problem and accountability gap — 35 seconds.
2. Slide 2: Control loop — 30 seconds.
3. Slide 3: Agent Registry demonstration — 90 seconds.
4. Slide 4: Governance resolution demonstration — 60 seconds.
5. Slide 5: Risk control decision — 45 seconds.
6. Slide 6: Analytics or supply context — 30 seconds.
7. Slide 7: Scope and next step — 50 seconds.

## Demo safety checklist

- Launch the dashboard before presenting; do not install dependencies live.
- Use the seeded synthetic database and label it clearly as synthetic.
- Prefer the dashboard over `python3 demo.py` for the main demo.
- Do not demonstrate the placeholder webhook URL as a live integration.
- If showing the CLI, disable or replace outbound webhook dispatch first; otherwise it retries against the example endpoint.
- Keep one backup screenshot or screen recording of Agent Registry and Governance in case Streamlit state or network conditions fail.
