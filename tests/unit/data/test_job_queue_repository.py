"""Tests for lease-safe durable job queue operations."""

from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sports_analytics.core.exceptions import JobLeaseError, RepositoryError
from sports_analytics.data.database import connect_database, transaction
from sports_analytics.data.migrations import ensure_database_ready
from sports_analytics.data.repositories.job_queue import JobQueueRepository
from sports_analytics.data.repositories.jobs import JobRepository
from sports_analytics.data.repositories.workers import WorkerRepository
from sports_analytics.data.types import JobStatus
from sports_analytics.jobs.types import JobFinalizationKind, WorkerStatus

FIXED = datetime(2026, 7, 24, 19, 30, 0, tzinfo=UTC)
WORKER_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def _register_running_worker(connection, worker_id: str = WORKER_ID) -> str:
    worker = WorkerRepository(connection).register_worker(
        worker_id=worker_id,
        name="test-worker",
        process_id=1234,
        hostname="test-host",
        started_at=FIXED,
        heartbeat_at=FIXED,
        capabilities={"job_types": ["demo.job"]},
        status=WorkerStatus.RUNNING,
    )
    return worker.id


def _create_job(
    connection,
    *,
    job_id: str,
    priority: int = 100,
    available_at: datetime = FIXED,
    maximum_attempts: int = 2,
):
    return JobRepository(connection).create_job(
        job_id=job_id,
        job_type="demo.job",
        payload={"job_id": job_id},
        maximum_attempts=maximum_attempts,
        priority=priority,
        available_at=available_at,
        actor="cli",
        created_at=FIXED,
    )


def _claim(connection, *, worker_id: str = WORKER_ID, at: datetime = FIXED):
    claim = JobQueueRepository(connection).claim_next_job(
        worker_id=worker_id,
        claimed_at=at,
        lease_duration_seconds=60,
        actor=worker_id,
    )
    assert claim is not None
    return claim


def test_claim_next_job_orders_by_priority_availability_and_updates_worker(tmp_path: Path) -> None:
    db = tmp_path / "ops.sqlite3"
    ensure_database_ready(db)
    with connect_database(db) as connection:
        with transaction(connection, immediate=True):
            _register_running_worker(connection)
            later = _create_job(
                connection,
                job_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1",
                priority=1,
                available_at=FIXED + timedelta(minutes=1),
            )
            first = _create_job(
                connection,
                job_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa2",
                priority=5,
            )
            second = _create_job(
                connection,
                job_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa3",
                priority=10,
            )
            claim = _claim(connection, at=FIXED)

        assert claim.job.id == first.id
        assert claim.job.status is JobStatus.RUNNING
        assert claim.job.attempts == 1
        assert claim.job.lease_owner == WORKER_ID
        assert claim.job.lease_expires_at == FIXED + timedelta(seconds=60)
        worker = WorkerRepository(connection).get_worker(WORKER_ID)
        assert worker is not None and worker.current_job_id == first.id
        remaining = JobRepository(connection).list_jobs(status=JobStatus.PENDING)
        assert {job.id for job in remaining} == {later.id, second.id}


def test_occupied_worker_cannot_claim_second_job_and_unique_index_rolls_back(
    tmp_path: Path,
) -> None:
    db = tmp_path / "ops.sqlite3"
    ensure_database_ready(db)
    with connect_database(db) as connection:
        queue = JobQueueRepository(connection)
        with transaction(connection, immediate=True):
            _register_running_worker(connection)
            first = _create_job(
                connection,
                job_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1",
            )
            second = _create_job(
                connection,
                job_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa2",
            )
            claim = _claim(connection, at=FIXED)
            assert claim.job.id == first.id
            with pytest.raises(JobLeaseError, match="already has current_job_id"):
                queue.claim_next_job(
                    worker_id=WORKER_ID,
                    claimed_at=FIXED + timedelta(seconds=1),
                    lease_duration_seconds=60,
                    actor=WORKER_ID,
                )

        jobs = {job.id: job for job in JobRepository(connection).list_jobs()}
        assert jobs[second.id].status is JobStatus.PENDING
        assert jobs[second.id].attempts == 0
        first_events = JobRepository(connection).list_job_events(first.id)
        second_events = JobRepository(connection).list_job_events(second.id)
        assert [event.event_type for event in first_events] == [
            "created",
            "claimed",
        ]
        assert [event.event_type for event in second_events] == ["created"]

        with pytest.raises(sqlite3.IntegrityError):
            with transaction(connection, immediate=True):
                extra_worker = _register_running_worker(
                    connection,
                    "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb2",
                )
                connection.execute(
                    "UPDATE worker_instances SET current_job_id = ? WHERE id = ?",
                    (first.id, extra_worker),
                )
        assert (
            WorkerRepository(connection).get_worker("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb2") is None
        )


