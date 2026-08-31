"""
Runs the full pipeline end-to-end against synthetic data — both the original
supply chain planner and the new agent governance pipeline:

  1. Supply chain:   synthetic usage → burn rate → reorder → recommendation
  2. Agent pipeline: discover agents → infer manifest → classify → risk assess
                     → workflow items → trends snapshot

Run with zero setup:

    python demo.py
"""

from __future__ import annotations

from datetime import datetime, timedelta

from connectors.synthetic import SyntheticConnector
from core.agent_registry import AgentRegistry
from core.alerting import AlertDispatcher
from core.burn_rate import summarize_burn_rate
from core.classifier import classify
from core.models import (
    AgentManifest,
    AgentStatus,
    AgentTier,
    AlertEventType,
    AlertRule,
    CapacityPool,
    ClassificationRequest,
    ClassificationResult,
    ControlFlowType,
    CostConstruct,
    DemandDriver,
    ModelStatus,
    Severity,
    SourceApp,
)
from core.recommender import recommend_for_pool
from core.risk import assess_risk, detect_burn_rate_spike
from core.trends import TrendsEngine
from core.workflow import WorkflowEngine
from storage.sqlite import SqliteBackend

PTU_LEAD_TIME_DAYS = 5


# ------------------------------------------------------------------
# Supply chain demo (unchanged from original)
# ------------------------------------------------------------------

def build_demo_pools(start: datetime, days: int) -> list[CapacityPool]:
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


def run_supply_chain_demo(events, pools):
    print("\n" + "=" * 100)
    print("SUPPLY CHAIN PLANNER — Burn Rate & Reorder Recommendations")
    print("=" * 100)
    print(
        f"{'Pool':<25} {'Model':<22} {'Avg/day':>12} {'Remaining':>12} "
        f"{'Days left':>10} {'Action':>10} {'Urgency':>8}"
    )
    print("-" * 100)

    for pool in pools:
        summary = summarize_burn_rate(pool, events, window_days=14)
        rec = recommend_for_pool(pool, summary, lead_time_days=PTU_LEAD_TIME_DAYS)
        print(
            f"{pool.pool_id:<25} {pool.model:<22} "
            f"{summary.avg_daily_tokens:>12,.0f} "
            f"{summary.tokens_remaining:>12,.0f} "
            f"{summary.days_of_supply_remaining:>10.1f} "
            f"{rec.action:>10} {rec.urgency:>8}"
        )
        print(f"   -> {rec.reason}")
        if rec.projected_stockout_date:
            print(f"   -> Projected stockout: {rec.projected_stockout_date:%Y-%m-%d}")
        print()


# ------------------------------------------------------------------
# Agent governance demo
# ------------------------------------------------------------------

