"""Tests for durable worker-instance repository operations."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sports_analytics.core.exceptions import DatabaseIntegrityError, JobLeaseError, RepositoryError
from sports_analytics.data.codec import format_utc_timestamp
from sports_analytics.data.database import connect_database, transaction
from sports_analytics.data.migrations import ensure_database_ready
from sports_analytics.data.repositories.job_queue import JobQueueRepository
from sports_analytics.data.repositories.jobs import JobRepository
from sports_analytics.data.repositories.workers import WorkerRepository
from sports_analytics.data.types import JobStatus
from sports_analytics.jobs.types import WorkerStatus

FIXED = datetime(2026, 7, 24, 19, 30, 0, tzinfo=UTC)
WORKER_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
SECOND_WORKER_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb2"
JOB_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
SECOND_JOB_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaab"


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


def test_lifecycle_heartbeat_preserves_current_job_and_stop_clears(tmp_path: Path) -> None:
    db = tmp_path / "ops.sqlite3"
    ensure_database_ready(db)
    with connect_database(db) as connection:
        workers = WorkerRepository(connection)
        jobs = JobRepository(connection)
        queue = JobQueueRepository(connection)
        with transaction(connection, immediate=True):
            worker = _register(workers)
            running = workers.mark_worker_running(
                worker.id,
                expected_version=worker.version,
                heartbeat_at=FIXED + timedelta(seconds=1),
            )
            assert running.status is WorkerStatus.RUNNING
            assert running.version == 2
            jobs.create_job(
                job_id=JOB_ID,
                job_type="demo.job",
                payload={},
                maximum_attempts=1,
                actor="cli",
                created_at=FIXED,
            )
            claim = queue.claim_next_job(
                worker_id=running.id,
                claimed_at=FIXED + timedelta(seconds=2),
                lease_duration_seconds=30,
                actor="cli",
            )
            assert claim is not None
            occupied = workers.get_worker(running.id)
            assert occupied is not None
            assert occupied.current_job_id == JOB_ID
            heartbeat = workers.heartbeat_worker(
                occupied.id,
                expected_version=occupied.version,
                heartbeat_at=FIXED + timedelta(seconds=3),
            )
            assert heartbeat.current_job_id == JOB_ID
            assert heartbeat.heartbeat_at == FIXED + timedelta(seconds=3)
            assert heartbeat.version == occupied.version + 1
            stopping = workers.mark_worker_stopping(
                heartbeat.id,
                expected_version=heartbeat.version,
                stopping_at=FIXED + timedelta(seconds=4),
            )
            assert stopping.status is WorkerStatus.STOPPING
            assert stopping.current_job_id == JOB_ID
            stopped = workers.mark_worker_stopped(
                stopping.id,
                expected_version=stopping.version,
                stopped_at=FIXED + timedelta(seconds=5),
                shutdown_note="clean shutdown",
            )
            assert stopped.status is WorkerStatus.STOPPED
            assert stopped.current_job_id is None
            assert stopped.last_error == "clean shutdown"
            with pytest.raises(RepositoryError, match="terminal worker"):
                workers.mark_worker_failed(
                    stopped.id,
                    expected_version=stopped.version,
                    stopped_at=FIXED + timedelta(seconds=6),
                    error="too late",
                )


def test_heartbeat_worker_rejects_assignment_bypass_kwargs(tmp_path: Path) -> None:
    db = tmp_path / "ops.sqlite3"
    ensure_database_ready(db)
    with connect_database(db) as connection:
        repository = WorkerRepository(connection)
        with transaction(connection):
            worker = _register(repository, status=WorkerStatus.RUNNING)
            with pytest.raises(TypeError):
                repository.heartbeat_worker(
                    worker.id,
                    expected_version=worker.version,
                    heartbeat_at=FIXED + timedelta(seconds=1),
                    current_job_id=JOB_ID,  # type: ignore[call-arg]
                )
            with pytest.raises(TypeError):
                repository.heartbeat_worker(
                    worker.id,
                    expected_version=worker.version,
                    heartbeat_at=FIXED + timedelta(seconds=1),
                    clear_current_job=True,  # type: ignore[call-arg]
                )


def test_optimistic_version_mismatch_on_heartbeat(tmp_path: Path) -> None:
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


def test_heartbeat_cannot_assign_replace_or_clear_and_second_claim_stays_impossible(
    tmp_path: Path,
) -> None:
    db = tmp_path / "ops.sqlite3"
    ensure_database_ready(db)
    with connect_database(db) as connection:
        workers = WorkerRepository(connection)
        jobs = JobRepository(connection)
        queue = JobQueueRepository(connection)
        with transaction(connection, immediate=True):
            worker = _register(workers, status=WorkerStatus.RUNNING)
            jobs.create_job(
                job_id=JOB_ID,
                job_type="demo.job",
                payload={},
                maximum_attempts=2,
                actor="cli",
                created_at=FIXED,
            )
            jobs.create_job(
                job_id=SECOND_JOB_ID,
                job_type="demo.job",
                payload={},
                maximum_attempts=2,
                actor="cli",
                created_at=FIXED,
            )
            idle = workers.heartbeat_worker(
                worker.id,
                expected_version=worker.version,
                heartbeat_at=FIXED + timedelta(seconds=1),
            )
            assert idle.current_job_id is None
            claim = queue.claim_next_job(
                worker_id=worker.id,
                claimed_at=FIXED + timedelta(seconds=2),
                lease_duration_seconds=60,
                actor="cli",
            )
            assert claim is not None

        with transaction(connection, immediate=True):
            occupied = workers.get_worker(worker.id)
            assert occupied is not None and occupied.current_job_id == JOB_ID
            preserved = workers.heartbeat_worker(
                occupied.id,
                expected_version=occupied.version,
                heartbeat_at=FIXED + timedelta(seconds=3),
            )
            assert preserved.current_job_id == JOB_ID
            assert preserved.heartbeat_at == FIXED + timedelta(seconds=3)

        with pytest.raises(sqlite3.IntegrityError, match="matching running lease"):
            with transaction(connection, immediate=True):
                connection.execute(
                    "UPDATE worker_instances SET current_job_id = ? WHERE id = ?",
                    (SECOND_JOB_ID, worker.id),
                )

        with transaction(connection, immediate=True):
            refreshed = workers.get_worker(worker.id)
            assert refreshed is not None and refreshed.current_job_id == JOB_ID
            with pytest.raises(JobLeaseError, match="already has current_job_id"):
                queue.claim_next_job(
                    worker_id=worker.id,
                    claimed_at=FIXED + timedelta(seconds=4),
                    lease_duration_seconds=60,
                    actor="cli",
                )
            second = jobs.get_job(SECOND_JOB_ID)
            assert second is not None
            assert second.status is JobStatus.PENDING
            assert second.attempts == 0


def test_direct_sql_current_job_requires_matching_running_lease(tmp_path: Path) -> None:
    db = tmp_path / "ops.sqlite3"
    ensure_database_ready(db)
    with connect_database(db) as connection:
        workers = WorkerRepository(connection)
        jobs = JobRepository(connection)
        queue = JobQueueRepository(connection)
        with transaction(connection, immediate=True):
            owner = _register(workers, worker_id=WORKER_ID, status=WorkerStatus.RUNNING)
            other = _register(
                workers,
                worker_id=SECOND_WORKER_ID,
                status=WorkerStatus.RUNNING,
            )
            jobs.create_job(
                job_id=JOB_ID,
                job_type="demo.job",
                payload={},
                maximum_attempts=1,
                actor="cli",
                created_at=FIXED,
            )

        with pytest.raises(sqlite3.IntegrityError, match="matching running lease"):
            with transaction(connection, immediate=True):
                connection.execute(
                    "UPDATE worker_instances SET current_job_id = ? WHERE id = ?",
                    (JOB_ID, owner.id),
                )

        with transaction(connection, immediate=True):
            claim = queue.claim_next_job(
                worker_id=owner.id,
                claimed_at=FIXED + timedelta(seconds=1),
                lease_duration_seconds=30,
                actor="cli",
            )
            assert claim is not None

        with pytest.raises(sqlite3.IntegrityError, match="matching running lease"):
            with transaction(connection, immediate=True):
                connection.execute(
                    "UPDATE worker_instances SET current_job_id = ? WHERE id = ?",
                    (JOB_ID, other.id),
                )

        with transaction(connection, immediate=True):
            owner_now = workers.get_worker(owner.id)
            assert owner_now is not None and owner_now.current_job_id == JOB_ID
            queue.complete_claimed_job(
                job_id=JOB_ID,
                worker_id=owner.id,
                expected_job_version=claim.job.version,
                completed_at=FIXED + timedelta(seconds=2),
                result={},
                actor="cli",
            )
            cleared = workers.get_worker(owner.id)
            assert cleared is not None and cleared.current_job_id is None


def test_worker_failure_and_recovery_clear_current_job(tmp_path: Path) -> None:
    db = tmp_path / "ops.sqlite3"
    ensure_database_ready(db)
    with connect_database(db) as connection:
        workers = WorkerRepository(connection)
        jobs = JobRepository(connection)
        queue = JobQueueRepository(connection)
        with transaction(connection, immediate=True):
            worker = _register(workers, status=WorkerStatus.RUNNING)
            jobs.create_job(
                job_id=JOB_ID,
                job_type="demo.job",
                payload={},
                maximum_attempts=2,
                actor="cli",
                created_at=FIXED,
            )
            claim = queue.claim_next_job(
                worker_id=worker.id,
                claimed_at=FIXED,
                lease_duration_seconds=10,
                actor="cli",
            )
            assert claim is not None
            occupied = workers.get_worker(worker.id)
            assert occupied is not None
            failed = workers.mark_worker_failed(
                worker.id,
                expected_version=occupied.version,
                stopped_at=FIXED + timedelta(seconds=1),
                error="boom",
            )
            assert failed.current_job_id is None
            assert failed.status is WorkerStatus.FAILED

            worker2 = _register(
                workers,
                worker_id=SECOND_WORKER_ID,
                status=WorkerStatus.RUNNING,
            )
            jobs.create_job(
                job_id=SECOND_JOB_ID,
                job_type="demo.job",
                payload={},
                maximum_attempts=2,
                actor="cli",
                created_at=FIXED,
            )
            claim2 = queue.claim_next_job(
                worker_id=worker2.id,
                claimed_at=FIXED,
                lease_duration_seconds=10,
                actor="cli",
            )
            assert claim2 is not None
            connection.execute(
                "UPDATE jobs SET lease_expires_at = ? WHERE id = ?",
                (format_utc_timestamp(FIXED - timedelta(seconds=1)), SECOND_JOB_ID),
            )
            result = queue.recover_expired_leases(
                recovered_at=FIXED,
                actor="recovery",
                retry_backoff_base_seconds=1,
                retry_backoff_max_seconds=1,
                maximum_rows=10,
            )
            assert result.requeued_count == 1
            cleared = workers.get_worker(worker2.id)
            assert cleared is not None and cleared.current_job_id is None


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
