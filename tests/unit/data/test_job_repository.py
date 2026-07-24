"""Tests for job repository foundation and transitions."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from sports_analytics.core.exceptions import DatabaseIntegrityError, RepositoryError
from sports_analytics.data.codec import dumps_canonical_json
from sports_analytics.data.database import connect_database, transaction
from sports_analytics.data.migrations import ensure_database_ready
from sports_analytics.data.repositories.jobs import JobRepository
from sports_analytics.data.types import JobStatus

FIXED = datetime(2026, 7, 24, 19, 30, 0, tzinfo=UTC)
JOB_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


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
        with transaction(connection):
            job = repo.create_job(
                job_id=JOB_ID,
                job_type="ingest.refresh",
                payload={},
                maximum_attempts=2,
                actor="cli",
                created_at=FIXED,
            )
            running = repo.transition_job(
                job.id,
                expected_status=JobStatus.PENDING,
                expected_version=1,
                new_status=JobStatus.RUNNING,
                actor="worker",
                occurred_at=FIXED.replace(microsecond=1),
            )
            assert running.status is JobStatus.RUNNING
            assert running.attempts == 1
            assert running.version == 2
            succeeded = repo.transition_job(
                job.id,
                expected_status=JobStatus.RUNNING,
                expected_version=2,
                new_status=JobStatus.SUCCEEDED,
                actor="worker",
                occurred_at=FIXED.replace(microsecond=2),
                result={"ok": True},
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
        with pytest.raises(DatabaseIntegrityError):
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
        with transaction(connection):
            job = repo.create_job(
                job_type="ingest.refresh",
                payload={},
                maximum_attempts=1,
                actor="cli",
                created_at=FIXED,
            )
            running = repo.transition_job(
                job.id,
                expected_status=JobStatus.PENDING,
                expected_version=1,
                new_status=JobStatus.RUNNING,
                actor="worker",
                occurred_at=FIXED.replace(microsecond=1),
            )
            with pytest.raises(RepositoryError, match="last_error"):
                repo.transition_job(
                    running.id,
                    expected_status=JobStatus.RUNNING,
                    expected_version=2,
                    new_status=JobStatus.FAILED,
                    actor="worker",
                    occurred_at=FIXED.replace(microsecond=2),
                )
            failed = repo.transition_job(
                running.id,
                expected_status=JobStatus.RUNNING,
                expected_version=2,
                new_status=JobStatus.FAILED,
                actor="worker",
                occurred_at=FIXED.replace(microsecond=2),
                last_error="boom",
            )
            assert failed.last_error == "boom"
            with pytest.raises(RepositoryError, match="maximum_attempts"):
                repo.transition_job(
                    failed.id,
                    expected_status=JobStatus.FAILED,
                    expected_version=3,
                    new_status=JobStatus.PENDING,
                    actor="worker",
                    occurred_at=FIXED.replace(microsecond=3),
                )
