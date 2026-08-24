"""
Model lifecycle and auto-deprecation.

Models are tracked as (model_family, version_number) rather than parsed
from name strings, since naming isn't consistent across vendors
(gpt-4o vs. claude-sonnet-4-6 vs. gpt-4-turbo-2024-04-09). A model's
version_number is just its position within its own family's release order.

Deprecation rule: version N in a family is auto-deprecated the moment
version N+2 in that same family is live. One version of buffer stays
active — e.g. if opus-4.5 is live and opus-4.6 releases, 4.5 stays active;
once opus-4.7 releases, opus-4.5 (N, with N+2 = 4.7 now live) is
auto-deprecated. opus-4.6 remains active until 4.8 lands.

Deprecated pools are excluded from "increase" recommendations (no top-up
purchase for a dying model) but retained for historical trend views.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.models import CapacityPool, ModelStatus

DEPRECATION_VERSION_GAP = 2


@dataclass
class ModelRegistryEntry:
    model_family: str
    version_number: int
    status: ModelStatus


def compute_deprecations(
    registry: list[ModelRegistryEntry],
) -> list[ModelRegistryEntry]:
    """Given the full set of known model versions, return an updated list
    with status set to DEPRECATED wherever a version N+2 or later in the
    same family is present and not itself deprecated/candidate.

    Does not mutate the input; returns new entries with updated status.
    Entries already CANDIDATE or BENCHMARKED (not yet carrying real
    traffic) are left alone — deprecation only applies to models that have
    an active pool (PILOTED or ESTABLISHED).
    """
    latest_version_by_family: dict[str, int] = {}
    for entry in registry:
        latest_version_by_family[entry.model_family] = max(
            latest_version_by_family.get(entry.model_family, 0),
            entry.version_number,
        )

    updated: list[ModelRegistryEntry] = []
    for entry in registry:
        if entry.status not in (ModelStatus.PILOTED, ModelStatus.ESTABLISHED):
            updated.append(entry)
            continue

        latest = latest_version_by_family[entry.model_family]
        should_deprecate = (latest - entry.version_number) >= DEPRECATION_VERSION_GAP

        updated.append(
            ModelRegistryEntry(
                model_family=entry.model_family,
                version_number=entry.version_number,
                status=ModelStatus.DEPRECATED if should_deprecate else entry.status,
            )
        )

    return updated


def apply_deprecations_to_pools(pools: list[CapacityPool]) -> list[CapacityPool]:
    """Convenience wrapper: derive a registry from a pool list, compute
    deprecations, and return pools with .status updated accordingly."""
    registry = [
        ModelRegistryEntry(
            model_family=p.model_family,
            version_number=p.version_number,
            status=p.status,
        )
        for p in pools
    ]
    updated_registry = compute_deprecations(registry)
    status_by_key = {
        (e.model_family, e.version_number): e.status for e in updated_registry
    }

    result = []
    for p in pools:
        new_status = status_by_key[(p.model_family, p.version_number)]
        if new_status != p.status:
            p = CapacityPool(
                pool_id=p.pool_id,
                model=p.model,
                model_family=p.model_family,
                version_number=p.version_number,
                region=p.region,
                ptu_quantity=p.ptu_quantity,
                cost_usd=p.cost_usd,
                start_date=p.start_date,
                end_date=p.end_date,
                demand_driver=p.demand_driver,
                status=new_status,
                throughput_capacity_tokens_per_hour=p.throughput_capacity_tokens_per_hour,
            )
        result.append(p)
    return result
