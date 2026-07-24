"""Tests for the sequential local durable-job worker runner."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sports_analytics.core.exceptions import PermanentJobError, RetryableJobError, WorkerError
from sports_analytics.core.runtime import RuntimeContext, bootstrap_runtime
from sports_analytics.data.database import connect_database, transaction
from sports_analytics.data.repositories.jobs import JobRepository
from sports_analytics.data.types import JobStatus
from sports_analytics.jobs.context import JobExecutionContext
from sports_analytics.jobs.registry import HandlerRegistry
from sports_analytics.jobs.runner import LocalWorker
from sports_analytics.jobs.types import JobExecutionState, WorkerStatus

FIXED = datetime(2026, 7, 24, 19, 30, 0, tzinfo=UTC)
WORKER_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
JOB_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


class FakeClock:
    def __init__(self, start: datetime = FIXED) -> None:
        self._current = start

    def __call__(self) -> datetime:
        value = self._current
        self._current = self._current + timedelta(seconds=1)
        return value


class FakeMonotonic:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


class FakeSleeper:
    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def _runtime(tmp_path: Path) -> RuntimeContext:
    return bootstrap_runtime(
        "worker",
        base_directory=tmp_path,
        environ={},
        overrides={
            "logging": {"file_enabled": False},
            "worker": {
                "poll_interval_seconds": 1,
                "heartbeat_interval_seconds": 0.5,
                "stale_job_timeout_seconds": 10,
                "shutdown_grace_seconds": 1,
            },
        },
    )


def _create_job(context: RuntimeContext, *, maximum_attempts: int = 2) -> str:
    with connect_database(context.database_path) as connection:
        with transaction(connection, immediate=True):
            job = JobRepository(connection).create_job(
                job_id=JOB_ID,
                job_type="demo.job",
                payload={"ok": True},
                maximum_attempts=maximum_attempts,
                actor="test",
                created_at=FIXED,
            )
            return job.id


def _registry(handler) -> HandlerRegistry:
    registry = HandlerRegistry()
    registry.register("demo.job", handler)
    return registry


def _read_job(context: RuntimeContext):
    with connect_database(context.database_path, read_only=True) as connection:
        return JobRepository(connection).get_job(JOB_ID)


def test_worker_once_no_job_stops_without_sleeping(tmp_path: Path) -> None:
    context = _runtime(tmp_path)
    sleeper = FakeSleeper()
    worker = LocalWorker(
        clock=FakeClock(),
        sleeper=sleeper,
        monotonic=FakeMonotonic(),
        pid=1234,
        hostname="test-host",
        uuid_factory=lambda: WORKER_ID,
        install_signals=False,
    )

    result = worker.run(context, registry=HandlerRegistry(), once=True)

    assert result.worker_id == WORKER_ID
    assert result.jobs_processed == 0
    assert result.stop_reason == "once_no_job"
    assert result.status is WorkerStatus.STOPPED
    assert sleeper.calls == []


def test_worker_claims_and_completes_registered_job(tmp_path: Path) -> None:
    context = _runtime(tmp_path)
    _create_job(context)

    def handler(job_context: JobExecutionContext, payload: object) -> dict[str, object]:
        assert job_context.job_id == JOB_ID
        return {"payload": payload, "attempt": job_context.attempt}

    worker = LocalWorker(
        clock=FakeClock(),
        sleeper=FakeSleeper(),
        monotonic=FakeMonotonic(),
        pid=1234,
        hostname="test-host",
        uuid_factory=lambda: WORKER_ID,
        install_signals=False,
    )

    result = worker.run(context, registry=_registry(handler), once=True)
    job = _read_job(context)

    assert result.jobs_processed == 1
    assert result.stop_reason == "once"
    assert job is not None
    assert job.status is JobStatus.SUCCEEDED
    assert job.result == {"attempt": 1, "payload": {"ok": True}}


def test_retryable_and_permanent_handler_errors_finalize_jobs(tmp_path: Path) -> None:
    context = _runtime(tmp_path)
    _create_job(context, maximum_attempts=2)

    def retryable(_context: JobExecutionContext, _payload: object) -> object:
        raise RetryableJobError("try again")

    worker = LocalWorker(
        clock=FakeClock(),
        sleeper=FakeSleeper(),
        monotonic=FakeMonotonic(),
        pid=1234,
        hostname="test-host",
        uuid_factory=lambda: WORKER_ID,
        install_signals=False,
    )
    result = worker.run(context, registry=_registry(retryable), once=True)
    job = _read_job(context)
    assert result.jobs_processed == 1
    assert job is not None and job.status is JobStatus.PENDING
    assert job.last_error == "RetryableJobError: try again"

    with connect_database(context.database_path) as connection:
        with transaction(connection, immediate=True):
            claimable = JobRepository(connection).get_job(JOB_ID)
            assert claimable is not None
            connection.execute("DELETE FROM job_events WHERE job_id = ?", (claimable.id,))
            connection.execute("DELETE FROM jobs WHERE id = ?", (claimable.id,))
            JobRepository(connection).create_job(
                job_id=JOB_ID,
                job_type="demo.job",
                payload={},
                maximum_attempts=2,
                actor="test",
                created_at=FIXED,
            )

    def permanent(_context: JobExecutionContext, _payload: object) -> object:
        raise PermanentJobError("no retry")

    worker = LocalWorker(
        clock=FakeClock(),
        sleeper=FakeSleeper(),
        monotonic=FakeMonotonic(),
        pid=1234,
        hostname="test-host",
        uuid_factory=lambda: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb2",
        install_signals=False,
    )
    result = worker.run(context, registry=_registry(permanent), once=True)
    job = _read_job(context)
    assert result.jobs_processed == 1
    assert job is not None and job.status is JobStatus.FAILED
    assert job.last_error == "PermanentJobError: no retry"


def test_invalid_runner_arguments_are_rejected(tmp_path: Path) -> None:
    context = _runtime(tmp_path)
    worker = LocalWorker(install_signals=False)
    with pytest.raises(WorkerError, match="once"):
        worker.run(context, once="yes")  # type: ignore[arg-type]
    with pytest.raises(WorkerError, match="max_jobs"):
        worker.run(context, max_jobs=0)


def test_state_from_finalization_mapping() -> None:
    from sports_analytics.jobs.types import JobFinalizationKind

    assert (
        LocalWorker._state_from_finalization(JobFinalizationKind.RETRY_SCHEDULED)
        is JobExecutionState.RETRY_SCHEDULED
    )
    assert (
        LocalWorker._state_from_finalization(JobFinalizationKind.FAILED)
        is JobExecutionState.FAILED
    )
    assert (
        LocalWorker._state_from_finalization(JobFinalizationKind.SUCCEEDED)
        is JobExecutionState.SUCCEEDED
    )
