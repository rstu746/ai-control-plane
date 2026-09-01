"""
Seed the dashboard database with synthetic demo data.

Called by app.py on first launch when the DB is empty. Runs the same
pipeline as demo.py so all five dashboard pages have data to display.

The seed is idempotent — calling it on an already-populated DB is safe
(upserts overwrite, inserts are skipped on conflict).
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from connectors.synthetic import SyntheticConnector
from core.agent_registry import AgentRegistry
from core.alerting import AlertDispatcher
from core.burn_rate import summarize_burn_rate
from core.classifier import classify
from core.models import (
    AgentManifest,
    AgentStatus,
    AlertEventType,
    AlertRule,
    CapacityPool,
    ClassificationResult,
    ControlFlowType,
    DemandDriver,
    ModelStatus,
    Severity,
    SourceApp,
)
from core.recommender import recommend_for_pool
from core.risk import assess_risk
from core.trends import TrendsEngine
from core.workflow import WorkflowEngine
from storage.sqlite import SqliteBackend


def _build_pools(start: datetime, days: int) -> list[CapacityPool]:
    end = start + timedelta(days=days)
    return [
        CapacityPool(
            pool_id="pool-gpt4o-eastus",
            model="gpt-4o",
            model_family="gpt-4o",
            version_number=1,
            region="eastus",
            ptu_quantity=20,
            cost_usd=16_000,
            start_date=start,
            end_date=end,
            demand_driver=DemandDriver.HUMAN_DRIVEN,
            status=ModelStatus.ESTABLISHED,
            throughput_capacity_tokens_per_hour=4_500,
        ),
        CapacityPool(
            pool_id="pool-sonnet-eastus",
            model="claude-sonnet-4-6",
            model_family="claude-sonnet",
            version_number=6,
            region="eastus",
            ptu_quantity=18,
            cost_usd=15_000,
            start_date=start,
            end_date=end,
            demand_driver=DemandDriver.HUMAN_DRIVEN,
            status=ModelStatus.ESTABLISHED,
            throughput_capacity_tokens_per_hour=3_800,
        ),
        CapacityPool(
            pool_id="pool-haiku-eastus",
            model="claude-haiku-4-5",
            model_family="claude-haiku",
            version_number=5,
            region="eastus",
            ptu_quantity=6,
            cost_usd=3_600,
            start_date=start,
            end_date=end,
            demand_driver=DemandDriver.HUMAN_DRIVEN,
            status=ModelStatus.ESTABLISHED,
            throughput_capacity_tokens_per_hour=1_100,
        ),
    ]


_DEMO_AGENTS = [
    {
        "agent_id": "agent-hr-knowledge",
        "name": "HR Knowledge Agent",
        "owner_id": "owner-hr",
        "team_id": "team-platform",
        "manifest": AgentManifest(
            tool_list=["search", "policy-retriever"],
            data_scope="invoker_only",
            execution_rights=False,
            credentials_held=False,
            can_modify_repos_or_pipelines=False,
            can_modify_other_agents=False,
            writes_to_system_of_record=False,
            reaches_external_party=False,
            distributes_output_autonomously=False,
            grounding_includes_confidential_of_others=False,
            data_sources=["hr-policy-docs"],
        ),
        "control_flow_type": ControlFlowType.SCRIPTED,
    },
    {
        "agent_id": "agent-dpia",
        "name": "DPIA Automation Agent",
        "owner_id": "owner-platform",
        "team_id": "team-platform",
        "manifest": AgentManifest(
            tool_list=["sharepoint-writer", "risk-scorer"],
            data_scope="invoker_only",
            execution_rights=False,
            credentials_held=False,
            can_modify_repos_or_pipelines=False,
            can_modify_other_agents=False,
            writes_to_system_of_record=True,
            reaches_external_party=False,
            distributes_output_autonomously=False,
            grounding_includes_confidential_of_others=False,
            data_sources=["project-metadata"],
        ),
        "control_flow_type": ControlFlowType.SCRIPTED,
    },
    {
        "agent_id": "agent-claude-code",
        "name": "Claude Code Agent",
        "owner_id": "owner-engineering",
        "team_id": "team-engineering",
        "manifest": AgentManifest(
            tool_list=["bash", "file-write", "git"],
            data_scope="beyond_invoker",
            execution_rights=True,
            credentials_held=True,
            can_modify_repos_or_pipelines=True,
            can_modify_other_agents=False,
            writes_to_system_of_record=True,
            reaches_external_party=False,
            distributes_output_autonomously=False,
            grounding_includes_confidential_of_others=False,
            data_sources=["source-repos", "ci-pipeline"],
        ),
        "control_flow_type": ControlFlowType.AGENT,
    },
    {
        "agent_id": "agent-customer-chat",
        "name": "Customer Support Agent",
        "owner_id": "owner-cx",
        "team_id": "team-cx",
        "manifest": AgentManifest(
            tool_list=["crm-read", "ticket-write", "email"],
            data_scope="beyond_invoker",
            execution_rights=False,
            credentials_held=False,
            can_modify_repos_or_pipelines=False,
            can_modify_other_agents=False,
            writes_to_system_of_record=True,
            reaches_external_party=True,
            distributes_output_autonomously=True,
            grounding_includes_confidential_of_others=True,
            data_sources=["crm", "ticket-system"],
        ),
        "control_flow_type": ControlFlowType.SCRIPTED,
    },
    {
        "agent_id": "agent-incomplete",
        "name": "Data Pipeline Agent (incomplete)",
        "owner_id": "owner-data",
        "team_id": "team-data",
        "manifest": AgentManifest(
            tool_list=["sql-runner"],
            # data_scope intentionally missing — triggers classification request
        ),
        "control_flow_type": ControlFlowType.SCRIPTED,
    },
]


def is_seeded(db: SqliteBackend) -> bool:
    """Return True if the DB already has agent data."""
    return len(db.get_agents()) > 0


def run_seed(db: SqliteBackend) -> None:
    """Populate the DB with synthetic usage events, demo agents, pools, and trends."""
    days = 28
    since = datetime.now() - timedelta(days=days)

    # --- Usage events ---
    connector = SyntheticConnector(days=days)
    events = connector.pull(since=since)
    db.insert_usage_events(events)

    # --- Capacity pools ---
    pools = _build_pools(start=since, days=days)
    for pool in pools:
        db.upsert_capacity_pool(pool)

    # --- Alert rules ---
    db.upsert_alert_rule(AlertRule(
        rule_id="platform-fallback",
        webhook_url="https://example.com/ai-control-plane/alerts",
        event_types=list(AlertEventType),
        min_severity=Severity.LOW,
        team_id=None,
        description="Platform-wide fallback (demo — replace with your webhook URL)",
    ))

    # --- Agent registry + classification ---
    registry = AgentRegistry(storage=db)
    dispatcher = AlertDispatcher(storage=db)
    engine = WorkflowEngine(storage=db, alerting=dispatcher)

    # Discover agents from usage events
    registry.process_usage_events(events)

    # Register and classify demo agents
    for demo in _DEMO_AGENTS:
        agent = registry.register_agent(
            agent_id=demo["agent_id"],
            name=demo["name"],
            owner_id=demo["owner_id"],
            team_id=demo["team_id"],
            source_platform=SourceApp.AI_GATEWAY,
            manifest=demo["manifest"],
        )

        result = classify(
            agent,
            demo["manifest"],
            control_flow_type=demo["control_flow_type"],
        )

        if isinstance(result, ClassificationResult):
            agent.tier = result.tier
            agent.functional_role = result.functional_role
            agent.control_flow_type = result.control_flow_type
            agent.status = AgentStatus.ACTIVE
            agent.last_classified_at = result.classified_at
            agent.last_seen_at = datetime.now() - timedelta(days=1)
            db.upsert_agent(agent)
            db.insert_classification_result(result)
            db.upsert_agent_manifest(demo["agent_id"], demo["manifest"])

            if demo["control_flow_type"] == ControlFlowType.AGENT:
                engine.open_holistic_review(agent)
        else:
            # ClassificationRequest — open workflow item
            engine.open_classification_request(result, agent)

    # --- Trend snapshots ---
    trends = TrendsEngine(storage=db)
    # Compute a snapshot per day in the window
    for day_offset in range(days):
        day = since + timedelta(days=day_offset)
        day_events = [e for e in events if e.timestamp.date() == day.date()]
        if day_events:
            trends.compute_daily_snapshots(day_events, date=day)
    trends.compute_week_over_week()
