"""Section 58: football ingestion handler and registry tests."""

from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path

import pytest

from sports_analytics.core.exceptions import (
    JobLeaseError,
    PermanentJobError,
    RetryableJobError,
    WorkerShutdownError,
)
from sports_analytics.core.runtime import RuntimeContext, bootstrap_runtime
from sports_analytics.data.database import connect_database
from sports_analytics.data.repositories.snapshots import SnapshotRepository
from sports_analytics.data.types import SnapshotRecord, SnapshotStatus
from sports_analytics.ingestion.handlers import ingest_football_data_csv_handler
from sports_analytics.ingestion.snapshot_specs import resolve_snapshot_suite
from sports_analytics.ingestion.types import INGEST_FOOTBALL_DATA_CSV_JOB_TYPE
from sports_analytics.jobs.context import JobExecutionContext
from sports_analytics.jobs.registry import build_default_registry
from sports_analytics.snapshots.reader import verify_snapshot_directory
from sports_analytics.snapshots.spec import SnapshotDatasetSuite
from sports_analytics.sports.football.contracts import (
    FOOTBALL_CANONICAL_SCHEMA_VERSION,
    FOOTBALL_INGESTION_SNAPSHOT_TYPE,
)
from tests.helpers_http import FakeClock, FakeHttpTransport
from tests.helpers_snapshots import (
    OBSERVED_AT,
    SYNTHETIC_CSV_WITH_ODDS,
    store_artifact,
)

