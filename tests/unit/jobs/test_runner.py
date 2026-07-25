"""Tests for the sequential local durable-job worker runner."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sports_analytics.core.exceptions import (
    JobLeaseError,
    PermanentJobError,
    RetryableJobError,
    WorkerError,
)
from sports_analytics.core.runtime import RuntimeContext, bootstrap_runtime
from sports_analytics.data.database import connect_database, transaction
from sports_analytics.data.repositories.jobs import JobRepository
from sports_analytics.data.repositories.workers import WorkerRepository
from sports_analytics.data.types import JobStatus
from sports_analytics.jobs import runner as runner_module
from sports_analytics.jobs.context import JobExecutionContext
from sports_analytics.jobs.registry import HandlerRegistry
from sports_analytics.jobs.runner import LocalWorker
from sports_analytics.jobs.types import JobExecutionState, WorkerStatus

FIXED = datetime(2026, 7, 24, 19, 30, 0, tzinfo=UTC)
WORKER_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
SECOND_WORKER_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb2"
JOB_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
SECOND_JOB_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaab"


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
                "retry_backoff_base_seconds": 0.1,
                "retry_backoff_max_seconds": 0.1,
                "shutdown_grace_seconds": 1,
            },
        },
    )


def _create_job(
    context: RuntimeContext,
    *,
    job_id: str = JOB_ID,
    maximum_attempts: int = 2,
    priority: int = 100,
) -> str:
    with connect_database(context.database_path) as connection:
        with transaction(connection, immediate=True):
            job = JobRepository(connection).create_job(
                job_id=job_id,
                job_type="demo.job",
                payload={"ok": True},
                maximum_attempts=maximum_attempts,
                priority=priority,
                actor="test",
                created_at=FIXED,
            )
            return job.id


def _registry(handler) -> HandlerRegistry:
    registry = HandlerRegistry()
    registry.register("demo.job", handler)
    return registry


def _read_job(context: RuntimeContext, job_id: str = JOB_ID):
    with connect_database(context.database_path, read_only=True) as connection:
        return JobRepository(connection).get_job(job_id)


def _job_events(context: RuntimeContext, job_id: str = JOB_ID) -> list[str]:
    with connect_database(context.database_path, read_only=True) as connection:
        return [event.event_type for event in JobRepository(connection).list_job_events(job_id)]


def _worker_statuses(context: RuntimeContext) -> dict[str, WorkerStatus]:
    with connect_database(context.database_path, read_only=True) as connection:
        return {worker.id: worker.status for worker in WorkerRepository(connection).list_workers()}


class _LeaseLostOnStopController:
    def __init__(self, *, context: JobExecutionContext, **_kwargs: object) -> None:
        self._context = context

    def start(self) -> None:
        pass

    def stop(self, *, timeout_seconds: float) -> bool:
        del timeout_seconds
        self._context.report_lease_lost()
        return True


class _CleanupTimeoutController:
    def __init__(self, **_kwargs: object) -> None:
        pass

    def start(self) -> None:
        pass

    def stop(self, *, timeout_seconds: float) -> bool:
        del timeout_seconds
        return False


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


def test_once_and_max_jobs_combination_rejected_before_registration(tmp_path: Path) -> None:
    context = _runtime(tmp_path)
    worker = LocalWorker(install_signals=False)

    with pytest.raises(WorkerError, match="once and max_jobs"):
        worker.run(context, registry=HandlerRegistry(), once=True, max_jobs=1)

    with connect_database(context.database_path, read_only=True) as connection:
        assert WorkerRepository(connection).count_workers() == 0


def test_post_checkpoint_lease_loss_prevents_success_finalization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _runtime(tmp_path)
    _create_job(context)
    monkeypatch.setattr(runner_module, "LeaseHeartbeatController", _LeaseLostOnStopController)

    def handler(_job_context: JobExecutionContext, _payload: object) -> dict[str, bool]:
        return {"ok": True}

    worker = LocalWorker(
        clock=FakeClock(),
        sleeper=FakeSleeper(),
        monotonic=FakeMonotonic(),
        pid=1234,
        hostname="test-host",
        uuid_factory=lambda: WORKER_ID,
        install_signals=False,
    )

    with pytest.raises(JobLeaseError, match="lost lease"):
        worker.run(context, registry=_registry(handler), once=True)

    job = _read_job(context)
    assert job is not None
    assert job.status is JobStatus.RUNNING
    assert job.result is None
    assert _job_events(context) == ["created", "claimed"]
    assert _worker_statuses(context)[WORKER_ID] is WorkerStatus.FAILED


def test_controller_cleanup_timeout_prevents_failure_finalization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _runtime(tmp_path)
    _create_job(context)
    monkeypatch.setattr(runner_module, "LeaseHeartbeatController", _CleanupTimeoutController)

    def handler(_job_context: JobExecutionContext, _payload: object) -> object:
        raise PermanentJobError("do not finalize after cleanup timeout")

    worker = LocalWorker(
        clock=FakeClock(),
        sleeper=FakeSleeper(),
        monotonic=FakeMonotonic(),
        pid=1234,
        hostname="test-host",
        uuid_factory=lambda: WORKER_ID,
        install_signals=False,
    )

    with pytest.raises(JobLeaseError, match="lost lease"):
        worker.run(context, registry=_registry(handler), once=True)

    job = _read_job(context)
    assert job is not None
    assert job.status is JobStatus.RUNNING
    assert job.last_error is None
    assert _job_events(context) == ["created", "claimed"]
    assert _worker_statuses(context)[WORKER_ID] is WorkerStatus.FAILED


def test_runner_exits_on_lease_loss_and_recovery_processes_first_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _runtime(tmp_path)
    _create_job(context, job_id=JOB_ID, priority=1)
    _create_job(context, job_id=SECOND_JOB_ID, priority=100)
    original_controller = runner_module.LeaseHeartbeatController
    monkeypatch.setattr(runner_module, "LeaseHeartbeatController", _LeaseLostOnStopController)

    first_worker = LocalWorker(
        clock=FakeClock(),
        sleeper=FakeSleeper(),
        monotonic=FakeMonotonic(),
        pid=1234,
        hostname="test-host",
        uuid_factory=lambda: WORKER_ID,
        install_signals=False,
    )

    with pytest.raises(JobLeaseError, match="lost lease"):
        first_worker.run(
            context,
            registry=_registry(lambda _ctx, _payload: {"ok": True}),
            once=True,
        )

    first_after_loss = _read_job(context, JOB_ID)
    second_after_loss = _read_job(context, SECOND_JOB_ID)
    assert first_after_loss is not None
    assert second_after_loss is not None
    assert first_after_loss.status is JobStatus.RUNNING
    assert second_after_loss.status is JobStatus.PENDING
    assert _worker_statuses(context)[WORKER_ID] is WorkerStatus.FAILED

    monkeypatch.setattr(runner_module, "LeaseHeartbeatController", original_controller)
    processed: list[str] = []

    def handler(job_context: JobExecutionContext, _payload: object) -> dict[str, str]:
        processed.append(job_context.job_id)
        return {"job_id": job_context.job_id}

    recovery_worker = LocalWorker(
        clock=FakeClock(start=FIXED + timedelta(seconds=30)),
        sleeper=FakeSleeper(),
        monotonic=FakeMonotonic(),
        pid=1235,
        hostname="test-host",
        uuid_factory=lambda: SECOND_WORKER_ID,
        install_signals=False,
    )

    result = recovery_worker.run(context, registry=_registry(handler), once=True)

    assert result.jobs_processed == 1
    assert processed == [JOB_ID]
    recovered_first = _read_job(context, JOB_ID)
    untouched_second = _read_job(context, SECOND_JOB_ID)
    assert recovered_first is not None
    assert untouched_second is not None
    assert recovered_first.status is JobStatus.SUCCEEDED
    assert untouched_second.status is JobStatus.PENDING


def test_state_from_finalization_mapping() -> None:
    from sports_analytics.jobs.types import JobFinalizationKind

    assert (
        LocalWorker._state_from_finalization(JobFinalizationKind.RETRY_SCHEDULED)
        is JobExecutionState.RETRY_SCHEDULED
    )
    assert (
        LocalWorker._state_from_finalization(JobFinalizationKind.FAILED) is JobExecutionState.FAILED
    )
    assert (
        LocalWorker._state_from_finalization(JobFinalizationKind.SUCCEEDED)
        is JobExecutionState.SUCCEEDED
    )


def test_worker_signal_handler_only_sets_event_without_lock_or_logging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import logging
    import signal
    import threading

    context = _runtime(tmp_path)
    installed: dict[int, object] = {}
    originals_map = {signal.SIGINT: signal.SIG_DFL}

    def fake_getsignal(signum: int) -> object:
        return originals_map.get(signum, signal.SIG_DFL)

    def fake_signal(signum: int, handler: object) -> object:
        previous = originals_map.get(signum, signal.SIG_DFL)
        installed[signum] = handler
        originals_map[signum] = handler  # type: ignore[assignment]
        return previous

    monkeypatch.setattr(signal, "getsignal", fake_getsignal)
    monkeypatch.setattr(signal, "signal", fake_signal)

    worker = LocalWorker(install_signals=True)
    local_stop = threading.Event()
    active = JobExecutionContext(
        job_id=JOB_ID,
        worker_id=WORKER_ID,
        attempt=1,
        maximum_attempts=2,
        claimed_at=FIXED,
        lease_expires_at=FIXED + timedelta(seconds=10),
        logger=logging.getLogger("test.worker.signal"),
    )
    worker._active_context = active
    assert worker._active_context_lock.acquire(blocking=False)
    try:
        originals = worker._install_signal_handlers(context, local_stop)
        assert originals
        handler = installed[signal.SIGINT]
        assert callable(handler)
        # Holding the non-reentrant lock must not deadlock the signal callback.
        handler(signal.SIGINT, None)
        assert local_stop.is_set()
        assert not active.is_stop_requested()
    finally:
        worker._active_context_lock.release()

    assert worker._is_stop_requested(local_stop, None)
    assert active.is_stop_requested()
    worker._restore_signal_handlers(originals)
    assert originals_map[signal.SIGINT] is signal.SIG_DFL


def test_heartbeat_propagates_stop_and_continues_renewal_after_signal_event() -> None:
    import logging
    import threading

    from sports_analytics.jobs.runner import LeaseHeartbeatController

    class _Service:
        def __init__(self) -> None:
            self.calls = 0
            self.called = threading.Event()

        def renew_lease(self, **_kwargs: object) -> None:
            self.calls += 1
            self.called.set()

    service = _Service()
    context = JobExecutionContext(
        job_id=JOB_ID,
        worker_id=WORKER_ID,
        attempt=1,
        maximum_attempts=2,
        claimed_at=FIXED,
        lease_expires_at=FIXED,
        logger=logging.getLogger("test.worker.hb"),
    )
    local_stop = threading.Event()
    local_stop.set()
    controller = LeaseHeartbeatController(
        service=service,  # type: ignore[arg-type]
        context=context,
        expected_job_version=1,
        interval_seconds=0.001,
        clock=lambda: FIXED,
        should_stop=lambda: local_stop.is_set(),
    )
    controller.start()
    assert service.called.wait(timeout=1)
    assert controller.stop(timeout_seconds=1)
    assert context.is_stop_requested()
    assert service.calls >= 1
    assert not context.is_lease_lost()
