"""
Canonical data models for the AI Control Plane.

Every source system (AI gateway, GitHub Copilot, M365 Copilot, Copilot
Studio, M365 Cowork, Snowflake, Databricks, LangSmith, Dynatrace, ...) is
normalized into a UsageEvent. USD is the common unit across all sources;
tokens are a first-class unit only where they natively exist (the gateway),
and are the unit the Supply Chain Planner reasons about directly.

Note on naming: "Copilot" alone is ambiguous across this stack — GitHub
Copilot, M365 Copilot, Copilot Studio, and M365 Cowork are distinct
products with distinct billing models (seat-based vs. usage-based) and
distinct usage APIs. SourceApp values are always fully qualified
(GITHUB_COPILOT, not COPILOT) to keep that explicit end to end.

Agent classification follows the tier framework defined in the governance
documentation. Tiers are assigned mechanically from an agent's manifest
(what it can reach and do), not from the platform it runs on. See
core/classifier.py for the rule engine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


# ---------------------------------------------------------------------------
# Source apps
# ---------------------------------------------------------------------------

class SourceApp(str, Enum):
    AI_GATEWAY = "ai_gateway"
    GITHUB_COPILOT = "github_copilot"
    M365_COPILOT = "m365_copilot"
    COPILOT_STUDIO = "copilot_studio"
    M365_COWORK = "m365_cowork"
    SNOWFLAKE = "snowflake"
    DATABRICKS = "databricks"
    AZURE_AI_FOUNDRY = "azure_ai_foundry"
    ANTHROPIC = "anthropic"
    LANGSMITH = "langsmith"
    DYNATRACE = "dynatrace"
    SYNTHETIC = "synthetic"
    UNKNOWN = "unknown"


class ResourceType(str, Enum):
    TOKENS = "tokens"
    SEAT = "seat"
    COMPUTE_CREDIT = "compute_credit"


# ---------------------------------------------------------------------------
# Capacity pool — existing supply chain planner enums
# ---------------------------------------------------------------------------

class DemandDriver(str, Enum):
    """What actually generates demand against a capacity pool. Coding
    assistants are human-paced (bound by headcount and working hours);
    agents can be machine-paced (continuous, scheduled, or event-triggered,
    decoupled from headcount entirely). Burn rate shape and forecasting
    assumptions differ meaningfully between the two."""

    HUMAN_DRIVEN = "human_driven"   # e.g. coding assistants
    AGENT_DRIVEN = "agent_driven"   # e.g. autonomous/scheduled agents


class ModelStatus(str, Enum):
    """Lifecycle state for a model within a pool. See core/model_lifecycle.py
    for the state transitions and auto-deprecation logic."""

    CANDIDATE = "candidate"       # new on the gateway, no pool/traffic yet
    BENCHMARKED = "benchmarked"   # preset task suite run, projected demand known
    PILOTED = "piloted"           # small pool provisioned, blending projection + real usage
    ESTABLISHED = "established"   # full statistical reorder logic applies
    DEPRECATED = "deprecated"     # superseded; no new reorder recommendations


# ---------------------------------------------------------------------------
# Agent classification enums
# ---------------------------------------------------------------------------

class AgentTier(str, Enum):
    """Governance tier. Assigned mechanically from the agent manifest.
    Platform is context, not a classification input — the same test applies
    regardless of whether the agent runs in Copilot Studio, AI Foundry, or a
    custom gateway build."""

    TIER_1 = "tier_1"           # Contained: reads own entitlements, reversible actions only
    TIER_2 = "tier_2"           # Crossing: reaches beyond invoker, writes to systems of record
    TIER_3 = "tier_3"           # Executing: runs code, holds credentials, modifies infra/agents
    UNCLASSIFIED = "unclassified"  # Manifest incomplete; classification pending


class FunctionalRole(str, Enum):
    """What the agent does, independent of what it can reach (which is tier)."""

    STRIKER = "striker"      # Does the work: retrieves, drafts, acts
    SUMMONER = "summoner"    # Delegates to named sub-agents (orchestrator)
    ARTIFICER = "artificer"  # Builds or modifies agents, code, or infrastructure
    WATCHER = "watcher"      # Monitors and observes — read-only by design
    UNKNOWN = "unknown"


class ControlFlowType(str, Enum):
    """How the agent decides what to do next.

    Scripted agents are deterministic and manifest-describable — every action
    is declared up front, so tiers can be assigned mechanically and the fast
    path to tier 1 is available. Agent-type builds choose their own tool,
    MCP server, or sub-agent at runtime; individually safe capabilities can
    combine into something none of them exposes alone, so a holistic review
    WorkflowItem is always raised and the fast path is never available."""

    SCRIPTED = "scripted"  # Deterministic; tiered mechanically
    AGENT = "agent"        # Runtime tool/MCP selection; always holistic review


class CostConstruct(str, Enum):
    """Commercial billing model, independent of tier."""

    ALL_INCLUSIVE = "all_inclusive"              # Per-user seat, unlimited within licence
    PAY_AS_YOU_GO_TOOL = "pay_as_you_go_tool"   # Platform consumption-billed by tool call
    PAY_AS_YOU_GO_AGENT = "pay_as_you_go_agent"  # Budget attaches to the agent, not a user seat


class RegulatoryFlag(str, Enum):
    """Regulatory obligations that attach per-agent. Flags carry the
    obligation; tiers carry the governance machinery. A new regulation adds a
    flag, not a new tier."""

    EXTERNAL_FACING = "external_facing"            # AI Act transparency, consumer protection
    PERSONAL_DATA = "personal_data"                # DPIA, data residency, retention constraints
    FINANCIALLY_MATERIAL = "financially_material"  # Evidenced, testable control design required
    MARKET_FACING = "market_facing"                # REMIT and MAR exposure


class AgentStatus(str, Enum):
    DISCOVERED = "discovered"          # Seen in logs; not yet registered by an owner
    REGISTERED = "registered"          # Owner has registered; classification not yet run
    UNCLASSIFIED = "unclassified"      # Classification attempted; manifest too sparse
    PENDING_REVIEW = "pending_review"  # Holistic review WorkflowItem open
    ACTIVE = "active"                  # Classified and approved
    DORMANT = "dormant"                # No usage events in the configured dormancy window
    DEPRECATED = "deprecated"          # Decommissioned


class DiscoverySource(str, Enum):
    GATEWAY_LOGS = "gateway_logs"          # Inferred from usage events with unknown actor
    SELF_REGISTRATION = "self_registration"  # Owner explicitly registered via API/manifest
    CONNECTOR = "connector"                # Inferred from platform connector metadata


# ---------------------------------------------------------------------------
# Workflow items — classification and holistic review tracking
# ---------------------------------------------------------------------------

class WorkflowItemType(str, Enum):
    CLASSIFICATION_REQUEST = "classification_request"  # Manifest too sparse to classify
    HOLISTIC_REVIEW = "holistic_review"                # Agent-type build; needs combined review


class WorkflowItemStatus(str, Enum):
    OPEN = "open"
    REMINDED = "reminded"    # At least one reminder sent
    ESCALATED = "escalated"  # Manager/platform owner notified
    RESOLVED = "resolved"    # Owner completed the required information or review
    CAPPED = "capped"        # Token cap applied; still waiting for resolution


# ---------------------------------------------------------------------------
# Risk assessment
# ---------------------------------------------------------------------------

class AutonomyControl(str, Enum):
    """Recommended control from the blast-radius × reversibility matrix."""

    LET_RUN = "let_run"          # Low blast radius, reversible — log and sample only
    DETECT_FAST = "detect_fast"  # Low blast radius, irreversible — undo needs detection
    RATE_LIMIT = "rate_limit"    # High blast radius, reversible — cheap alone, systemic at scale
    HUMAN_GATE = "human_gate"    # High blast radius, irreversible — no autonomy without approval


# ---------------------------------------------------------------------------
# Alerting
# ---------------------------------------------------------------------------

class AlertEventType(str, Enum):
    CLASSIFICATION_REQUEST = "classification_request"
    HOLISTIC_REVIEW_REQUIRED = "holistic_review_required"
    WORKFLOW_REMINDER = "workflow_reminder"
    WORKFLOW_ESCALATED = "workflow_escalated"
    TOKEN_CAP_APPLIED = "token_cap_applied"
    RISK_THRESHOLD_BREACH = "risk_threshold_breach"
    RECLASSIFICATION_TRIGGERED = "reclassification_triggered"
    BUDGET_BREACH = "budget_breach"
    CAPACITY_REORDER = "capacity_reorder"
    UNKNOWN_AGENT_DETECTED = "unknown_agent_detected"
    DORMANT_AGENT_DETECTED = "dormant_agent_detected"
    MODEL_ADOPTION_SHIFT = "model_adoption_shift"
    REGULATORY_FLAG_RAISED = "regulatory_flag_raised"


class Severity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


# ---------------------------------------------------------------------------
# Core usage event (extended with optional agent/workflow linkage)
# ---------------------------------------------------------------------------

@dataclass
class UsageEvent:
    timestamp: datetime
    actor_id: str
    team_id: str
    source_app: SourceApp
    resource_type: ResourceType
    quantity: float
    unit_cost_usd: float
    model: str | None = None
    agent_id: str | None = None      # populated when the actor is a known agent
    workflow_id: str | None = None   # populated when part of a tracked workflow
    metadata: dict = field(default_factory=dict)

    @property
    def cost_usd(self) -> float:
        return round(self.quantity * self.unit_cost_usd, 6)


# ---------------------------------------------------------------------------
# Capacity pool (unchanged from supply chain planner)
# ---------------------------------------------------------------------------

@dataclass
class CapacityPool:
    """A pre-purchased block of model capacity (e.g. Azure PTUs)."""

    pool_id: str
    model: str
    model_family: str
    version_number: int
    region: str
    ptu_quantity: int
    cost_usd: float
    start_date: datetime
    end_date: datetime
    demand_driver: DemandDriver
    status: ModelStatus = ModelStatus.ESTABLISHED
    # Tokens/hour this pool can sustain. In real deployments this comes from
    # the provider's PTU->throughput conversion; treated as a config input here.
    throughput_capacity_tokens_per_hour: float = 0.0


@dataclass
class Recommendation:
    pool_id: str
    action: str  # "increase" | "decrease" | "reallocate" | "hold"
    reason: str
    urgency: str  # "low" | "medium" | "high"
    projected_stockout_date: datetime | None = None


# ---------------------------------------------------------------------------
# Budget / user models
# ---------------------------------------------------------------------------

@dataclass
class Role:
    role_id: str
    role_name: str
    monthly_budget_usd: float


@dataclass
class User:
    user_id: str
    display_name: str
    team_id: str
    role_id: str


@dataclass
class BudgetOverride:
    """A one-off budget for a specific user in a specific month — e.g. a
    mid-month role change or a manually approved higher allowance. Also used
    by the workflow engine to apply a token cap when a WorkflowItem is not
    resolved within the escalation window. Takes precedence over the user's
    role-based budget for that month only."""

    user_id: str
    year_month: str  # "YYYY-MM"
    override_budget_usd: float
    reason: str


# ---------------------------------------------------------------------------
# Agent manifest and agent entity
# ---------------------------------------------------------------------------

@dataclass
class AgentManifest:
    """What an agent can reach and do — the input the classifier reasons about.

    Every capability flag has three states:
      True  — confirmed present
      False — confirmed absent
      None  — not yet declared (counts against manifest completeness)

    The classifier will not run until completeness is above the configured
    threshold. Below that, a ClassificationRequest WorkflowItem is raised."""

    # Tool and integration surface
    tool_list: list[str] = field(default_factory=list)
    mcp_servers: list[str] = field(default_factory=list)
    data_sources: list[str] = field(default_factory=list)
    sub_agent_ids: list[str] = field(default_factory=list)
    external_targets: list[str] = field(default_factory=list)

    # Tier 3 triggers — any True puts the agent at tier 3
    execution_rights: bool | None = None           # Executes code
    credentials_held: bool | None = None           # Holds secrets or long-lived credentials
    can_modify_repos_or_pipelines: bool | None = None
    can_modify_other_agents: bool | None = None

    # Tier 2 triggers — any True puts the agent at tier 2 (if not already tier 3)
    writes_to_system_of_record: bool | None = None
    reaches_external_party: bool | None = None
    distributes_output_autonomously: bool | None = None
    grounding_includes_confidential_of_others: bool | None = None

    # Data scope — "invoker_only" keeps the agent at tier 1; "beyond_invoker" is a tier 2 trigger
    data_scope: str | None = None  # "invoker_only" | "beyond_invoker"

    last_updated: datetime | None = None

    @property
    def completeness(self) -> float:
        """Fraction of capability flags declared (not None). Used to decide
        whether the manifest is sufficient for classification."""
        flags = [
            self.execution_rights,
            self.credentials_held,
            self.can_modify_repos_or_pipelines,
            self.can_modify_other_agents,
            self.writes_to_system_of_record,
            self.reaches_external_party,
            self.distributes_output_autonomously,
            self.grounding_includes_confidential_of_others,
            self.data_scope,
        ]
        declared = sum(1 for f in flags if f is not None)
        return declared / len(flags)

    @property
    def missing_capability_flags(self) -> list[str]:
        """Names of capability flags that have not yet been declared."""
        checks = {
            "execution_rights": self.execution_rights,
            "credentials_held": self.credentials_held,
            "can_modify_repos_or_pipelines": self.can_modify_repos_or_pipelines,
            "can_modify_other_agents": self.can_modify_other_agents,
            "writes_to_system_of_record": self.writes_to_system_of_record,
            "reaches_external_party": self.reaches_external_party,
            "distributes_output_autonomously": self.distributes_output_autonomously,
            "grounding_includes_confidential_of_others": self.grounding_includes_confidential_of_others,
            "data_scope": self.data_scope,
        }
        return [k for k, v in checks.items() if v is None]


@dataclass
class AgentManifestFragment:
    """Partial manifest data inferred from a connector or gateway logs.
    The agent registry merges fragments from multiple sources into a full
    AgentManifest. Later fragments override earlier ones for the same field."""

    agent_id: str
    source: DiscoverySource
    observed_at: datetime

    tool_list: list[str] = field(default_factory=list)
    mcp_servers: list[str] = field(default_factory=list)
    data_sources: list[str] = field(default_factory=list)
    sub_agent_ids: list[str] = field(default_factory=list)
    external_targets: list[str] = field(default_factory=list)

    execution_rights: bool | None = None
    credentials_held: bool | None = None
    can_modify_repos_or_pipelines: bool | None = None
    can_modify_other_agents: bool | None = None
    writes_to_system_of_record: bool | None = None
    reaches_external_party: bool | None = None
    distributes_output_autonomously: bool | None = None
    grounding_includes_confidential_of_others: bool | None = None
    data_scope: str | None = None


@dataclass
class Agent:
    """A registered or discovered AI agent."""

    agent_id: str
    name: str
    owner_id: str
    team_id: str
    source_platform: SourceApp
    status: AgentStatus
    discovery_source: DiscoverySource
    discovered_at: datetime

    # Classification results — populated after classifier runs
    tier: AgentTier = AgentTier.UNCLASSIFIED
    functional_role: FunctionalRole = FunctionalRole.UNKNOWN
    control_flow_type: ControlFlowType | None = None
    cost_construct: CostConstruct | None = None
    regulatory_flags: list[RegulatoryFlag] = field(default_factory=list)

    # Escalation webhook — team-specific, resolved from AlertRule by team_id
    # Stored on the agent for quick lookup during workflow escalation
    escalation_webhook_url: str | None = None

    # Tracking
    last_seen_at: datetime | None = None
    last_classified_at: datetime | None = None
    manifest: AgentManifest | None = None

    # Token cap applied by workflow engine at escalation T+21 (Phase A: internal only)
    monthly_token_cap: float | None = None  # None = no cap applied


@dataclass
class Workflow:
    """A multi-agent pipeline or DAG."""

    workflow_id: str
    name: str
    orchestrator_agent_id: str
    participating_agent_ids: list[str] = field(default_factory=list)
    description: str = ""
    created_at: datetime = field(default_factory=datetime.now)


# ---------------------------------------------------------------------------
# Classification output
# ---------------------------------------------------------------------------

@dataclass
class ClassificationResult:
    agent_id: str
    tier: AgentTier
    functional_role: FunctionalRole
    control_flow_type: ControlFlowType
    cost_construct: CostConstruct | None
    regulatory_flags: list[RegulatoryFlag]
    confidence: float          # manifest completeness at time of classification (0.0–1.0)
    classified_at: datetime
    classified_by: str = "auto"  # "auto" | "manual" | "holistic_review"
    notes: str = ""


@dataclass
class ClassificationRequest:
    """Raised when the manifest is too sparse to classify. Drives a
    ClassificationRequest WorkflowItem."""

    request_id: str
    agent_id: str
    missing_fields: list[str]   # which manifest fields are missing
    raised_at: datetime
    resolved_at: datetime | None = None


# ---------------------------------------------------------------------------
# Workflow item — tracks resolution of classification requests and holistic reviews
# ---------------------------------------------------------------------------

@dataclass
class WorkflowItem:
    item_id: str
    agent_id: str
    item_type: WorkflowItemType
    status: WorkflowItemStatus
    raised_at: datetime
    due_at: datetime   # T+21 by default; after this the token cap fires

    resolved_at: datetime | None = None
    reminder_count: int = 0
    escalation_target_webhook: str | None = None  # team's registered escalation webhook
    missing_fields: list[str] = field(default_factory=list)
    notes: str = ""


# ---------------------------------------------------------------------------
# Risk assessment
# ---------------------------------------------------------------------------

@dataclass
class RiskAssessment:
    agent_id: str
    autonomy_control: AutonomyControl
    blast_radius: str   # "low" | "high"
    reversibility: str  # "reversible" | "irreversible"
    regulatory_flags: list[RegulatoryFlag]
    assessed_at: datetime
    notes: str = ""


# ---------------------------------------------------------------------------
# Audit event — immutable; written to Azure Blob Storage
# ---------------------------------------------------------------------------

@dataclass
class AuditEvent:
    """Append-only audit record. Written to Azure Blob Storage (never mutated).
    Also stored in the primary DB for queryability, but Blob is authoritative."""

    event_id: str
    timestamp: datetime
    event_type: str        # e.g. "agent_classified" | "tier_changed" | "cap_applied"
    agent_id: str | None
    actor_id: str | None   # human or system that triggered this event
    before_state: dict
    after_state: dict
    source: str = "ai-control-plane"


# ---------------------------------------------------------------------------
# Alerting — webhook rules and envelope
# ---------------------------------------------------------------------------

@dataclass
class AlertRule:
    """A registered webhook destination. team_id=None is the platform-wide
    fallback, used when an agent's team has no registered rule or when the
    agent itself has not yet been attributed to a team."""

    rule_id: str
    webhook_url: str
    event_types: list[AlertEventType]
    min_severity: Severity
    team_id: str | None = None   # None = platform-wide fallback
    active: bool = True
    description: str = ""


@dataclass
class WebhookEnvelope:
    """Typed, versioned envelope for all outbound webhook calls.

    schema_version is incremented on any breaking change to the payload
    structure. Consumers should check this before parsing payload.
    event_id is a UUID usable as an idempotency key."""

    schema_version: str      # "1.0"
    event_id: str            # UUID
    event_type: AlertEventType
    severity: Severity
    timestamp: datetime
    source: str              # "ai-control-plane"
    payload: dict            # typed per event_type — see docs/webhook-schema.md


@dataclass
class AlertHistory:
    """Record of a dispatched webhook call."""

    history_id: str
    rule_id: str
    event_id: str            # WebhookEnvelope.event_id
    event_type: AlertEventType
    dispatched_at: datetime
    http_status: int | None  # None if dispatch failed before getting a response
    success: bool
    error_message: str | None = None


# ---------------------------------------------------------------------------
# Trends
# ---------------------------------------------------------------------------

@dataclass
class TrendSnapshot:
    """Daily aggregation snapshot. One row per (date, dimension combination).
    Dimensions with None mean 'all' — e.g. model=None means across all models."""

    snapshot_id: str
    snapshot_date: datetime

    # Dimension filters — any can be None (meaning "aggregate across all")
    model: str | None
    agent_id: str | None
    team_id: str | None
    source_app: SourceApp | None
    demand_driver: DemandDriver | None

    # Metrics
    total_tokens: float
    total_cost_usd: float
    event_count: int
    active_actors: int         # distinct actor_ids seen this day
    week_over_week_delta: float | None = None   # populated on weekly roll-up
