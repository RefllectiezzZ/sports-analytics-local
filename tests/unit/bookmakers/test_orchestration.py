"""Autonomous bookmaker orchestration tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from sports_analytics.bookmakers.orchestration import BookmakerAcquisitionOrchestrator
from sports_analytics.bookmakers.types import (
    PROVIDER_BETANO_PT,
    PROVIDER_BETCLIC_PT,
    BookmakerIngestionResult,
    QuoteSelectionReason,
    SelectionMode,
)
from sports_analytics.core.exceptions import RetryableJobError
from sports_analytics.core.settings import BookmakerProviderSettings, BookmakersSettings
from sports_analytics.data.database import connect_database, transaction
from sports_analytics.data.migrations import ensure_database_ready
from sports_analytics.data.repositories.bookmakers import BookmakerRepository

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)


def _settings(
    *,
    selection_mode: str = "preferred-unless-better",
    betano_enabled: bool = True,
    betclic_enabled: bool = True,
) -> BookmakersSettings:
    provider = BookmakerProviderSettings(
        enabled=True,
        acquisition_interval_seconds=300,
        initial_delay_seconds=0,
        blocked_cooldown_seconds=900,
    )
    return BookmakersSettings(
        enabled=True,
        selection_mode=selection_mode,
        betano=provider if betano_enabled else BookmakerProviderSettings(enabled=False),
        betclic=provider if betclic_enabled else BookmakerProviderSettings(enabled=False),
    )


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


def _orchestrator(tmp_path: Path, settings: BookmakersSettings, service: MagicMock):
    return BookmakerAcquisitionOrchestrator(
        service=service,
        bookmakers=settings,
        database_path=tmp_path / "db.sqlite3",
        raw_directory=tmp_path / "raw",
        snapshots_directory=tmp_path / "snapshots",
        clock=lambda: NOW,
    )


def test_betano_success_also_acquires_betclic_in_comparison_mode(tmp_path: Path) -> None:
    ensure_database_ready(tmp_path / "db.sqlite3")
    service = MagicMock()
    service.ingest.side_effect = [
        _result(provider_id=PROVIDER_BETANO_PT),
        _result(provider_id=PROVIDER_BETCLIC_PT),
    ]
    result = _orchestrator(tmp_path, _settings(), service).run_autonomous_cycle(
        sport="football",
        acquisition_cycle_id="cycle-1",
    )
    assert service.ingest.call_count == 2
    assert result.betano_result is not None
    assert result.betclic_result is not None
    assert result.fallback_decision.reason_code is QuoteSelectionReason.BOTH_RETAINED


def test_forced_betano_mode_skips_betclic(tmp_path: Path) -> None:
    ensure_database_ready(tmp_path / "db.sqlite3")
    service = MagicMock()
    service.ingest.return_value = _result(provider_id=PROVIDER_BETANO_PT)
    result = _orchestrator(
        tmp_path,
        _settings(selection_mode=SelectionMode.BETANO.value),
        service,
    ).run_autonomous_cycle(sport="football", acquisition_cycle_id="cycle-betano-only")
    assert service.ingest.call_count == 1
    assert result.betclic_result is None


def test_forced_betclic_mode_skips_betano(tmp_path: Path) -> None:
    ensure_database_ready(tmp_path / "db.sqlite3")
    service = MagicMock()
    service.ingest.return_value = _result(provider_id=PROVIDER_BETCLIC_PT)
    result = _orchestrator(
        tmp_path,
        _settings(selection_mode=SelectionMode.BETCLIC.value),
        service,
    ).run_autonomous_cycle(sport="football", acquisition_cycle_id="cycle-betclic-only")
    assert service.ingest.call_count == 1
    assert result.betano_result is None


def test_betano_blocked_attempts_betclic(tmp_path: Path) -> None:
    ensure_database_ready(tmp_path / "db.sqlite3")
    service = MagicMock()
    service.ingest.side_effect = [
        _result(provider_id=PROVIDER_BETANO_PT, status="blocked", failure_classification="captcha"),
        _result(provider_id=PROVIDER_BETCLIC_PT),
    ]
    result = _orchestrator(tmp_path, _settings(), service).run_autonomous_cycle(
        sport="football",
        acquisition_cycle_id="cycle-2",
    )
    assert service.ingest.call_count == 2
    assert result.selected_result is not None
    assert result.fallback_decision.selected_provider == PROVIDER_BETCLIC_PT


def test_retryable_betano_continues_to_betclic(tmp_path: Path) -> None:
    ensure_database_ready(tmp_path / "db.sqlite3")
    service = MagicMock()

    def _ingest(**kwargs):
        if kwargs["provider_id"] == PROVIDER_BETANO_PT:
            raise RetryableJobError("source timeout")
        return _result(provider_id=PROVIDER_BETCLIC_PT)

    service.ingest.side_effect = _ingest
    result = _orchestrator(tmp_path, _settings(), service).run_autonomous_cycle(
        sport="football",
        acquisition_cycle_id="cycle-3",
        attempt_number=1,
        maximum_attempts=2,
    )
    assert service.ingest.call_count == 2
    assert result.betclic_result is not None


def test_both_retryable_re_raises_when_no_success(tmp_path: Path) -> None:
    ensure_database_ready(tmp_path / "db.sqlite3")
    service = MagicMock()
    service.ingest.side_effect = RetryableJobError("source timeout")
    with pytest.raises(RetryableJobError):
        _orchestrator(tmp_path, _settings(), service).run_autonomous_cycle(
            sport="football",
            acquisition_cycle_id="cycle-retry",
            attempt_number=1,
            maximum_attempts=2,
        )


def test_betano_blocked_betclic_operational_independent_cooldown(tmp_path: Path) -> None:
    ensure_database_ready(tmp_path / "db.sqlite3")
    with connect_database(tmp_path / "db.sqlite3") as connection:
        with transaction(connection, immediate=True):
            BookmakerRepository(connection).upsert_provider_status(
                provider_id=PROVIDER_BETANO_PT,
                sport="football",
                status="blocked",
                updated_at=NOW,
                next_eligible_at=NOW + timedelta(minutes=30),
            )
    service = MagicMock()
    service.ingest.return_value = _result(provider_id=PROVIDER_BETCLIC_PT)
    result = _orchestrator(tmp_path, _settings(), service).run_autonomous_cycle(
        sport="football",
        acquisition_cycle_id="cycle-cooldown",
    )
    assert service.ingest.call_count == 1
    assert result.betclic_result is not None


def test_both_fail_preserves_cached_reason(tmp_path: Path) -> None:
    ensure_database_ready(tmp_path / "db.sqlite3")
    service = MagicMock()
    service.ingest.side_effect = [
        _result(provider_id=PROVIDER_BETANO_PT, status="unavailable"),
        _result(provider_id=PROVIDER_BETCLIC_PT, status="unavailable"),
    ]
    result = _orchestrator(tmp_path, _settings(), service).run_autonomous_cycle(
        sport="football",
        acquisition_cycle_id="cycle-4",
    )
    assert result.selected_result is None
    assert result.fallback_decision.reason_code in {
        QuoteSelectionReason.NEITHER_AVAILABLE,
        QuoteSelectionReason.PROVIDER_UNAVAILABLE,
        QuoteSelectionReason.CACHED_STALE_PRESERVED,
    }


def test_exact_replay_reuses_finalized_provider_sub_attempt(tmp_path: Path) -> None:
    ensure_database_ready(tmp_path / "db.sqlite3")
    service = MagicMock()
    service.ingest.return_value = _result(provider_id=PROVIDER_BETANO_PT)
    orchestrator = _orchestrator(tmp_path, _settings(selection_mode="betano"), service)
    first = orchestrator.run_autonomous_cycle(sport="football", acquisition_cycle_id="cycle-replay")
    second = orchestrator.run_autonomous_cycle(
        sport="football",
        acquisition_cycle_id="cycle-replay",
    )
    assert first.betano_result is not None
    assert second.betano_result is not None
    assert service.ingest.call_count == 2
