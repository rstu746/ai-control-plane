from datetime import datetime

from core.model_lifecycle import ModelRegistryEntry, compute_deprecations
from core.models import CapacityPool, DemandDriver, ModelStatus


def test_no_deprecation_when_gap_is_less_than_two():
    registry = [
        ModelRegistryEntry("opus", 5, ModelStatus.ESTABLISHED),
        ModelRegistryEntry("opus", 6, ModelStatus.ESTABLISHED),
    ]
    updated = compute_deprecations(registry)
    statuses = {e.version_number: e.status for e in updated}
    assert statuses[5] == ModelStatus.ESTABLISHED
    assert statuses[6] == ModelStatus.ESTABLISHED


def test_deprecates_when_two_versions_ahead_released():
    # opus-4.5 should deprecate once opus-4.7 is live (gap of 2)
    registry = [
        ModelRegistryEntry("opus", 5, ModelStatus.ESTABLISHED),
        ModelRegistryEntry("opus", 6, ModelStatus.ESTABLISHED),
        ModelRegistryEntry("opus", 7, ModelStatus.ESTABLISHED),
    ]
    updated = compute_deprecations(registry)
    statuses = {e.version_number: e.status for e in updated}
    assert statuses[5] == ModelStatus.DEPRECATED
    assert statuses[6] == ModelStatus.ESTABLISHED  # only 1 version behind latest
    assert statuses[7] == ModelStatus.ESTABLISHED


def test_deprecation_is_per_family_not_global():
    registry = [
        ModelRegistryEntry("opus", 5, ModelStatus.ESTABLISHED),
        ModelRegistryEntry("opus", 7, ModelStatus.ESTABLISHED),
        ModelRegistryEntry("gpt-4o", 1, ModelStatus.ESTABLISHED),
        ModelRegistryEntry("gpt-4o", 2, ModelStatus.ESTABLISHED),
    ]
    updated = compute_deprecations(registry)
    by_key = {(e.model_family, e.version_number): e.status for e in updated}
    assert by_key[("opus", 5)] == ModelStatus.DEPRECATED
    assert by_key[("gpt-4o", 1)] == ModelStatus.ESTABLISHED  # gap of 1, not deprecated


def test_candidate_and_benchmarked_models_never_auto_deprecated():
    # A model with no live pool yet shouldn't be swept into deprecation just
    # because a later version exists — it never had traffic to deprecate.
    registry = [
        ModelRegistryEntry("opus", 5, ModelStatus.CANDIDATE),
        ModelRegistryEntry("opus", 6, ModelStatus.BENCHMARKED),
        ModelRegistryEntry("opus", 7, ModelStatus.ESTABLISHED),
    ]
    updated = compute_deprecations(registry)
    statuses = {e.version_number: e.status for e in updated}
    assert statuses[5] == ModelStatus.CANDIDATE
    assert statuses[6] == ModelStatus.BENCHMARKED
