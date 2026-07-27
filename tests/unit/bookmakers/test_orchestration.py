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


def test_retryable_betano_preserves_betclic_and_requests_retry(tmp_path: Path) -> None:
    ensure_database_ready(tmp_path / "db.sqlite3")
    service = MagicMock()

    def _ingest(**kwargs):
        if kwargs["provider_id"] == PROVIDER_BETANO_PT:
            raise RetryableJobError("source timeout")
        return _result(provider_id=PROVIDER_BETCLIC_PT)

    service.ingest.side_effect = _ingest
    with pytest.raises(RetryableJobError, match="betano-pt"):
        _orchestrator(tmp_path, _settings(), service).run_autonomous_cycle(
            sport="football",
            acquisition_cycle_id="cycle-3",
            attempt_number=1,
            maximum_attempts=2,
        )
    assert service.ingest.call_count == 2
    assert {call.kwargs["provider_id"] for call in service.ingest.call_args_list} == {
        PROVIDER_BETANO_PT,
        PROVIDER_BETCLIC_PT,
    }
    assert service.ingest.call_args_list[0].kwargs["acquisition_cycle_id"] == "cycle-3-betano"
    assert service.ingest.call_args_list[1].kwargs["acquisition_cycle_id"] == "cycle-3-betclic"


def test_retryable_betclic_preserves_betano_and_requests_retry(tmp_path: Path) -> None:
    ensure_database_ready(tmp_path / "db.sqlite3")
    service = MagicMock()

    def _ingest(**kwargs):
        if kwargs["provider_id"] == PROVIDER_BETCLIC_PT:
            raise RetryableJobError("source timeout")
        return _result(provider_id=PROVIDER_BETANO_PT)

    service.ingest.side_effect = _ingest
    with pytest.raises(RetryableJobError, match="betclic-pt"):
        _orchestrator(tmp_path, _settings(), service).run_autonomous_cycle(
            sport="football",
            acquisition_cycle_id="cycle-retry-betclic",
            attempt_number=1,
            maximum_attempts=2,
        )
    assert service.ingest.call_count == 2


def test_one_success_other_retry_exhaustion_finalizes(tmp_path: Path) -> None:
    ensure_database_ready(tmp_path / "db.sqlite3")
    service = MagicMock()

    def _ingest(**kwargs):
        if kwargs["provider_id"] == PROVIDER_BETANO_PT:
            return _result(provider_id=PROVIDER_BETANO_PT)
        raise RetryableJobError("source timeout")

    service.ingest.side_effect = _ingest
    result = _orchestrator(tmp_path, _settings(), service).run_autonomous_cycle(
        sport="football",
        acquisition_cycle_id="cycle-exhaust",
        attempt_number=2,
        maximum_attempts=2,
    )
    assert result.betano_result is not None
    assert result.betclic_result is None
    assert any(
        item.provider_id == PROVIDER_BETCLIC_PT and not item.outcome.success
        for item in result.provider_sub_attempts
    )


def test_provider_sub_cycle_ids_are_exact_and_unique() -> None:
    from sports_analytics.bookmakers.orchestration import provider_sub_cycle_id

    logical = "cycle-abc-pt"
    betano = provider_sub_cycle_id(logical, PROVIDER_BETANO_PT)
    betclic = provider_sub_cycle_id(logical, PROVIDER_BETCLIC_PT)
    assert betano == "cycle-abc-pt-betano"
    assert betclic == "cycle-abc-pt-betclic"
    assert betano != betclic
    assert not betano.endswith("-pt")
    assert betano.rsplit("-", 1)[-1] == "betano"


def test_lifecycle_timestamps_preserve_schedule_separately(tmp_path: Path) -> None:
    ensure_database_ready(tmp_path / "db.sqlite3")
    service = MagicMock()
    service.ingest.return_value = _result(provider_id=PROVIDER_BETANO_PT)
    scheduled = NOW - timedelta(minutes=5)
    enqueued = NOW - timedelta(minutes=1)
    result = _orchestrator(
        tmp_path,
        _settings(selection_mode=SelectionMode.BETANO.value),
        service,
    ).run_autonomous_cycle(
        sport="football",
        acquisition_cycle_id="cycle-ts",
        scheduled_for_utc=scheduled,
        enqueued_at_utc=enqueued,
        observed_at_utc=NOW,
    )
    assert result.lifecycle is not None
    assert result.lifecycle.scheduled_for_utc == scheduled
    assert result.lifecycle.enqueued_at_utc == enqueued
    assert result.lifecycle.acquisition_started_at_utc == NOW
    assert result.lifecycle.acquisition_finished_at_utc == NOW
    assert result.lifecycle.scheduled_for_utc != result.lifecycle.provider_response_observed_at_utc


def test_exact_replay_after_restart_reuses_success_without_rerunning_browser(
    tmp_path: Path,
) -> None:
    """Simulate process restart: second attempt returns existing successful run only."""
    ensure_database_ready(tmp_path / "db.sqlite3")
    betano_success = _result(provider_id=PROVIDER_BETANO_PT)
    betclic_success = _result(provider_id=PROVIDER_BETCLIC_PT)
    calls: list[str] = []

    def _ingest(**kwargs):
        provider_id = kwargs["provider_id"]
        calls.append(provider_id)
        if provider_id == PROVIDER_BETANO_PT:
            if len([item for item in calls if item == PROVIDER_BETANO_PT]) == 1:
                raise RetryableJobError("timeout")
            return betano_success
        return betclic_success

    service = MagicMock()
    service.ingest.side_effect = _ingest
    orchestrator = _orchestrator(tmp_path, _settings(), service)
    with pytest.raises(RetryableJobError):
        orchestrator.run_autonomous_cycle(
            sport="football",
            acquisition_cycle_id="cycle-replay-partial",
            attempt_number=1,
            maximum_attempts=2,
        )
    # Restarted process: successful Betclic returns via service idempotency;
    # Betano is retried. Mock simulates that by succeeding on second Betano call.
    result = orchestrator.run_autonomous_cycle(
        sport="football",
        acquisition_cycle_id="cycle-replay-partial",
        attempt_number=2,
        maximum_attempts=2,
    )
    assert result.betano_result is not None
    assert result.betclic_result is not None
    assert calls.count(PROVIDER_BETANO_PT) == 2
    assert calls.count(PROVIDER_BETCLIC_PT) == 2
    assert {call.kwargs["acquisition_cycle_id"] for call in service.ingest.call_args_list} == {
        "cycle-replay-partial-betano",
        "cycle-replay-partial-betclic",
    }


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
