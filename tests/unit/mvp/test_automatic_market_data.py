from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from sports_analytics.core.paths import RuntimePaths, create_runtime_directories
from sports_analytics.data.database import connect_database
from sports_analytics.data.migrations import ensure_database_ready
from sports_analytics.data.repositories.jobs import JobRepository
from sports_analytics.data.types import JobStatus
from sports_analytics.mvp.automatic_market_data import (
    AUTOMATIC_MARKET_DATA_JOB_TYPE,
    RANKING_POLICY,
    AutomaticProviderConfig,
    AutomaticProviderStore,
    _proposal_status,
    _ranking_key,
    _retry_delay_with_jitter,
    ensure_automatic_market_data_job,
)
from sports_analytics.providers.the_odds_api.client import ProviderSecret

NOW = datetime(2026, 8, 1, 12, tzinfo=UTC)


def _paths(tmp_path: Path) -> RuntimePaths:
    root = tmp_path / "runtime"
    paths = RuntimePaths(
        base_directory=tmp_path,
        storage_root=root,
        sqlite_path=root / "operational.sqlite3",
        raw_directory=root / "raw",
        snapshots_directory=root / "snapshots",
        features_directory=root / "features",
        models_directory=root / "models",
        exports_directory=root / "exports",
        logs_directory=root / "logs",
    )
    create_runtime_directories(paths)
    ensure_database_ready(paths.sqlite_path)
    return paths


def _config(*, generation: int = 1) -> AutomaticProviderConfig:
    return AutomaticProviderConfig(
        enabled=True,
        paused=False,
        authentication_blocked=False,
        region="eu",
        competitions=("eng-premier-league",),
        markets=("h2h",),
        refresh_interval_minutes=10,
        quota_reserve=20,
        generation=generation,
        updated_at_utc=NOW,
    )


def test_secret_store_is_separate_atomic_and_redacted(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    store = AutomaticProviderStore(paths.storage_root)
    secret = ProviderSecret("private-test-key")

    store.save_config(_config())
    store.save_secret(secret)

    assert store.load_secret() == secret
    assert "private-test-key" not in repr(secret)
    assert "private-test-key" not in store.config_path.read_text(encoding="utf-8")
    assert store.secret_path.parent == paths.storage_root / "local" / "automatic-market-data"
    assert not list(store.directory.glob("*.tmp"))


def test_immediate_job_is_idempotent_and_interval_is_bounded(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    config = _config()

    first = ensure_automatic_market_data_job(
        database_path=paths.sqlite_path,
        config=config,
        due_at_utc=NOW,
        actor="test",
    )
    second = ensure_automatic_market_data_job(
        database_path=paths.sqlite_path,
        config=config,
        due_at_utc=NOW,
        actor="test",
    )

    assert first.id == second.id
    with connect_database(paths.sqlite_path, read_only=True) as connection:
        pending = JobRepository(connection).list_jobs(
            status=JobStatus.PENDING,
            job_type=AUTOMATIC_MARKET_DATA_JOB_TYPE,
        )
    assert len(pending) == 1
    assert pending[0].maximum_attempts == 3
    retry_delays = [
        _retry_delay_with_jitter(job_id=first.id, retry_number=retry_number)
        for retry_number in range(3)
    ]
    assert retry_delays == sorted(retry_delays)
    assert retry_delays == [
        _retry_delay_with_jitter(job_id=first.id, retry_number=retry_number)
        for retry_number in range(3)
    ]
    assert all(5 <= delay <= 60 for delay in retry_delays)


def test_ranking_policy_is_deterministic_and_status_aware() -> None:
    assert RANKING_POLICY[0].startswith("placeable-manual")
    base = {
        "risk_tier": "low",
        "expected_value": 0.05,
        "edge": 0.04,
        "observed_price_age_seconds": 10.0,
        "bookmaker_coverage_count": 2,
    }
    placeable = {**base, "status": "placeable", "canonical_identity": "b"}
    analytical = {**base, "status": "analytical", "canonical_identity": "a"}
    held = {**base, "status": "held", "canonical_identity": "c"}
    assert sorted((held, analytical, placeable), key=_ranking_key) == [
        placeable,
        analytical,
        held,
    ]
    moderate = {**placeable, "risk_tier": "moderate", "canonical_identity": "a"}
    higher_ev = {**placeable, "expected_value": 0.06, "canonical_identity": "c"}
    assert sorted((moderate, placeable), key=_ranking_key)[0] == placeable
    assert sorted((placeable, higher_ev), key=_ranking_key)[0] == higher_ev
    assert sorted((placeable, placeable), key=_ranking_key) == [placeable, placeable]
    assert _proposal_status(accepted=True, offered=None, reasons=()) == "placeable"
    assert (
        _proposal_status(
            accepted=False,
            offered=Decimal("2.00"),
            reasons=("edge-insufficient",),
        )
        == "analytical"
    )
    assert _proposal_status(accepted=False, offered=None, reasons=()) == "rejected"
