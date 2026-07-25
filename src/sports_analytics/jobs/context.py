"""Execution context passed to job handlers."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime
from logging import Logger
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sports_analytics.core.exceptions import JobLeaseError, WorkerShutdownError

if TYPE_CHECKING:
    from sports_analytics.core.runtime import RuntimeContext
    from sports_analytics.core.settings import ScrapingSettings
    from sports_analytics.sources.http import HttpTransport, MonotonicClock, Sleeper


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
    _database_path: Path | None = field(default=None, init=False, repr=False, compare=False)
    _raw_directory: Path | None = field(default=None, init=False, repr=False, compare=False)
    _snapshots_directory: Path | None = field(default=None, init=False, repr=False, compare=False)
    _scraping: ScrapingSettings | None = field(default=None, init=False, repr=False, compare=False)
    _http_transport: HttpTransport | None = field(default=None, init=False, repr=False, compare=False)
    _monotonic_clock: MonotonicClock | None = field(
        default=None, init=False, repr=False, compare=False
    )
    _sleeper: Sleeper | None = field(default=None, init=False, repr=False, compare=False)
    _clock: Any = field(default=None, init=False, repr=False, compare=False)

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

    def bind_runtime(self, runtime: RuntimeContext) -> None:
        """Attach non-connection runtime paths and settings for handler use."""
        object.__setattr__(self, "_database_path", runtime.database_path)
        object.__setattr__(self, "_raw_directory", runtime.paths.raw_directory)
        object.__setattr__(self, "_snapshots_directory", runtime.paths.snapshots_directory)
        object.__setattr__(self, "_scraping", runtime.settings.scraping)

    def bind_test_dependencies(
        self,
        *,
        transport: HttpTransport | None = None,
        monotonic_clock: MonotonicClock | None = None,
        sleeper: Sleeper | None = None,
        clock: Any = None,
    ) -> None:
        """Attach injectable transport/clock dependencies for tests."""
        object.__setattr__(self, "_http_transport", transport)
        object.__setattr__(self, "_monotonic_clock", monotonic_clock)
        object.__setattr__(self, "_sleeper", sleeper)
        object.__setattr__(self, "_clock", clock)
