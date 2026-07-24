"""Tests for job repository foundation and transitions."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from sports_analytics.core.exceptions import DatabaseIntegrityError, RepositoryError
from sports_analytics.data.codec import dumps_canonical_json
from sports_analytics.data.database import connect_database, transaction
from sports_analytics.data.migrations import ensure_database_ready
from sports_analytics.data.repositories.job_queue import JobQueueRepository
from sports_analytics.data.repositories.jobs import JobRepository
from sports_analytics.data.repositories.workers import WorkerRepository
from sports_analytics.data.types import JobStatus
from sports_analytics.jobs.types import JobClaim, WorkerStatus

FIXED = datetime(2026, 7, 24, 19, 30, 0, tzinfo=UTC)
JOB_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
WORKER_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def _register_running_worker(connection) -> str:
    worker_repo = WorkerRepository(connection)
    worker = worker_repo.register_worker(
        worker_id=WORKER_ID,
        name="test-worker",
        process_id=1234,
        hostname="test-host",
        started_at=FIXED,
        heartbeat_at=FIXED,
        capabilities={"job_types": ["ingest.refresh", "demo.job", "demo.job2"]},
        status=WorkerStatus.RUNNING,
    )
    return worker.id


def _claim_next_job(connection, *, worker_id: str = WORKER_ID) -> JobClaim:
    claim = JobQueueRepository(connection).claim_next_job(
        worker_id=worker_id,
        claimed_at=FIXED.replace(microsecond=1),
        lease_duration_seconds=60,
        actor=worker_id,
    )
    assert claim is not None
    return claim


def test_create_read_defaults_and_event(tmp_path: Path) -> None:
    db = tmp_path / "ops.sqlite3"
    ensure_database_ready(db)
    with connect_database(db) as connection:
        repo = JobRepository(connection)
        with transaction(connection):
            job = repo.create_job(
                job_id=JOB_ID.upper(),
                job_type="ingest.refresh",
                payload={"b": 2, "a": 1},
                maximum_attempts=3,
                actor="cli",
                created_at=FIXED,
            )
        assert job.id == JOB_ID
        assert job.status is JobStatus.PENDING
        assert job.priority == 100
        assert job.attempts == 0
        assert job.version == 1
        assert job.available_at == FIXED
        assert dumps_canonical_json(job.payload) == '{"a":1,"b":2}'
        events = repo.list_job_events(job.id)
        assert len(events) == 1
        assert events[0].event_type == "created"
        assert events[0].to_status is JobStatus.PENDING


def test_create_rollback_removes_job_and_event(tmp_path: Path) -> None:
    db = tmp_path / "ops.sqlite3"
    ensure_database_ready(db)
    with connect_database(db) as connection:
        repo = JobRepository(connection)
        with pytest.raises(RuntimeError):
            with transaction(connection):
                repo.create_job(
                    job_type="ingest.refresh",
                    payload={},
                    maximum_attempts=1,
                    actor="cli",
                    created_at=FIXED,
                )
                raise RuntimeError("rollback")
        assert repo.count_jobs() == 0
        assert connection.execute("SELECT COUNT(*) AS c FROM job_events").fetchone()["c"] == 0


def test_idempotency_same_and_conflict(tmp_path: Path) -> None:
    db = tmp_path / "ops.sqlite3"
    ensure_database_ready(db)
    with connect_database(db) as connection:
        repo = JobRepository(connection)
        with transaction(connection):
            first = repo.create_job(
                job_type="ingest.refresh",
                payload={"x": 1},
                maximum_attempts=2,
                actor="cli",
                idempotency_key="key-1",
                created_at=FIXED,
            )
            second = repo.create_job(
                job_type="ingest.refresh",
                payload={"x": 1},
                maximum_attempts=2,
                actor="cli",
                idempotency_key="key-1",
                created_at=FIXED,
            )
        assert first.id == second.id
        assert repo.count_jobs() == 1
        with pytest.raises(DatabaseIntegrityError):
            with transaction(connection):
                repo.create_job(
                    job_type="ingest.refresh",
                    payload={"x": 2},
                    maximum_attempts=2,
                    actor="cli",
                    idempotency_key="key-1",
                    created_at=FIXED,
                )


def test_list_count_pagination_and_ordering(tmp_path: Path) -> None:
    db = tmp_path / "ops.sqlite3"
    ensure_database_ready(db)
    with connect_database(db) as connection:
        repo = JobRepository(connection)
        with transaction(connection):
            repo.create_job(
                job_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1",
                job_type="a.job",
                payload={},
                maximum_attempts=1,
                actor="cli",
                created_at=FIXED,
            )
            repo.create_job(
                job_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa2",
                job_type="b.job",
                payload={},
                maximum_attempts=1,
                actor="cli",
                created_at=FIXED.replace(microsecond=1),
            )
        listed = repo.list_jobs(limit=1, offset=0)
        assert len(listed) == 1
        assert listed[0].id.endswith("2")
        assert repo.count_jobs(job_type="a.job") == 1
        with pytest.raises(RepositoryError):
            repo.list_jobs(limit=-1)


def test_transitions_and_failures(tmp_path: Path) -> None:
    db = tmp_path / "ops.sqlite3"
    ensure_database_ready(db)
    with connect_database(db) as connection:
        repo = JobRepository(connection)
        queue = JobQueueRepository(connection)
        with transaction(connection):
            job = repo.create_job(
                job_id=JOB_ID,
                job_type="ingest.refresh",
                payload={},
                maximum_attempts=2,
                actor="cli",
                created_at=FIXED,
            )
            _register_running_worker(connection)

            with pytest.raises(RepositoryError, match="claim_next_job"):
                repo.transition_job(
                    job.id,
                    expected_status=JobStatus.PENDING,
                    expected_version=1,
                    new_status=JobStatus.RUNNING,
                    actor="worker",
                    occurred_at=FIXED.replace(microsecond=1),
                )
            running = _claim_next_job(connection)
            assert running.job.status is JobStatus.RUNNING
            assert running.job.attempts == 1
            assert running.job.version == 2

            with pytest.raises(RepositoryError, match="complete_claimed_job|fail_claimed_job"):
                repo.transition_job(
                    job.id,
                    expected_status=JobStatus.RUNNING,
                    expected_version=2,
                    new_status=JobStatus.SUCCEEDED,
                    actor="worker",
                    occurred_at=FIXED.replace(microsecond=2),
                    result={"ok": True},
                )
            succeeded = queue.complete_claimed_job(
                job_id=job.id,
                worker_id=WORKER_ID,
                expected_job_version=running.job.version,
                completed_at=FIXED.replace(microsecond=2),
                result={"ok": True},
                actor="worker",
            )
            assert succeeded.status is JobStatus.SUCCEEDED
            assert succeeded.finished_at is not None
            assert succeeded.result == {"ok": True}
            events = repo.list_job_events(job.id)
            assert events[-1].job_version == 3

        with pytest.raises(RepositoryError):
            with transaction(connection):
                repo.transition_job(
                    job.id,
                    expected_status=JobStatus.SUCCEEDED,
                    expected_version=3,
                    new_status=JobStatus.PENDING,
                    actor="worker",
                    occurred_at=FIXED,
                )

        before_events = connection.execute("SELECT COUNT(*) AS c FROM job_events").fetchone()["c"]
        with pytest.raises(RepositoryError, match="fail_claimed_job"):
            with transaction(connection):
                repo.transition_job(
                    job.id,
                    expected_status=JobStatus.RUNNING,
                    expected_version=3,
                    new_status=JobStatus.FAILED,
                    actor="worker",
                    occurred_at=FIXED,
                    last_error="stale",
                )
        after = repo.get_job(job.id)
        assert after is not None and after.status is JobStatus.SUCCEEDED
        assert (
            connection.execute("SELECT COUNT(*) AS c FROM job_events").fetchone()["c"]
            == before_events
        )


def test_retry_and_failed_requirements(tmp_path: Path) -> None:
    db = tmp_path / "ops.sqlite3"
    ensure_database_ready(db)
    with connect_database(db) as connection:
        repo = JobRepository(connection)
        queue = JobQueueRepository(connection)
        with transaction(connection):
            _register_running_worker(connection)
            repo.create_job(
                job_type="ingest.refresh",
                payload={},
                maximum_attempts=1,
                actor="cli",
                created_at=FIXED,
            )
            running = _claim_next_job(connection)
            with pytest.raises(RepositoryError, match="error text"):
                queue.fail_claimed_job(
                    job_id=running.job.id,
                    worker_id=WORKER_ID,
                    expected_job_version=running.job.version,
                    failed_at=FIXED.replace(microsecond=2),
                    error="",
                    retryable=False,
                    actor="worker",
                    retry_backoff_base_seconds=5,
                    retry_backoff_max_seconds=300,
                )
            failed = queue.fail_claimed_job(
                job_id=running.job.id,
                worker_id=WORKER_ID,
                expected_job_version=running.job.version,
                failed_at=FIXED.replace(microsecond=2),
                error="boom",
                retryable=True,
                actor="worker",
                retry_backoff_base_seconds=5,
                retry_backoff_max_seconds=300,
            )
            assert failed.job.status is JobStatus.FAILED
            assert failed.job.last_error == "boom"
            with pytest.raises(RepositoryError, match="retry=True"):
                repo.transition_job(
                    failed.job.id,
                    expected_status=JobStatus.FAILED,
                    expected_version=3,
                    new_status=JobStatus.PENDING,
                    actor="worker",
                    occurred_at=FIXED.replace(microsecond=3),
                )
            with pytest.raises(RepositoryError, match="maximum_attempts"):
                repo.transition_job(
                    failed.job.id,
                    expected_status=JobStatus.FAILED,
                    expected_version=3,
                    new_status=JobStatus.PENDING,
                    actor="worker",
                    occurred_at=FIXED.replace(microsecond=3),
                    retry=True,
                )
