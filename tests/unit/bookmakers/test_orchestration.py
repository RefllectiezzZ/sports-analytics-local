"""Autonomous bookmaker orchestration tests."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from sports_analytics.bookmakers.orchestration import BookmakerAcquisitionOrchestrator
from sports_analytics.bookmakers.types import (
    PROVIDER_BETANO_PT,
    PROVIDER_BETCLIC_PT,
    BookmakerIngestionResult,
    QuoteSelectionReason,
)
from sports_analytics.core.exceptions import RetryableJobError
from sports_analytics.core.settings import BookmakerProviderSettings, BookmakersSettings
from sports_analytics.data.migrations import ensure_database_ready

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)


def _settings() -> BookmakersSettings:
    provider = BookmakerProviderSettings(
        enabled=True,
        acquisition_interval_seconds=300,
        initial_delay_seconds=0,
        blocked_cooldown_seconds=900,
    )
    return BookmakersSettings(enabled=True, betano=provider, betclic=provider)


def _result(
    *,
    provider_id: str,
    status: str = "succeeded",
    failure_classification: str = "",
) -> BookmakerIngestionResult:
    return BookmakerIngestionResult(
        provider_id=provider_id,
        sport="football",
        acquisition_cycle_id=f"cycle-{provider_id}",
        adapter_version="adapter-v1",
        status=status,
        observed_at_utc=NOW.isoformat(),
        snapshot_id="snap-1" if status == "succeeded" else None,
        snapshot_reused=False,
        block_reason=None,
        failure_classification=failure_classification,
        events_observed=1,
        valid_quotes_observed=1,
        unresolved_events=0,
        rejected_markets=0,
        warnings=(),
        drift_codes=(),
    )


def test_betano_success_skips_betclic(tmp_path: Path) -> None:
    ensure_database_ready(tmp_path / "db.sqlite3")
    service = MagicMock()
    service.ingest.return_value = _result(provider_id=PROVIDER_BETANO_PT)
    orchestrator = BookmakerAcquisitionOrchestrator(
        service=service,
        bookmakers=_settings(),
        database_path=tmp_path / "db.sqlite3",
        raw_directory=tmp_path / "raw",
        snapshots_directory=tmp_path / "snapshots",
        clock=lambda: NOW,
    )
    result = orchestrator.run_autonomous_cycle(sport="football", acquisition_cycle_id="cycle-1")
    assert service.ingest.call_count == 1
    assert result.betclic_result is None
    assert result.selected_result is not None
    assert result.fallback_decision.selected_provider == PROVIDER_BETANO_PT


def test_betano_blocked_attempts_betclic(tmp_path: Path) -> None:
    ensure_database_ready(tmp_path / "db.sqlite3")
    service = MagicMock()
    service.ingest.side_effect = [
        _result(provider_id=PROVIDER_BETANO_PT, status="blocked", failure_classification="captcha"),
        _result(provider_id=PROVIDER_BETCLIC_PT),
    ]
    orchestrator = BookmakerAcquisitionOrchestrator(
        service=service,
        bookmakers=_settings(),
        database_path=tmp_path / "db.sqlite3",
        raw_directory=tmp_path / "raw",
        snapshots_directory=tmp_path / "snapshots",
        clock=lambda: NOW,
    )
    result = orchestrator.run_autonomous_cycle(sport="football", acquisition_cycle_id="cycle-2")
    assert service.ingest.call_count == 2
    assert result.selected_result is not None
    assert result.fallback_decision.selected_provider == PROVIDER_BETCLIC_PT


def test_retryable_betano_re_raises_before_betclic(tmp_path: Path) -> None:
    service = MagicMock()
    service.ingest.side_effect = RetryableJobError("source timeout")
    orchestrator = BookmakerAcquisitionOrchestrator(
        service=service,
        bookmakers=_settings(),
        database_path=tmp_path / "db.sqlite3",
        raw_directory=tmp_path / "raw",
        snapshots_directory=tmp_path / "snapshots",
        clock=lambda: NOW,
    )
    with pytest.raises(RetryableJobError):
        orchestrator.run_autonomous_cycle(
            sport="football",
            acquisition_cycle_id="cycle-3",
            attempt_number=1,
            maximum_attempts=2,
        )
    assert service.ingest.call_count == 1


def test_both_fail_preserves_cached_reason(tmp_path: Path) -> None:
    ensure_database_ready(tmp_path / "db.sqlite3")
    service = MagicMock()
    service.ingest.side_effect = [
        _result(provider_id=PROVIDER_BETANO_PT, status="unavailable"),
        _result(provider_id=PROVIDER_BETCLIC_PT, status="unavailable"),
    ]
    orchestrator = BookmakerAcquisitionOrchestrator(
        service=service,
        bookmakers=_settings(),
        database_path=tmp_path / "db.sqlite3",
        raw_directory=tmp_path / "raw",
        snapshots_directory=tmp_path / "snapshots",
        clock=lambda: NOW,
    )
    result = orchestrator.run_autonomous_cycle(sport="football", acquisition_cycle_id="cycle-4")
    assert result.selected_result is None
    assert result.fallback_decision.reason_code in {
        QuoteSelectionReason.NEITHER_AVAILABLE,
        QuoteSelectionReason.PROVIDER_UNAVAILABLE,
        QuoteSelectionReason.CACHED_STALE_PRESERVED,
    }
