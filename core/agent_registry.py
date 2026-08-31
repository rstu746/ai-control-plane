"""
Agent Registry — discovery, registration, and manifest management.

Responsibilities:
  1. Discover unknown agents from gateway logs (UsageEvents with actor_ids
     not yet in the registry) and emit unknown_agent_detected alerts.
  2. Merge AgentManifestFragments from multiple connectors into a single
     AgentManifest per agent.
  3. Provide the classifier with a complete-enough manifest to classify, or
     raise a ClassificationRequest WorkflowItem if the manifest is too sparse.
  4. Apply the Summoner floor rule: an orchestrator's effective tier is the
     maximum of its own tier and the maximum tier of its reachable sub-agents.
  5. Track last_seen_at and trigger dormant status changes.

The registry does not run the classifier directly — it prepares agents for
classification and delegates to core/classifier.py. This keeps the two
concerns (data completeness vs. rule evaluation) separate.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta

from core.models import (
    Agent,
    AgentManifest,
    AgentManifestFragment,
    AgentStatus,
    AgentTier,
    DiscoverySource,
    FunctionalRole,
    SourceApp,
    UsageEvent,
)
from storage.sqlite import SqliteBackend

logger = logging.getLogger(__name__)

# Minimum manifest completeness required before the classifier will attempt
# to assign a tier. Below this, a ClassificationRequest WorkflowItem is raised.
MINIMUM_MANIFEST_COMPLETENESS = 0.5

# Window after which an agent with no usage events is marked dormant.
DEFAULT_DORMANCY_DAYS = 30


class AgentRegistry:
    """Manages agent discovery, manifest assembly, and status transitions."""

    def __init__(
        self,
        storage: SqliteBackend,
        dormancy_days: int = DEFAULT_DORMANCY_DAYS,
        min_completeness: float = MINIMUM_MANIFEST_COMPLETENESS,
    ):
        self._storage = storage
        self._dormancy_days = dormancy_days
        self._min_completeness = min_completeness

    # ------------------------------------------------------------------
    # Discovery from usage events
    # ------------------------------------------------------------------

    def process_usage_events(self, events: list[UsageEvent]) -> list[Agent]:
        """Scan a batch of usage events. For each actor_id not yet in the
        registry, create a DISCOVERED agent record. Returns the list of
        newly discovered agents so the caller can fire alerts."""
        newly_discovered: list[Agent] = []
        seen_actors: set[str] = set()

        for event in events:
            actor = event.actor_id
            if actor in seen_actors:
                continue
            seen_actors.add(actor)

            existing = self._storage.get_agent(actor)
            if existing:
                # Update last_seen_at and check for dormancy recovery
                if existing.status == AgentStatus.DORMANT:
                    existing.status = AgentStatus.ACTIVE
                existing.last_seen_at = event.timestamp
                self._storage.upsert_agent(existing)
            else:
                # New actor — create a DISCOVERED stub
                agent = Agent(
                    agent_id=actor,
                    name=actor,  # placeholder until owner registers a name
                    owner_id="",
                    team_id=event.team_id or "",
                    source_platform=event.source_app,
                    status=AgentStatus.DISCOVERED,
                    discovery_source=DiscoverySource.GATEWAY_LOGS,
                    discovered_at=event.timestamp,
                    last_seen_at=event.timestamp,
                )
                self._storage.upsert_agent(agent)
                newly_discovered.append(agent)
                logger.info("New agent discovered from gateway logs: %s", actor)

        return newly_discovered

    # ------------------------------------------------------------------
    # Manifest assembly
    # ------------------------------------------------------------------

    def merge_manifest_fragment(
        self, fragment: AgentManifestFragment
    ) -> AgentManifest:
        """Merge a new fragment into the existing manifest for an agent.
        Fields present in the fragment overwrite the same field in the stored
        manifest. List fields (tool_list, etc.) are unioned. Returns the
        merged manifest."""
        existing = self._storage.get_agent_manifest(fragment.agent_id)
        if existing is None:
            existing = AgentManifest()

        # Union list fields
        existing.tool_list = _union(existing.tool_list, fragment.tool_list)
        existing.mcp_servers = _union(existing.mcp_servers, fragment.mcp_servers)
        existing.data_sources = _union(existing.data_sources, fragment.data_sources)
        existing.sub_agent_ids = _union(existing.sub_agent_ids, fragment.sub_agent_ids)
        existing.external_targets = _union(existing.external_targets, fragment.external_targets)

        # Scalar capability flags: only overwrite if the fragment has a value
        if fragment.execution_rights is not None:
            existing.execution_rights = fragment.execution_rights
        if fragment.credentials_held is not None:
            existing.credentials_held = fragment.credentials_held
        if fragment.can_modify_repos_or_pipelines is not None:
            existing.can_modify_repos_or_pipelines = fragment.can_modify_repos_or_pipelines
        if fragment.can_modify_other_agents is not None:
            existing.can_modify_other_agents = fragment.can_modify_other_agents
        if fragment.writes_to_system_of_record is not None:
            existing.writes_to_system_of_record = fragment.writes_to_system_of_record
        if fragment.reaches_external_party is not None:
            existing.reaches_external_party = fragment.reaches_external_party
        if fragment.distributes_output_autonomously is not None:
            existing.distributes_output_autonomously = fragment.distributes_output_autonomously
        if fragment.grounding_includes_confidential_of_others is not None:
            existing.grounding_includes_confidential_of_others = (
                fragment.grounding_includes_confidential_of_others
            )
        if fragment.data_scope is not None:
            existing.data_scope = fragment.data_scope

        existing.last_updated = fragment.observed_at
        self._storage.upsert_agent_manifest(fragment.agent_id, existing)
        logger.debug(
            "Merged manifest fragment for agent %s (completeness=%.0f%%)",
            fragment.agent_id,
            existing.completeness * 100,
        )
        return existing

    def get_manifest_for_classification(
        self, agent_id: str
    ) -> tuple[AgentManifest | None, list[str]]:
        """Return (manifest, missing_fields) where missing_fields is empty if
        the manifest is complete enough for classification, or a list of
        field names that still need to be provided."""
        manifest = self._storage.get_agent_manifest(agent_id)
        if manifest is None:
            return None, ["manifest (none submitted)"]

        missing = _minimum_viable_missing(manifest)
        if manifest.completeness < self._min_completeness:
            # Also report all undeclared capability flags
            extra = manifest.missing_capability_flags
            missing = list(dict.fromkeys(missing + extra))  # deduplicate, preserve order

        return manifest, missing

    # ------------------------------------------------------------------
    # Summoner floor rule
    # ------------------------------------------------------------------

    def effective_tier(self, agent_id: str) -> AgentTier:
        """Return the agent's effective tier, applying the Summoner floor rule:
        an orchestrator's tier is the max of its own tier and the max tier of
        all reachable sub-agents."""
        agent = self._storage.get_agent(agent_id)
        if agent is None:
            return AgentTier.UNCLASSIFIED

        own_tier = agent.tier
        manifest = self._storage.get_agent_manifest(agent_id)
        if not manifest or not manifest.sub_agent_ids:
            return own_tier

        sub_tiers = [own_tier]
        for sub_id in manifest.sub_agent_ids:
            sub = self._storage.get_agent(sub_id)
            if sub:
                sub_tiers.append(sub.tier)

        return _max_tier(sub_tiers)

    # ------------------------------------------------------------------
    # Dormancy checks
    # ------------------------------------------------------------------

    def mark_dormant_agents(
        self, cutoff: datetime | None = None
    ) -> list[Agent]:
        """Check all active agents and mark those with no recent usage as DORMANT.
        Returns the list of newly dormant agents so alerts can be fired."""
        if cutoff is None:
            cutoff = datetime.now() - timedelta(days=self._dormancy_days)

        active_agents = self._storage.get_agents(status=AgentStatus.ACTIVE.value)
        newly_dormant: list[Agent] = []

        for agent in active_agents:
            last_seen = agent.last_seen_at
            if last_seen is None or last_seen < cutoff:
                agent.status = AgentStatus.DORMANT
                self._storage.upsert_agent(agent)
                newly_dormant.append(agent)
                logger.info(
                    "Agent %s marked dormant (last seen: %s)",
                    agent.agent_id,
                    last_seen,
                )

        return newly_dormant

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register_agent(
        self,
        agent_id: str,
        name: str,
        owner_id: str,
        team_id: str,
        source_platform: SourceApp,
        manifest: AgentManifest | None = None,
        escalation_webhook_url: str | None = None,
    ) -> Agent:
        """Register or update an agent. If the agent was previously DISCOVERED,
        upgrades its status to REGISTERED. If a manifest is provided, stores it
        and attempts to trigger classification via the registry caller."""
        existing = self._storage.get_agent(agent_id)
        now = datetime.now()

        if existing:
            existing.name = name
            existing.owner_id = owner_id
            existing.team_id = team_id
            existing.source_platform = source_platform
            existing.escalation_webhook_url = escalation_webhook_url
            if existing.status == AgentStatus.DISCOVERED:
                existing.status = AgentStatus.REGISTERED
            agent = existing
        else:
            agent = Agent(
                agent_id=agent_id,
                name=name,
                owner_id=owner_id,
                team_id=team_id,
                source_platform=source_platform,
                status=AgentStatus.REGISTERED,
                discovery_source=DiscoverySource.SELF_REGISTRATION,
                discovered_at=now,
                escalation_webhook_url=escalation_webhook_url,
            )

        self._storage.upsert_agent(agent)

        if manifest is not None:
            self._storage.upsert_agent_manifest(agent_id, manifest)

        logger.info("Agent registered: %s (%s)", agent_id, name)
        return agent


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _union(a: list[str], b: list[str]) -> list[str]:
    """Return deduplicated union of two lists, preserving order."""
    seen: set[str] = set(a)
    result = list(a)
    for item in b:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


_TIER_ORDER = {
    AgentTier.UNCLASSIFIED: 0,
    AgentTier.TIER_1: 1,
    AgentTier.TIER_2: 2,
    AgentTier.TIER_3: 3,
}


def _max_tier(tiers: list[AgentTier]) -> AgentTier:
    return max(tiers, key=lambda t: _TIER_ORDER.get(t, 0))


def _minimum_viable_missing(manifest: AgentManifest) -> list[str]:
    """Return the fields that are required at minimum before classification
    can proceed. Owner and team are checked on the Agent, not the manifest."""
    missing = []
    if manifest.data_scope is None:
        missing.append("data_scope")
    if (
        not manifest.tool_list
        and not manifest.data_sources
        and manifest.execution_rights is None
    ):
        missing.append("tool_list or data_sources or execution_rights (at least one)")
    return missing