def run_agent_governance_demo(events, db: SqliteBackend):
    print("\n" + "=" * 100)
    print("AGENT GOVERNANCE — Discovery, Classification & Risk Assessment")
    print("=" * 100)

    registry = AgentRegistry(storage=db)
    dispatcher = AlertDispatcher(storage=db)

    # Register a platform-wide fallback webhook (demo — points nowhere real)
    db.upsert_alert_rule(AlertRule(
        rule_id="platform-fallback",
        webhook_url="https://example.com/ai-control-plane/alerts",
        event_types=list(AlertEventType),
        min_severity=Severity.LOW,
        team_id=None,
        description="Platform-wide fallback (demo)",
    ))

    # 1. Discover agents from synthetic usage events
    newly_discovered = registry.process_usage_events(events)
    all_agents = db.get_agents()
    print(f"\nDiscovered {len(newly_discovered)} new agents from {len(events):,} usage events")
    print(f"Total agents in registry: {len(all_agents)}")

    # 2. Register two demo agents with different manifests (simulating real builds)
    demo_agents = [
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
            "expected_tier": "tier_1",
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
                writes_to_system_of_record=True,   # Tier 2 trigger
                reaches_external_party=False,
                distributes_output_autonomously=False,
                grounding_includes_confidential_of_others=False,
                data_sources=["project-metadata"],
            ),
            "control_flow_type": ControlFlowType.SCRIPTED,
            "expected_tier": "tier_2",
        },
        {
            "agent_id": "agent-claude-code",
            "name": "Claude Code Agent",
            "owner_id": "owner-engineering",
            "team_id": "team-engineering",
            "manifest": AgentManifest(
                tool_list=["bash", "file-write", "git"],
                data_scope="beyond_invoker",
                execution_rights=True,       # Tier 3 trigger
                credentials_held=True,       # Tier 3 trigger
                can_modify_repos_or_pipelines=True,  # Tier 3 trigger
                can_modify_other_agents=False,
                writes_to_system_of_record=True,
                reaches_external_party=False,
                distributes_output_autonomously=False,
                grounding_includes_confidential_of_others=False,
                data_sources=["source-repos", "ci-pipeline"],
            ),
            "control_flow_type": ControlFlowType.AGENT,
            "expected_tier": "tier_3",
        },
        {
            "agent_id": "agent-incomplete",
            "name": "Incomplete Agent (missing manifest)",
            "owner_id": "owner-unknown",
            "team_id": "team-data",
            "manifest": AgentManifest(
                tool_list=["search"],
                # data_scope NOT declared — will trigger classification request
            ),
            "control_flow_type": ControlFlowType.SCRIPTED,
            "expected_tier": "unclassified",
        },
    ]

    print("\n" + "-" * 100)
    print(f"{'Agent':<30} {'Expected':>12} {'Result':>12} {'Role':>12} {'Confidence':>12}")
    print("-" * 100)

    engine = WorkflowEngine(storage=db, alerting=dispatcher)

    for demo in demo_agents:
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
            # Update agent with classification
            agent.tier = result.tier
            agent.functional_role = result.functional_role
            agent.control_flow_type = result.control_flow_type
            agent.status = AgentStatus.ACTIVE
            agent.last_classified_at = result.classified_at
            db.upsert_agent(agent)
            db.insert_classification_result(result)
            db.upsert_agent_manifest(agent.agent_id, demo["manifest"])

            # Risk assessment
            risk = assess_risk(agent, demo["manifest"])

            tier_str = result.tier.value
            role_str = result.functional_role.value
            conf_str = f"{result.confidence:.0%}"

            print(
                f"{demo['name']:<30} {demo['expected_tier']:>12} {tier_str:>12} "
                f"{role_str:>12} {conf_str:>12}"
            )
            flags_str = ", ".join(f.value for f in result.regulatory_flags) or "none"
            print(f"   Regulatory flags: {flags_str}")
            print(f"   Autonomy control: {risk.autonomy_control.value}  "
                  f"(blast={risk.blast_radius}, reversibility={risk.reversibility})")

            # Agent-type needs holistic review
            if result.control_flow_type == ControlFlowType.AGENT:
                engine.open_holistic_review(agent)
                print(f"   [WORKFLOW] Holistic review opened — agent-type control flow")

        else:
            # Classification request
            print(
                f"{demo['name']:<30} {demo['expected_tier']:>12} {'unclassified':>12} "
                f"{'—':>12} {'—':>12}"
            )
            print(f"   [WORKFLOW] Classification request: missing {result.missing_fields}")
            engine.open_classification_request(result, agent)

        print()

    # 3. Burn rate spike detection on synthetic events (agent-attributed)
    print("-" * 100)
    print("BURN RATE SPIKE DETECTION (agent-level)")
    print("-" * 100)
    # Tag a subset of events with an agent_id to simulate agent-driven traffic
    for e in events[:100]:
        e.agent_id = "agent-dpia"
    spike = detect_burn_rate_spike("agent-dpia", events)
    if spike:
        print(f"Spike detected for agent-dpia: {spike.reason}")
    else:
        print("No burn rate spike detected for agent-dpia (expected with synthetic data)")

    # 4. Trends snapshot
    print()
    print("-" * 100)
    print("TRENDS — Daily Snapshot")
    print("-" * 100)
    trends = TrendsEngine(storage=db)
    since = datetime.now() - timedelta(days=28)
    snaps = trends.compute_daily_snapshots(events, date=since)
    summary = trends.model_adoption_summary(days=28)
    print(f"Computed {len(snaps)} snapshots. Model adoption summary:")
    for row in summary[:5]:
        print(
            f"  {row['model']:<25} {row['total_tokens']:>15,.0f} tokens  "
            f"${row['total_cost_usd']:>10,.2f}"
        )

    # 5. Workflow items summary
    items = db.get_workflow_items()
    print(f"\nOpen workflow items: {len(items)}")
    for item in items:
        print(f"  [{item.item_type.value}] agent={item.agent_id}  status={item.status.value}  "
              f"due={item.due_at.strftime('%Y-%m-%d')}")


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------

def main() -> None:
    days = 28
    since = datetime.now() - timedelta(days=days)
    import tempfile, os, pathlib
    demo_db = pathlib.Path(tempfile.gettempdir()) / "ai_control_plane_demo.db"
    demo_db.unlink(missing_ok=True)  # start fresh each run
    db = SqliteBackend(db_path=str(demo_db))

    print(f"Generating {days} days of synthetic usage...")
    connector = SyntheticConnector(days=days)
    events = connector.pull(since=since)
    print(f"Generated {len(events):,} usage events  "
          f"(total cost: ${sum(e.cost_usd for e in events):,.2f} USD)")

    pools = build_demo_pools(start=since, days=days)
    db.insert_usage_events(events)

    run_supply_chain_demo(events, pools)
    run_agent_governance_demo(events, db)

    print("\n" + "=" * 100)
    print("Demo complete. Run python demo.py to see the full pipeline.")
    print("=" * 100)


if __name__ == "__main__":
    main()
