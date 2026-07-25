"""Section 59: LocalWorker once-mode football ingestion E2E tests."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from sports_analytics.core.runtime import RuntimeContext, bootstrap_runtime
from sports_analytics.data.database import connect_database
from sports_analytics.data.repositories.jobs import JobRepository
from sports_analytics.data.repositories.snapshots import SnapshotRepository
from sports_analytics.data.types import JobStatus, SnapshotStatus
from sports_analytics.ingestion.football import enqueue_football_data_ingestion
from sports_analytics.ingestion.snapshot_specs import resolve_snapshot_suite
from sports_analytics.ingestion.types import INGEST_FOOTBALL_DATA_CSV_JOB_TYPE
from sports_analytics.jobs.runner import LocalWorker
from sports_analytics.jobs.types import WorkerStatus
from sports_analytics.snapshots.reader import verify_snapshot_directory
from sports_analytics.sports.football.contracts import (
    FOOTBALL_CANONICAL_SCHEMA_VERSION,
    FOOTBALL_INGESTION_SNAPSHOT_TYPE,
)
from tests.helpers_snapshots import OBSERVED_AT, SYNTHETIC_CSV_WITH_ODDS, store_artifact

EXPECTED_ROW_COUNTS = (
    ("competitions", 1),
    ("seasons", 1),
    ("participants", 2),
    ("source_participants", 2),
    ("events", 1),
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
        "events.parquet",
        "event_reconciliations.parquet",
        "market_quotes.parquet",
        "post_match_statistics.parquet",
    }
)


class IncrementingClock:
    def __init__(self, start: datetime = OBSERVED_AT) -> None:
        self._current = start

    def __call__(self) -> datetime:
        value = self._current
        self._current += timedelta(seconds=1)
        return value


class FakeMonotonic:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


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
                "poll_interval_seconds": 1,
                "heartbeat_interval_seconds": 0.5,
                "stale_job_timeout_seconds": 10,
                "retry_backoff_base_seconds": 0.1,
                "retry_backoff_max_seconds": 0.1,
                "shutdown_grace_seconds": 1,
            },
        },
    )


def _store_raw(runtime: RuntimeContext) -> str:
    artifact = store_artifact(
        runtime.paths.raw_directory,
        content=SYNTHETIC_CSV_WITH_ODDS,
    )
    return artifact.checksum_sha256


def test_local_worker_once_processes_enqueued_football_ingestion_from_cached_raw(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    raw_sha = _store_raw(runtime)
    job = enqueue_football_data_ingestion(
        database_path=runtime.database_path,
        scraping=runtime.settings.scraping,
        competition_id="eng-premier-league",
        season="2023-2024",
        raw_sha256=raw_sha,
        actor="test",
        created_at=OBSERVED_AT,
    )
    worker = LocalWorker(
        clock=IncrementingClock(),
        sleeper=lambda _seconds: None,
        monotonic=FakeMonotonic(),
        pid=1234,
        hostname="test-host",
        uuid_factory=lambda: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        install_signals=False,
    )

    result = worker.run(runtime, once=True)

    assert result.jobs_processed == 1
    assert result.stop_reason == "once"
    assert result.status is WorkerStatus.STOPPED
    with connect_database(runtime.database_path, read_only=True) as connection:
        completed = JobRepository(connection).get_job(job.id)
        snapshots = SnapshotRepository(connection).list_snapshots()

    assert completed is not None
    assert completed.job_type == INGEST_FOOTBALL_DATA_CSV_JOB_TYPE
    assert completed.status is JobStatus.SUCCEEDED
    assert completed.result is not None
    assert completed.result["snapshot_status"] == "ready"
    assert completed.result["snapshot_reused"] is False
    assert completed.result["source_file_sha256"] == raw_sha
    assert completed.result["snapshot_type"] == FOOTBALL_INGESTION_SNAPSHOT_TYPE
    assert completed.result["schema_version"] == FOOTBALL_CANONICAL_SCHEMA_VERSION
    assert completed.result["events_count"] == 1
    assert completed.result["participants_count"] == 2
    assert completed.result["market_quotes_count"] == 3
    assert completed.result["post_match_statistics_count"] == 1
    assert completed.result["unresolved_event_count"] == 0
    assert len(snapshots) == 1
    assert snapshots[0].status is SnapshotStatus.READY
    verification = verify_snapshot_directory(
        snapshots_directory=runtime.paths.snapshots_directory,
        relative_manifest_path=snapshots[0].relative_path,
        suite=resolve_snapshot_suite(
            snapshot_type=FOOTBALL_INGESTION_SNAPSHOT_TYPE,
            schema_version=FOOTBALL_CANONICAL_SCHEMA_VERSION,
        ),
        expected_snapshot=snapshots[0],
    )
    assert verification.row_counts == EXPECTED_ROW_COUNTS
    assert verification.file_count == len(EXPECTED_ROW_COUNTS)
    snapshot_directory = runtime.paths.snapshots_directory / Path(snapshots[0].relative_path).parent
    assert {path.name for path in snapshot_directory.iterdir()} == EXPECTED_DIRECTORY_FILES
    assert snapshots[0].relative_path.startswith(
        f"{FOOTBALL_INGESTION_SNAPSHOT_TYPE}/{FOOTBALL_CANONICAL_SCHEMA_VERSION}/"
        "eng-premier-league/2023-2024/"
    )
