"""
UsageConnector: the pluggable interface every data source implements.

Real connectors (Snowflake, Databricks, GitHub Copilot, M365 Copilot,
Copilot Studio, M365 Cowork, Azure AI Foundry, LangSmith, Dynatrace) live
in a private repo alongside this project, since they embed org-specific auth
and cost-center mappings. They import this interface as a dependency. Only
the SyntheticConnector ships publicly, so the whole system is runnable with
zero credentials.

Each connector implements two methods:
  pull()                  — returns UsageEvents (always required)
  pull_manifest_fragments() — returns AgentManifestFragments inferred from
                              this source (optional; defaults to empty list)

Manifest fragments are partial — a connector may know an agent's tool list
but not its data_scope. The agent registry merges fragments from multiple
sources into a full AgentManifest.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from core.models import AgentManifestFragment, UsageEvent


class UsageConnector(ABC):
    """Every connector normalizes its source's usage into UsageEvents and
    optionally yields AgentManifestFragments inferred from that source."""

    @abstractmethod
    def pull(self, since: datetime) -> list[UsageEvent]:
        """Return all usage events for this source since the given timestamp."""
        raise NotImplementedError

    @abstractmethod
    def source_name(self) -> str:
        raise NotImplementedError

    def pull_manifest_fragments(
        self, since: datetime
    ) -> list[AgentManifestFragment]:
        """Return manifest fragments inferred from this source since the given
        timestamp. The default implementation returns an empty list — override
        in connectors that can infer agent structure (tool list, data scope,
        execution rights, etc.) from the platform's metadata API.

        Fragments from multiple connectors are merged by the agent registry;
        later observations override earlier ones for the same field."""
        return []
