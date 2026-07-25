"""Section 59: LocalWorker once-mode football ingestion E2E tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from sports_analytics.core.runtime import RuntimeContext, bootstrap_runtime
from sports_analytics.data.database import connect_database
from sports_analytics.data.repositories.jobs import JobRepository
from sports_analytics.data.repositories.snapshots import SnapshotRepository
from sports_analytics.data.types import JobStatus, SnapshotStatus
from sports_analytics.ingestion.football import enqueue_football_data_ingestion
from sports_analytics.ingestion.types import INGEST_FOOTBALL_DATA_CSV_JOB_TYPE
from sports_analytics.jobs.runner import LocalWorker
from sports_analytics.jobs.types import WorkerStatus
from sports_analytics.snapshots.reader import verify_snapshot_directory
from sports_analytics.sources.football_data_co_uk.catalog import build_csv_url
from sports_analytics.sources.raw_store import RawSourceStore
from sports_analytics.sources.types import SOURCE_FOOTBALL_DATA_CO_UK

OBSERVED_AT = datetime(2024, 1, 15, 12, 0, tzinfo=UTC)
SYNTHETIC_CSV = (
    b"Div,Date,HomeTeam,AwayTeam,FTHG,FTAG,FTR\n"
    b"E0,12/08/2023,Northbridge FC,Southport Athletic,2,1,H\n"
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
    assert completed.result["source_file_sha256"] == raw_sha
    assert completed.result["games_count"] == 1
    assert len(snapshots) == 1
    assert snapshots[0].status is SnapshotStatus.READY
    assert (
        verify_snapshot_directory(
            snapshots_directory=runtime.paths.snapshots_directory,
            relative_manifest_path=snapshots[0].relative_path,
            expected_snapshot=snapshots[0],
        ).games_count
        == 1
    )
