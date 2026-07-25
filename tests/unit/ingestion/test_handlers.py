"""Section 58: football ingestion handler and registry tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sports_analytics.core.exceptions import PermanentJobError, RetryableJobError
from sports_analytics.core.runtime import RuntimeContext, bootstrap_runtime
from sports_analytics.data.database import connect_database
from sports_analytics.data.repositories.snapshots import SnapshotRepository
from sports_analytics.data.types import SnapshotStatus
from sports_analytics.ingestion.handlers import ingest_football_data_csv_handler
from sports_analytics.ingestion.types import INGEST_FOOTBALL_DATA_CSV_JOB_TYPE
from sports_analytics.jobs.context import JobExecutionContext
from sports_analytics.jobs.registry import build_default_registry
from sports_analytics.snapshots.reader import verify_snapshot_directory
from sports_analytics.sources.football_data_co_uk.catalog import build_csv_url
from sports_analytics.sources.raw_store import RawSourceStore
from sports_analytics.sources.types import SOURCE_FOOTBALL_DATA_CO_UK
from tests.helpers_http import FakeClock, FakeHttpTransport

OBSERVED_AT = datetime(2024, 1, 15, 12, 0, tzinfo=UTC)
SYNTHETIC_CSV = (
    b"Div,Date,HomeTeam,AwayTeam,FTHG,FTAG,FTR\n"
    b"E0,12/08/2023,Northbridge FC,Southport Athletic,2,1,H\n"
)


def _runtime(tmp_path: Path) -> RuntimeContext:
    return bootstrap_runtime(
        "worker",
        base_directory=tmp_path,
        environ={},
        overrides={
            "logging": {"file_enabled": False},
            "scraping": {
                "enabled": True,
                "maximum_retries": 0,
                "minimum_request_interval_seconds": 0,
                "retry_backoff_base_seconds": 0.1,
                "retry_backoff_max_seconds": 0.1,
            },
            "worker": {
                "heartbeat_interval_seconds": 1,
                "stale_job_timeout_seconds": 10,
                "shutdown_grace_seconds": 1,
            },
        },
    )


def _context(runtime: RuntimeContext) -> JobExecutionContext:
    context = JobExecutionContext(
        job_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        worker_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        attempt=1,
        maximum_attempts=3,
        claimed_at=OBSERVED_AT,
        lease_expires_at=OBSERVED_AT + timedelta(minutes=5),
        logger=runtime.logger,
    )
    context.bind_runtime(runtime)
    return context


def _payload(raw_sha256: str | None = None) -> dict[str, object]:
    return {
        "competition_id": "eng-premier-league",
        "season": "2023-2024",
        "raw_sha256": raw_sha256,
    }


def _stored_raw(runtime: RuntimeContext) -> str:
    artifact = RawSourceStore(runtime.paths.raw_directory).store_bytes(
        source_name=SOURCE_FOOTBALL_DATA_CO_UK,
        source_url=build_csv_url(division_code="E0", source_season_code="2324"),
        content=SYNTHETIC_CSV,
        retrieved_at=OBSERVED_AT,
        content_type="text/csv",
        etag='"etag-1"',
        last_modified="Wed, 01 Jan 2025 00:00:00 GMT",
        encoding="utf-8",
    )
    return artifact.checksum_sha256


def _snapshot_records(runtime: RuntimeContext):
    with connect_database(runtime.database_path, read_only=True) as connection:
        return SnapshotRepository(connection).list_snapshots()


def test_ingest_handler_downloads_with_fake_transport_and_publishes(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    context = _context(runtime)
    fake_clock = FakeClock()
    transport = FakeHttpTransport([SYNTHETIC_CSV])
    context.bind_test_dependencies(
        transport=transport,
        sleeper=fake_clock.sleep,
        monotonic_clock=fake_clock.monotonic,
        clock=lambda: OBSERVED_AT,
    )

    result = ingest_football_data_csv_handler(context, _payload())

    assert result["snapshot_status"] == "ready"
    assert result["snapshot_reused"] is False
    assert result["competition_id"] == "eng-premier-league"
    assert result["games_count"] == 1
    assert result["teams_count"] == 2
    assert result["odds_quotes_count"] == 0
    assert len(transport.calls) == 1
    records = _snapshot_records(runtime)
    assert len(records) == 1
    assert records[0].status is SnapshotStatus.READY
    assert (
        verify_snapshot_directory(
            snapshots_directory=runtime.paths.snapshots_directory,
            relative_manifest_path=str(result["snapshot_relative_path"]),
            expected_snapshot=records[0],
        ).games_count
        == 1
    )


def test_ingest_handler_reuses_cached_raw_sha_without_http(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    raw_sha = _stored_raw(runtime)
    context = _context(runtime)
    transport = FakeHttpTransport([])
    context.bind_test_dependencies(
        transport=transport,
        sleeper=lambda _seconds: None,
        monotonic_clock=lambda: 0.0,
        clock=lambda: OBSERVED_AT,
    )

    result = ingest_football_data_csv_handler(context, _payload(raw_sha))

    assert result["snapshot_status"] == "ready"
    assert result["source_file_sha256"] == raw_sha
    assert transport.calls == []


def test_ingest_handler_reuses_existing_ready_snapshot(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    raw_sha = _stored_raw(runtime)
    first_context = _context(runtime)
    first_context.bind_test_dependencies(clock=lambda: OBSERVED_AT)
    first = ingest_football_data_csv_handler(first_context, _payload(raw_sha))
    second_context = _context(runtime)
    second_context.bind_test_dependencies(clock=lambda: OBSERVED_AT)

    second = ingest_football_data_csv_handler(second_context, _payload(raw_sha))

    assert first["snapshot_reused"] is False
    assert second["snapshot_reused"] is True
    assert second["snapshot_id"] == first["snapshot_id"]
    assert len(_snapshot_records(runtime)) == 1


@pytest.mark.parametrize(
    "payload, message",
    [
        (None, "payload must be a JSON object"),
        ({}, "payload requires competition_id and season"),
        (
            {"competition_id": "eng-premier-league", "season": "2023-2024", "url": "x"},
            "unknown",
        ),
        (
            {"competition_id": "eng-premier-league", "season": "2023-2024", "raw_sha256": 123},
            "raw_sha256",
        ),
    ],
)
def test_ingest_handler_rejects_invalid_payloads(
    tmp_path: Path,
    payload: object,
    message: str,
) -> None:
    runtime = _runtime(tmp_path)
    context = _context(runtime)

    with pytest.raises(PermanentJobError, match=message):
        ingest_football_data_csv_handler(context, payload)


def test_ingest_handler_requires_runtime_binding() -> None:
    context = JobExecutionContext(
        job_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        worker_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        attempt=1,
        maximum_attempts=3,
        claimed_at=OBSERVED_AT,
        lease_expires_at=OBSERVED_AT + timedelta(minutes=5),
        logger=__import__("logging").getLogger("test"),
    )

    with pytest.raises(PermanentJobError, match="runtime context binding"):
        ingest_football_data_csv_handler(context, _payload())


def test_ingest_handler_maps_retryable_source_errors_to_retryable_job(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    context = _context(runtime)
    context.bind_test_dependencies(
        transport=FakeHttpTransport([(500, b"temporarily unavailable", "text/plain")]),
        sleeper=lambda _seconds: None,
        monotonic_clock=lambda: 0.0,
        clock=lambda: OBSERVED_AT,
    )

    with pytest.raises(RetryableJobError, match="temporary source HTTP failure"):
        ingest_football_data_csv_handler(context, _payload())


def test_default_registry_contains_ingestion_handler() -> None:
    registry = build_default_registry()

    assert registry.get(INGEST_FOOTBALL_DATA_CSV_JOB_TYPE) is ingest_football_data_csv_handler
