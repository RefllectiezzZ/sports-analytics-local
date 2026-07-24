"""Tests for durable worker-instance repository operations."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sports_analytics.core.exceptions import DatabaseIntegrityError, RepositoryError
from sports_analytics.data.database import connect_database, transaction
from sports_analytics.data.migrations import ensure_database_ready
from sports_analytics.data.repositories.jobs import JobRepository
from sports_analytics.data.repositories.workers import WorkerRepository
from sports_analytics.jobs.types import WorkerStatus

FIXED = datetime(2026, 7, 24, 19, 30, 0, tzinfo=UTC)
WORKER_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
JOB_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def _register(
    repository: WorkerRepository,
    *,
    worker_id: str = WORKER_ID,
    status: WorkerStatus = WorkerStatus.STARTING,
    heartbeat_at: datetime = FIXED,
):
    return repository.register_worker(
        worker_id=worker_id,
        name="local-worker",
        process_id=1234,
        hostname="test-host",
        started_at=FIXED,
        heartbeat_at=heartbeat_at,
        capabilities={"job_types": ["demo.job"]},
        status=status,
    )


def test_register_get_list_and_count(tmp_path: Path) -> None:
    db = tmp_path / "ops.sqlite3"
    ensure_database_ready(db)
    with connect_database(db) as connection:
        repository = WorkerRepository(connection)
        with transaction(connection):
            worker = _register(repository, worker_id=WORKER_ID.upper())

        assert worker.id == WORKER_ID
        assert worker.status is WorkerStatus.STARTING
        assert worker.version == 1
        assert worker.current_job_id is None
        assert worker.capabilities == {"job_types": ["demo.job"]}
        assert repository.get_worker(WORKER_ID) == worker
        assert repository.count_workers() == 1
        assert repository.count_workers(status=WorkerStatus.STARTING) == 1
        assert repository.list_workers() == [worker]
        assert repository.list_workers(status=WorkerStatus.RUNNING) == []


def test_register_requires_transaction_and_valid_fields(tmp_path: Path) -> None:
    db = tmp_path / "ops.sqlite3"
    ensure_database_ready(db)
    with connect_database(db) as connection:
        repository = WorkerRepository(connection)
        with pytest.raises(RepositoryError, match="active explicit transaction"):
            _register(repository)
        with transaction(connection):
            with pytest.raises(RepositoryError, match="process_id"):
                repository.register_worker(
                    worker_id=WORKER_ID,
                    name="local-worker",
                    process_id=0,
                    hostname="test-host",
                    started_at=FIXED,
                    capabilities={},
                )
            with pytest.raises(RepositoryError, match="status"):
                repository.register_worker(
                    worker_id=WORKER_ID,
                    name="local-worker",
                    process_id=1,
                    hostname="test-host",
                    started_at=FIXED,
                    capabilities={},
                    status=WorkerStatus.STOPPED,
                )


def test_lifecycle_heartbeat_stop_and_fail(tmp_path: Path) -> None:
    db = tmp_path / "ops.sqlite3"
    ensure_database_ready(db)
    with connect_database(db) as connection:
        workers = WorkerRepository(connection)
        jobs = JobRepository(connection)
        with transaction(connection):
            worker = _register(workers)
            running = workers.mark_worker_running(
                worker.id,
                expected_version=worker.version,
                heartbeat_at=FIXED + timedelta(seconds=1),
            )
            assert running.status is WorkerStatus.RUNNING
            assert running.version == 2
            job = jobs.create_job(
                job_id=JOB_ID,
                job_type="demo.job",
                payload={},
                maximum_attempts=1,
                actor="cli",
                created_at=FIXED,
            )
            heartbeat = workers.heartbeat_worker(
                running.id,
                expected_version=running.version,
                heartbeat_at=FIXED + timedelta(seconds=2),
                current_job_id=job.id,
            )
            assert heartbeat.current_job_id == job.id
            assert heartbeat.version == 3
            stopping = workers.mark_worker_stopping(
                heartbeat.id,
                expected_version=heartbeat.version,
                stopping_at=FIXED + timedelta(seconds=3),
            )
            assert stopping.status is WorkerStatus.STOPPING
            stopped = workers.mark_worker_stopped(
                stopping.id,
                expected_version=stopping.version,
                stopped_at=FIXED + timedelta(seconds=4),
                shutdown_note="clean shutdown",
            )
            assert stopped.status is WorkerStatus.STOPPED
            assert stopped.current_job_id is None
            assert stopped.last_error == "clean shutdown"
            with pytest.raises(RepositoryError, match="terminal worker"):
                workers.mark_worker_failed(
                    stopped.id,
                    expected_version=stopped.version,
                    stopped_at=FIXED + timedelta(seconds=5),
                    error="too late",
                )


def test_optimistic_version_and_clear_current_job(tmp_path: Path) -> None:
    db = tmp_path / "ops.sqlite3"
    ensure_database_ready(db)
    with connect_database(db) as connection:
        repository = WorkerRepository(connection)
        with transaction(connection):
            worker = _register(repository, status=WorkerStatus.RUNNING)
            with pytest.raises(DatabaseIntegrityError, match="expected version"):
                repository.heartbeat_worker(
                    worker.id,
                    expected_version=worker.version + 1,
                    heartbeat_at=FIXED + timedelta(seconds=1),
                )
            with pytest.raises(RepositoryError, match="clear_current_job"):
                repository.heartbeat_worker(
                    worker.id,
                    expected_version=worker.version,
                    heartbeat_at=FIXED + timedelta(seconds=1),
                    current_job_id=JOB_ID,
                    clear_current_job=True,
                )


def test_reconcile_stale_workers_marks_running_and_stopping_failed(tmp_path: Path) -> None:
    db = tmp_path / "ops.sqlite3"
    ensure_database_ready(db)
    with connect_database(db) as connection:
        repository = WorkerRepository(connection)
        old = FIXED - timedelta(minutes=10)
        fresh = FIXED - timedelta(seconds=10)
        with transaction(connection):
            stale_running = _register(
                repository,
                worker_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb1",
                status=WorkerStatus.RUNNING,
                heartbeat_at=old,
            )
            stale_stopping = _register(
                repository,
                worker_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb2",
                status=WorkerStatus.RUNNING,
                heartbeat_at=old,
            )
            stale_stopping = repository.mark_worker_stopping(
                stale_stopping.id,
                expected_version=stale_stopping.version,
                stopping_at=old,
            )
            _register(
                repository,
                worker_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb3",
                status=WorkerStatus.RUNNING,
                heartbeat_at=fresh,
            )
            result = repository.reconcile_stale_workers(
                now=FIXED,
                stale_threshold_seconds=60,
                actor="supervisor",
            )

        assert result.scanned_count == 2
        assert result.failed_count == 2
        assert set(result.failed_worker_ids) == {stale_running.id, stale_stopping.id}
        assert repository.count_workers(status=WorkerStatus.FAILED) == 2
