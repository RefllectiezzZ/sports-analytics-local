"""Tests for durable job handler registry."""

from __future__ import annotations

import pytest

from sports_analytics.core.exceptions import JobRegistryError
from sports_analytics.jobs.context import JobExecutionContext
from sports_analytics.jobs.registry import HandlerRegistry, build_default_registry
from sports_analytics.jobs.types import SYSTEM_NOOP_JOB_TYPE


def _handler(context: JobExecutionContext, payload: object) -> dict[str, object]:
    return {"job_id": context.job_id, "payload": payload}


def test_register_get_list_and_freeze() -> None:
    registry = HandlerRegistry()
    registry.register("demo.job", _handler)
    assert registry.get("demo.job") is _handler
    assert registry.list_job_types() == ("demo.job",)
    registry.freeze()
    with pytest.raises(JobRegistryError, match="frozen"):
        registry.register("demo.other", _handler)


def test_duplicate_unknown_and_invalid_job_types_raise_registry_error() -> None:
    registry = HandlerRegistry()
    registry.register("demo.job", _handler)
    with pytest.raises(JobRegistryError, match="already registered"):
        registry.register("demo.job", _handler)
    with pytest.raises(JobRegistryError, match="no handler"):
        registry.get("demo.missing")
    with pytest.raises(JobRegistryError, match="invalid job type"):
        registry.register("Bad Job", _handler)


def test_default_registry_contains_frozen_system_noop_handler() -> None:
    registry = build_default_registry()
    assert registry.list_job_types() == (
        "analysis.football-product",
        "ingest.bookmaker-autonomous-cycle",
        "ingest.bookmaker-current-odds",
        "ingest.football-data-csv",
        "monitoring.refresh",
        "monitoring.run",
        "results.register-from-snapshot",
        "settlement.settle-analysis",
        "settlement.settle-new-results",
        SYSTEM_NOOP_JOB_TYPE,
        "training.evaluate-retraining-trigger",
        "training.run-challenger-cycle",
    )
    handler = registry.get(SYSTEM_NOOP_JOB_TYPE)
    assert callable(handler)
    assert callable(registry.get("ingest.bookmaker-autonomous-cycle"))
    assert callable(registry.get("ingest.bookmaker-current-odds"))
    assert callable(registry.get("ingest.football-data-csv"))
    assert callable(registry.get("monitoring.run"))
    assert callable(registry.get("analysis.football-product"))
    assert callable(registry.get("settlement.settle-analysis"))
    assert callable(registry.get("results.register-from-snapshot"))
    assert callable(registry.get("settlement.settle-new-results"))
    assert callable(registry.get("monitoring.refresh"))
    assert callable(registry.get("training.evaluate-retraining-trigger"))
    assert callable(registry.get("training.run-challenger-cycle"))
    with pytest.raises(JobRegistryError, match="frozen"):
        registry.register("demo.job", _handler)
