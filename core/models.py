"""
Canonical data models for the AI Control Plane.

Every source system (AI gateway, GitHub Copilot, M365 Copilot, Copilot
Studio, M365 Cowork, Snowflake, Databricks, ...) is normalized into a
UsageEvent. USD is the common unit across all sources; tokens are a
first-class unit only where they natively exist (the gateway), and are
the unit the Supply Chain Planner reasons about directly.

Note on naming: "Copilot" alone is ambiguous across this stack — GitHub
Copilot, M365 Copilot, Copilot Studio, and M365 Cowork are distinct
products with distinct billing models (seat-based vs. usage-based) and
distinct usage APIs. SourceApp values are always fully qualified
(GITHUB_COPILOT, not COPILOT) to keep that explicit end to end.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class SourceApp(str, Enum):
    AI_GATEWAY = "ai_gateway"
    GITHUB_COPILOT = "github_copilot"
    M365_COPILOT = "m365_copilot"
    COPILOT_STUDIO = "copilot_studio"
    M365_COWORK = "m365_cowork"
    SNOWFLAKE = "snowflake"
    DATABRICKS = "databricks"
    SYNTHETIC = "synthetic"


class ResourceType(str, Enum):
    TOKENS = "tokens"
    SEAT = "seat"
    COMPUTE_CREDIT = "compute_credit"


class DemandDriver(str, Enum):
    """What actually generates demand against a capacity pool. Coding
    assistants are human-paced (bound by headcount and working hours);
    agents can be machine-paced (continuous, scheduled, or event-triggered,
    decoupled from headcount entirely). Burn rate shape and forecasting
    assumptions differ meaningfully between the two."""

    HUMAN_DRIVEN = "human_driven"   # e.g. coding assistants
    AGENT_DRIVEN = "agent_driven"    # e.g. autonomous/scheduled agents


class ModelStatus(str, Enum):
    """Lifecycle state for a model within a pool. See core/model_lifecycle.py
    for the state transitions and auto-deprecation logic."""

    CANDIDATE = "candidate"      # new on the gateway, no pool/traffic yet
    BENCHMARKED = "benchmarked"   # preset task suite run, projected demand known
    PILOTED = "piloted"           # small pool provisioned, blending projection + real usage
    ESTABLISHED = "established"    # full statistical reorder logic applies
    DEPRECATED = "deprecated"      # superseded; no new reorder recommendations


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
    metadata: dict = field(default_factory=dict)

    @property
    def cost_usd(self) -> float:
        return round(self.quantity * self.unit_cost_usd, 6)


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
    mid-month role change (apply the higher tier for that month) or a
    manually approved higher allowance. Takes precedence over the user's
    role-based budget for that month only."""

    user_id: str
    year_month: str  # "YYYY-MM"
    override_budget_usd: float
    reason: str
