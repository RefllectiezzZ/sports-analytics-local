"""Tests for job lease heartbeat controller."""

from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime

from sports_analytics.core.exceptions import JobLeaseError
from sports_analytics.jobs.context import JobExecutionContext
from sports_analytics.jobs.runner import LeaseHeartbeatController

FIXED = datetime(2026, 7, 24, 19, 30, 0, tzinfo=UTC)
JOB_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
WORKER_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def _context() -> JobExecutionContext:
    return JobExecutionContext(
        job_id=JOB_ID,
        worker_id=WORKER_ID,
        attempt=1,
        maximum_attempts=2,
        claimed_at=FIXED,
        lease_expires_at=FIXED,
        logger=logging.getLogger("test.heartbeat"),
    )


class _Service:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[dict[str, object]] = []
        self.called = threading.Event()

    def renew_lease(self, **kwargs: object) -> None:
        self.calls.append(kwargs)
        self.called.set()
        if self.fail:
            raise JobLeaseError("lost")


def test_controller_renews_until_stopped() -> None:
    service = _Service()
    context = _context()
    controller = LeaseHeartbeatController(
        service=service,  # type: ignore[arg-type]
        context=context,
        expected_job_version=2,
        interval_seconds=0.001,
        clock=lambda: FIXED,
        should_stop=lambda: False,
    )
    controller.start()
    assert service.called.wait(timeout=1)
    assert controller.stop(timeout_seconds=1)

    assert service.calls
    assert service.calls[0]["job_id"] == JOB_ID
    assert service.calls[0]["worker_id"] == WORKER_ID
    assert service.calls[0]["expected_job_version"] == 2
    assert not context.is_lease_lost()


def test_controller_marks_lease_lost_when_renewal_fails() -> None:
    service = _Service(fail=True)
    context = _context()
    controller = LeaseHeartbeatController(
        service=service,  # type: ignore[arg-type]
        context=context,
        expected_job_version=2,
        interval_seconds=0.001,
        clock=lambda: FIXED,
        should_stop=lambda: False,
    )
    controller.start()
    assert service.called.wait(timeout=1)
    assert controller.stop(timeout_seconds=1)
    assert context.is_lease_lost()


def test_controller_requests_context_stop_before_renewal() -> None:
    service = _Service()
    context = _context()
    checked = threading.Event()

    def should_stop() -> bool:
        checked.set()
        return True

    controller = LeaseHeartbeatController(
        service=service,  # type: ignore[arg-type]
        context=context,
        expected_job_version=2,
        interval_seconds=0.001,
        clock=lambda: FIXED,
        should_stop=should_stop,
    )
    controller.start()
    assert checked.wait(timeout=1)
    assert controller.stop(timeout_seconds=1)
    assert context.is_stop_requested()
    assert service.calls == []