EXPECTED_ROW_COUNTS = (
    ("competitions", 1),
    ("seasons", 1),
    ("participants", 2),
    ("source_participants", 2),
    ("participant_reconciliations", 2),
    ("events", 1),
    ("source_events", 1),
    ("event_reconciliations", 1),
    ("market_quotes", 3),
    ("post_match_statistics", 1),
)
EXPECTED_DIRECTORY_FILES = frozenset(
    {
        "manifest.json",
        "competitions.parquet",
        "seasons.parquet",
        "participants.parquet",
        "source_participants.parquet",
        "participant_reconciliations.parquet",
        "events.parquet",
        "source_events.parquet",
        "event_reconciliations.parquet",
        "market_quotes.parquet",
        "post_match_statistics.parquet",
    }
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
    artifact = store_artifact(
        runtime.paths.raw_directory,
        content=SYNTHETIC_CSV_WITH_ODDS,
    )
    return artifact.checksum_sha256


def _snapshot_records(runtime: RuntimeContext) -> list[SnapshotRecord]:
    with connect_database(runtime.database_path, read_only=True) as connection:
        return SnapshotRepository(connection).list_snapshots()


def _suite() -> SnapshotDatasetSuite:
    return resolve_snapshot_suite(
        snapshot_type=FOOTBALL_INGESTION_SNAPSHOT_TYPE,
        schema_version=FOOTBALL_CANONICAL_SCHEMA_VERSION,
    )


def test_ingest_handler_downloads_with_fake_transport_and_publishes(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    context = _context(runtime)
    fake_clock = FakeClock()
    transport = FakeHttpTransport([SYNTHETIC_CSV_WITH_ODDS])
    context.bind_test_dependencies(
        transport=transport,
        sleeper=fake_clock.sleep,
        monotonic_clock=fake_clock.monotonic,
        clock=lambda: OBSERVED_AT,
    )

    result = ingest_football_data_csv_handler(context, _payload())

    assert result["snapshot_status"] == "ready"
    assert result["snapshot_reused"] is False
    assert result["snapshot_type"] == FOOTBALL_INGESTION_SNAPSHOT_TYPE
    assert result["schema_version"] == FOOTBALL_CANONICAL_SCHEMA_VERSION
    assert result["competition_id"] == "eng-premier-league"
    assert result["season_label"] == "2023-2024"
    assert result["season_id"] == "eng-premier-league:2023-2024"
    assert result["events_count"] == 1
    assert result["participants_count"] == 2
    assert result["market_quotes_count"] == 3
    assert result["post_match_statistics_count"] == 1
    assert result["unresolved_event_count"] == 0
    assert len(transport.calls) == 1
    records = _snapshot_records(runtime)
    assert len(records) == 1
    assert records[0].status is SnapshotStatus.READY
    verification = verify_snapshot_directory(
        snapshots_directory=runtime.paths.snapshots_directory,
        relative_manifest_path=str(result["snapshot_relative_path"]),
        suite=_suite(),
        expected_snapshot=records[0],
    )
    assert verification.row_counts == EXPECTED_ROW_COUNTS
    assert verification.file_count == len(EXPECTED_ROW_COUNTS)
    assert verification.primary_dataset_name == "events"


def test_ingest_handler_publishes_expected_filesystem_layout(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    raw_sha = _stored_raw(runtime)
    context = _context(runtime)
    context.bind_test_dependencies(clock=lambda: OBSERVED_AT)

    result = ingest_football_data_csv_handler(context, _payload(raw_sha))

    snapshot_id = str(result["snapshot_id"])
    expected_relative_path = (
        f"{FOOTBALL_INGESTION_SNAPSHOT_TYPE}/{FOOTBALL_CANONICAL_SCHEMA_VERSION}/"
        f"eng-premier-league/2023-2024/{snapshot_id}/manifest.json"
    )
    assert result["snapshot_relative_path"] == expected_relative_path
    snapshot_directory = runtime.paths.snapshots_directory / Path(expected_relative_path).parent
    assert snapshot_directory.is_dir()
    assert {path.name for path in snapshot_directory.iterdir()} == EXPECTED_DIRECTORY_FILES
    assert [path.name for path in runtime.paths.snapshots_directory.iterdir()] == [
        FOOTBALL_INGESTION_SNAPSHOT_TYPE
    ]


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
    assert result["events_count"] == 1
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
    assert second["snapshot_relative_path"] == first["snapshot_relative_path"]
    assert second["events_count"] == first["events_count"]
    assert second["market_quotes_count"] == first["market_quotes_count"]
    assert len(_snapshot_records(runtime)) == 1


def test_ingest_handler_stops_before_acquisition_when_shutdown_requested(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    context = _context(runtime)
    transport = FakeHttpTransport([SYNTHETIC_CSV_WITH_ODDS])
    context.bind_test_dependencies(
        transport=transport,
        sleeper=lambda _seconds: None,
        monotonic_clock=lambda: 0.0,
        clock=lambda: OBSERVED_AT,
    )
    context.request_stop()

    with pytest.raises(WorkerShutdownError, match="worker shutdown requested"):
        ingest_football_data_csv_handler(context, _payload())

    assert transport.calls == []
    assert _snapshot_records(runtime) == []


def test_ingest_handler_fences_lost_lease_and_leaves_no_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    raw_sha = _stored_raw(runtime)
    context = _context(runtime)
    context.bind_test_dependencies(clock=lambda: OBSERVED_AT)
    original_checkpoint = JobExecutionContext.checkpoint
    checkpoints: list[int] = []
    # The fifth checkpoint runs after the prepared snapshot directory exists but
    # before publication, so fencing must discard the prepared tree.
    lease_lost_at_checkpoint = 5

    def counting_checkpoint(self: JobExecutionContext) -> None:
        checkpoints.append(len(checkpoints) + 1)
        if len(checkpoints) == lease_lost_at_checkpoint:
            self.report_lease_lost()
        original_checkpoint(self)

    monkeypatch.setattr(JobExecutionContext, "checkpoint", counting_checkpoint)

    with pytest.raises(JobLeaseError, match="job lease lost"):
        ingest_football_data_csv_handler(context, _payload(raw_sha))

    assert len(checkpoints) == lease_lost_at_checkpoint
    assert _snapshot_records(runtime) == []
    assert list(runtime.paths.snapshots_directory.iterdir()) == []


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
        logger=logging.getLogger("test"),
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

    assert _snapshot_records(runtime) == []


def test_ingest_handler_maps_missing_source_resource_to_permanent_job(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    context = _context(runtime)
    context.bind_test_dependencies(
        transport=FakeHttpTransport([(404, b"missing", "text/plain")]),
        sleeper=lambda _seconds: None,
        monotonic_clock=lambda: 0.0,
        clock=lambda: OBSERVED_AT,
    )

    with pytest.raises(PermanentJobError, match="not found"):
        ingest_football_data_csv_handler(context, _payload())

    assert _snapshot_records(runtime) == []


def test_default_registry_contains_ingestion_handler() -> None:
    registry = build_default_registry()

    assert registry.get(INGEST_FOOTBALL_DATA_CSV_JOB_TYPE) is ingest_football_data_csv_handler
