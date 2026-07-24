"""Execution context passed to job handlers."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime
from logging import Logger

from sports_analytics.core.exceptions import JobLeaseError, WorkerShutdownError


@dataclass(frozen=True, slots=True)
class JobExecutionContext:
    """Immutable public job execution metadata plus cooperative stop signals."""

    job_id: str
    worker_id: str
    attempt: int
    maximum_attempts: int
    claimed_at: datetime
    lease_expires_at: datetime
    logger: Logger
    _stop_requested: threading.Event = field(
        default_factory=threading.Event,
        init=False,
        repr=False,
        compare=False,
    )
    _lease_lost: threading.Event = field(
        default_factory=threading.Event,
        init=False,
        repr=False,
        compare=False,
    )

    def checkpoint(self) -> None:
        """Raise if the worker should stop or the job lease has been lost."""
        if self._stop_requested.is_set():
            msg = "worker shutdown requested"
            raise WorkerShutdownError(msg)
        if self._lease_lost.is_set():
            msg = "job lease lost"
            raise JobLeaseError(msg)

    def request_stop(self) -> None:
        """Request cooperative handler shutdown."""
        self._stop_requested.set()

    def report_lease_lost(self) -> None:
        """Record that this job lease can no longer be safely finalized."""
        self._lease_lost.set()

    def is_stop_requested(self) -> bool:
        """Return whether cooperative shutdown has been requested."""
        return self._stop_requested.is_set()

    def is_lease_lost(self) -> bool:
        """Return whether the heartbeat controller observed lease loss."""
        return self._lease_lost.is_set()
