"""
UsageConnector: the pluggable interface every data source implements.

Real connectors (Snowflake, Databricks, GitHub Copilot, M365 Copilot,
Copilot Studio, M365 Cowork, Azure AI Foundry) live in a
private repo alongside this project, since they embed org-specific auth and
cost-center mappings. They import this interface as a dependency. Only the
SyntheticConnector ships publicly, so the whole system is runnable with zero
credentials.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from core.models import UsageEvent


class UsageConnector(ABC):
    """Every connector normalizes its source's usage into UsageEvents."""

    @abstractmethod
    def pull(self, since: datetime) -> list[UsageEvent]:
        """Return all usage events for this source since the given timestamp."""
        raise NotImplementedError

    @abstractmethod
    def source_name(self) -> str:
        raise NotImplementedError