def test_claim_requires_running_worker_and_active_transaction(tmp_path: Path) -> None:
    db = tmp_path / "ops.sqlite3"
    ensure_database_ready(db)
    with connect_database(db) as connection:
        queue = JobQueueRepository(connection)
        with pytest.raises(RepositoryError, match="active explicit transaction"):
            queue.claim_next_job(
                worker_id=WORKER_ID,
                claimed_at=FIXED,
                lease_duration_seconds=60,
                actor="worker",
            )
        with transaction(connection, immediate=True):
            _create_job(connection, job_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1")
            with pytest.raises(RepositoryError, match="worker not found"):
                queue.claim_next_job(
                    worker_id=WORKER_ID,
                    claimed_at=FIXED,
                    lease_duration_seconds=60,
                    actor="worker",
                )
            worker = WorkerRepository(connection).register_worker(
                worker_id=WORKER_ID,
                name="test-worker",
                process_id=1234,
                hostname="test-host",
                started_at=FIXED,
                capabilities={},
                status=WorkerStatus.STARTING,
            )
            with pytest.raises(RepositoryError, match="expected running"):
                queue.claim_next_job(
                    worker_id=worker.id,
                    claimed_at=FIXED,
                    lease_duration_seconds=60,
                    actor="worker",
                )


def test_renew_complete_and_lease_fencing(tmp_path: Path) -> None:
    db = tmp_path / "ops.sqlite3"
    ensure_database_ready(db)
    with connect_database(db) as connection:
        queue = JobQueueRepository(connection)
        with transaction(connection, immediate=True):
            _register_running_worker(connection)
            _create_job(connection, job_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1")
            claim = _claim(connection, at=FIXED)
            renewed = queue.renew_job_lease(
                job_id=claim.job.id,
                worker_id=WORKER_ID,
                heartbeat_at=FIXED + timedelta(seconds=10),
                lease_duration_seconds=120,
                expected_job_version=claim.job.version,
            )
            assert renewed.version == claim.job.version
            assert renewed.lease_expires_at == FIXED + timedelta(seconds=130)
            with pytest.raises(JobLeaseError, match="does not match worker"):
                queue.complete_claimed_job(
                    job_id=claim.job.id,
                    worker_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb2",
                    expected_job_version=claim.job.version,
                    completed_at=FIXED + timedelta(seconds=20),
                    result={"ok": True},
                    actor="worker",
                )
            completed = queue.complete_claimed_job(
                job_id=claim.job.id,
                worker_id=WORKER_ID,
                expected_job_version=claim.job.version,
                completed_at=FIXED + timedelta(seconds=20),
                result={"ok": True},
                actor="worker",
            )

        assert completed.status is JobStatus.SUCCEEDED
        assert completed.lease_owner is None
        assert completed.result == {"ok": True}
        worker = WorkerRepository(connection).get_worker(WORKER_ID)
        assert worker is not None and worker.current_job_id is None
        events = JobRepository(connection).list_job_events(completed.id)
        assert [event.event_type for event in events] == ["created", "claimed", "succeeded"]


def test_fail_retry_schedule_and_terminal_failure(tmp_path: Path) -> None:
    db = tmp_path / "ops.sqlite3"
    ensure_database_ready(db)
    with connect_database(db) as connection:
        queue = JobQueueRepository(connection)
        with transaction(connection, immediate=True):
            _register_running_worker(connection)
            _create_job(
                connection,
                job_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1",
                maximum_attempts=2,
            )
            claim = _claim(connection, at=FIXED)
            outcome = queue.fail_claimed_job(
                job_id=claim.job.id,
                worker_id=WORKER_ID,
                expected_job_version=claim.job.version,
                failed_at=FIXED + timedelta(seconds=1),
                error="temporary",
                retryable=True,
                actor="worker",
                retry_backoff_base_seconds=5,
                retry_backoff_max_seconds=300,
                details={"kind": "transient"},
            )
            assert outcome.kind is JobFinalizationKind.RETRY_SCHEDULED
            assert outcome.job.status is JobStatus.PENDING
            assert outcome.job.available_at == FIXED + timedelta(seconds=6)
            assert outcome.job.last_error == "temporary"

            claim2 = _claim(connection, at=FIXED + timedelta(seconds=6))
            terminal = queue.fail_claimed_job(
                job_id=claim2.job.id,
                worker_id=WORKER_ID,
                expected_job_version=claim2.job.version,
                failed_at=FIXED + timedelta(seconds=7),
                error="permanent",
                retryable=True,
                actor="worker",
                retry_backoff_base_seconds=5,
                retry_backoff_max_seconds=300,
            )

        assert terminal.kind is JobFinalizationKind.FAILED
        assert terminal.job.status is JobStatus.FAILED
        assert terminal.job.finished_at == FIXED + timedelta(seconds=7)


def test_fail_claimed_job_rejects_non_bool_retryable_without_writes(tmp_path: Path) -> None:
    db = tmp_path / "ops.sqlite3"
    ensure_database_ready(db)
    with connect_database(db) as connection:
        queue = JobQueueRepository(connection)
        with transaction(connection, immediate=True):
            _register_running_worker(connection)
            _create_job(connection, job_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1")
            claim = _claim(connection, at=FIXED)
            before_events = JobRepository(connection).list_job_events(claim.job.id)
            with pytest.raises(RepositoryError, match="retryable must be a bool"):
                queue.fail_claimed_job(
                    job_id=claim.job.id,
                    worker_id=WORKER_ID,
                    expected_job_version=claim.job.version,
                    failed_at=FIXED + timedelta(seconds=1),
                    error="temporary",
                    retryable=1,  # type: ignore[arg-type]
                    actor="worker",
                    retry_backoff_base_seconds=5,
                    retry_backoff_max_seconds=300,
                )

        assert JobRepository(connection).get_job(claim.job.id) == claim.job
        assert JobRepository(connection).list_job_events(claim.job.id) == before_events


def test_fail_claimed_job_nests_hostile_details_under_details_key(tmp_path: Path) -> None:
    db = tmp_path / "ops.sqlite3"
    ensure_database_ready(db)
    hostile_details = {
        "worker_id": "evil",
        "attempt": 99,
        "maximum_attempts": 99,
        "error": "masked",
        "details": {"nested": "attacker"},
    }
    with connect_database(db) as connection:
        queue = JobQueueRepository(connection)
        with transaction(connection, immediate=True):
            _register_running_worker(connection)
            _create_job(connection, job_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1")
            claim = _claim(connection, at=FIXED)
            queue.fail_claimed_job(
                job_id=claim.job.id,
                worker_id=WORKER_ID,
                expected_job_version=claim.job.version,
                failed_at=FIXED + timedelta(seconds=1),
                error="temporary",
                retryable=True,
                actor="worker",
                retry_backoff_base_seconds=5,
                retry_backoff_max_seconds=300,
                details=hostile_details,
            )

        events = JobRepository(connection).list_job_events(claim.job.id)
        retry_event = events[-1]
        assert retry_event.event_type == "retry_scheduled"
        assert retry_event.details["worker_id"] == WORKER_ID
        assert retry_event.details["attempt"] == 1
        assert retry_event.details["maximum_attempts"] == 2
        assert retry_event.details["error"] == "temporary"
        assert retry_event.details["details"] == hostile_details


def test_recover_expired_leases_requeues_or_fails_and_clears_workers(tmp_path: Path) -> None:
    db = tmp_path / "ops.sqlite3"
    ensure_database_ready(db)
    with connect_database(db) as connection:
        queue = JobQueueRepository(connection)
        with transaction(connection, immediate=True):
            first_worker = _register_running_worker(
                connection,
                "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb1",
            )
            _create_job(
                connection,
                job_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1",
                available_at=FIXED - timedelta(minutes=3),
                maximum_attempts=2,
            )
            requeue_claim = _claim(
                connection,
                worker_id=first_worker,
                at=FIXED - timedelta(minutes=2),
            )
            second_worker = _register_running_worker(
                connection,
                "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb2",
            )
            _create_job(
                connection,
                job_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa2",
                available_at=FIXED - timedelta(minutes=3),
                maximum_attempts=1,
            )
            fail_claim = _claim(
                connection,
                worker_id=second_worker,
                at=FIXED - timedelta(minutes=2),
            )
            result = queue.recover_expired_leases(
                recovered_at=FIXED,
                actor="supervisor",
                retry_backoff_base_seconds=5,
                retry_backoff_max_seconds=300,
                maximum_rows=100,
            )

        assert result.scanned_count == 2
        assert result.requeued_job_ids == (requeue_claim.job.id,)
        assert result.failed_job_ids == (fail_claim.job.id,)
        jobs = {job.id: job for job in JobRepository(connection).list_jobs()}
        assert jobs[requeue_claim.job.id].status is JobStatus.PENDING
        assert jobs[fail_claim.job.id].status is JobStatus.FAILED
        for worker_id in (first_worker, second_worker):
            worker = WorkerRepository(connection).get_worker(worker_id)
            assert worker is not None and worker.current_job_id is None


def test_cancel_pending_and_failed_but_not_running(tmp_path: Path) -> None:
    db = tmp_path / "ops.sqlite3"
    ensure_database_ready(db)
    with connect_database(db) as connection:
        queue = JobQueueRepository(connection)
        with transaction(connection, immediate=True):
            _register_running_worker(connection)
            pending = _create_job(
                connection,
                job_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1",
            )
            cancelled = queue.cancel_pending_job(
                job_id=pending.id,
                expected_status=JobStatus.PENDING,
                expected_version=pending.version,
                cancelled_at=FIXED,
                actor="admin",
                details={"reason": "test"},
            )
            assert cancelled.status is JobStatus.CANCELLED

            failed_source = _create_job(
                connection,
                job_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa2",
                maximum_attempts=1,
            )
            claim = _claim(connection, at=FIXED)
            assert claim.job.id == failed_source.id
            failed = queue.fail_claimed_job(
                job_id=claim.job.id,
                worker_id=WORKER_ID,
                expected_job_version=claim.job.version,
                failed_at=FIXED + timedelta(seconds=1),
                error="boom",
                retryable=False,
                actor="worker",
                retry_backoff_base_seconds=5,
                retry_backoff_max_seconds=300,
            ).job
            cancelled_failed = queue.cancel_pending_job(
                job_id=failed.id,
                expected_status=JobStatus.FAILED,
                expected_version=failed.version,
                cancelled_at=FIXED + timedelta(seconds=2),
                actor="admin",
            )
            assert cancelled_failed.status is JobStatus.CANCELLED

            running_source = _create_job(
                connection,
                job_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa3",
            )
            running = _claim(connection, at=FIXED + timedelta(seconds=3))
            assert running.job.id == running_source.id
            with pytest.raises(RepositoryError, match="running jobs cannot"):
                queue.cancel_pending_job(
                    job_id=running.job.id,
                    expected_status=JobStatus.RUNNING,
                    expected_version=running.job.version,
                    cancelled_at=FIXED + timedelta(seconds=4),
                    actor="admin",
                )


def test_queue_status_counts_jobs_and_workers(tmp_path: Path) -> None:
    db = tmp_path / "ops.sqlite3"
    ensure_database_ready(db)
    with connect_database(db) as connection:
        queue = JobQueueRepository(connection)
        with transaction(connection, immediate=True):
            _register_running_worker(connection)
            _create_job(
                connection,
                job_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1",
                available_at=FIXED - timedelta(minutes=3),
            )
            _create_job(
                connection,
                job_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa2",
                available_at=FIXED + timedelta(minutes=5),
            )
            claim = _claim(connection, at=FIXED - timedelta(minutes=2))
            assert claim.job.status is JobStatus.RUNNING

        status = queue.get_queue_status(now=FIXED, stale_worker_threshold_seconds=60)
        assert status.pending_count == 1
        assert status.available_pending_count == 0
        assert status.delayed_pending_count == 1
        assert status.running_count == 1
        assert status.expired_running_lease_count == 1
        assert status.active_worker_count == 1
        assert status.stale_worker_count == 1


def test_concurrent_claim_fences_single_job_to_one_worker(tmp_path: Path) -> None:
    db = tmp_path / "ops.sqlite3"
    ensure_database_ready(db)
    with connect_database(db) as connection:
        with transaction(connection, immediate=True):
            _register_running_worker(connection, "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb1")
            _register_running_worker(connection, "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb2")
            _create_job(
                connection,
                job_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1",
            )

    barrier = threading.Barrier(2)
    claimed: list[tuple[str, str | None]] = []
    errors: list[BaseException] = []

    def worker(worker_id: str) -> None:
        try:
            barrier.wait(timeout=5)
            with connect_database(db) as connection:
                with transaction(connection, immediate=True):
                    claim = JobQueueRepository(connection).claim_next_job(
                        worker_id=worker_id,
                        claimed_at=FIXED,
                        lease_duration_seconds=60,
                        actor=worker_id,
                    )
                    claimed.append((worker_id, None if claim is None else claim.job.id))
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            errors.append(exc)

    threads = [
        threading.Thread(
            target=worker,
            args=(worker_id,),
        )
        for worker_id in (
            "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb1",
            "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb2",
        )
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    assert len(claimed) == 2
    assert [job_id for _worker_id, job_id in claimed].count(
        "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1"
    ) == 1
    assert [job_id for _worker_id, job_id in claimed].count(None) == 1
