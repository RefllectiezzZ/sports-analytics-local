"""Bookmaker ingestion retry lifecycle tests."""

from __future__ import annotations

from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from sports_analytics.bookmakers.admission import AdmissionDecision, AdmissionOutcome
from sports_analytics.bookmakers.service import BookmakerIngestionService
from sports_analytics.core.exceptions import PermanentJobError, RetryableJobError, SnapshotBusyError
from sports_analytics.core.settings import BookmakerProviderSettings, BookmakersSettings
from sports_analytics.data.database import connect_database
from sports_analytics.data.migrations import ensure_database_ready

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)


def _service(tmp_path: Path) -> BookmakerIngestionService:
    provider = BookmakerProviderSettings(
        enabled=True,
        acquisition_interval_seconds=300,
        initial_delay_seconds=0,
        blocked_cooldown_seconds=900,
    )
    return BookmakerIngestionService(
        database_path=tmp_path / "operational.sqlite3",
        raw_directory=tmp_path / "raw",
        snapshots_directory=tmp_path / "snapshots",
        bookmakers=BookmakersSettings(enabled=True, betano=provider, betclic=provider),
        clock=lambda: NOW,
        session=MagicMock(),
    )


def _admitted() -> AdmissionDecision:
    return AdmissionDecision(
        outcome=AdmissionOutcome.ADMITTED,
        reason_code="admitted",
        may_publish=True,
        may_replace_last_valid=True,
        warnings=(),
    )


def _busy_patches(service: BookmakerIngestionService):
    return (
        patch.object(
            service,
            "_acquire",
            return_value=(
                MagicMock(block_reason=None, warnings=(), pages=(), responses=()),
                MagicMock(events=(object(),), warnings=(), drift_codes=()),
                (),
            ),
        ),
        patch(
            "sports_analytics.bookmakers.service.reconcile_bookmaker_bundles",
            return_value=MagicMock(unresolved_event_reconciliations=()),
        ),
        patch(
            "sports_analytics.bookmakers.service.normalize_bookmaker_bundles",
            return_value=MagicMock(market_quotes=(object(),), unknown_markets=()),
        ),
        patch("sports_analytics.bookmakers.service.evaluate_admission", return_value=_admitted()),
        patch(
            "sports_analytics.bookmakers.service.build_capture_manifest",
            return_value=MagicMock(
                checksum_sha256="a" * 64,
                relative_path="betano-pt/manifests/sha256/aa/" + "a" * 64 + ".json",
                byte_count=32,
                entries=(MagicMock(),),
                manifest_bytes=b"{}",
            ),
        ),
        patch(
            "sports_analytics.bookmakers.service.manifest_to_raw_artifact",
            return_value=MagicMock(
                relative_path="betano-pt/manifests/sha256/aa/" + "a" * 64 + ".json",
                checksum_sha256="a" * 64,
                byte_count=32,
                encoding="utf-8",
            ),
        ),
        patch(
            "sports_analytics.bookmakers.service.persist_capture_manifest",
            side_effect=lambda **kwargs: kwargs["manifest"],
        ),
        patch("sports_analytics.bookmakers.service.verify_capture_manifest"),
        patch(
            "sports_analytics.bookmakers.service.publish_bookmaker_snapshot",
            side_effect=SnapshotBusyError("busy"),
        ),
    )


def test_snapshot_busy_final_attempt_finalizes_failed(tmp_path: Path) -> None:
    database = tmp_path / "operational.sqlite3"
    ensure_database_ready(database)
    service = _service(tmp_path)
    with ExitStack() as stack:
        for item in _busy_patches(service):
            stack.enter_context(item)
        with pytest.raises(PermanentJobError, match="retry attempts exhausted"):
            service.ingest(
                provider_id="betano-pt",
                sport="football",
                acquisition_cycle_id="cycle-busy",
                attempt_number=2,
                maximum_attempts=2,
            )
    with connect_database(database, read_only=True) as connection:
        row = connection.execute(
            """
            SELECT status, failure_classification
            FROM bookmaker_acquisition_runs
            WHERE acquisition_cycle_id = ?
            """,
            ("cycle-busy",),
        ).fetchone()
    assert row is not None
    assert row["status"] == "failed"
    assert row["failure_classification"] == "retry-exhausted"


def test_snapshot_busy_non_final_attempt_stays_retryable(tmp_path: Path) -> None:
    ensure_database_ready(tmp_path / "operational.sqlite3")
    service = _service(tmp_path)
    service = _service(tmp_path)
    with ExitStack() as stack:
        for item in _busy_patches(service):
            stack.enter_context(item)
        with pytest.raises(RetryableJobError):
            service.ingest(
                provider_id="betano-pt",
                sport="football",
                acquisition_cycle_id="cycle-retry",
                attempt_number=1,
                maximum_attempts=2,
            )
