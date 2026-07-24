"""Job worker types, outcomes, and queue status records."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final

from sports_analytics.data.types import JobRecord, JsonValue

MAX_JOB_ERROR_LENGTH: Final[int] = 2_048
SYSTEM_NOOP_JOB_TYPE: Final[str] = "system.noop"
DEFAULT_WORKER_NAME: Final[str] = "local-worker"
DEFAULT_WORKER_CAPABILITIES: Final[tuple[str, ...]] = (SYSTEM_NOOP_JOB_TYPE,)


class WorkerStatus(StrEnum):
    """Allowed durable worker-instance statuses."""

    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


class JobExecutionState(StrEnum):
    """High-level outcome of executing one claimed job."""

    SUCCEEDED = "succeeded"
    RETRY_SCHEDULED = "retry_scheduled"
    FAILED = "failed"
    LEASE_LOST = "lease_lost"
    SHUTDOWN_INTERRUPTED = "shutdown_interrupted"


class JobFinalizationKind(StrEnum):
    """Finalization result returned by fail/complete queue operations."""

    SUCCEEDED = "succeeded"
    RETRY_SCHEDULED = "retry_scheduled"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class WorkerRecord:
    """Immutable worker-instance row representation."""

    id: str
    name: str
    status: WorkerStatus
    process_id: int
    hostname: str
    started_at: datetime
    heartbeat_at: datetime
    stopping_at: datetime | None
    stopped_at: datetime | None
    current_job_id: str | None
    last_error: str | None
    capabilities: JsonValue
    version: int


@dataclass(frozen=True, slots=True)
class JobClaim:
    """Typed result of atomically claiming one pending job."""

    job: JobRecord
    worker_id: str
    claimed_at: datetime
    lease_expires_at: datetime
    attempt: int


@dataclass(frozen=True, slots=True)
class JobExecutionOutcome:
    """Typed result of completing or failing a claimed job."""

    kind: JobFinalizationKind
    job: JobRecord


@dataclass(frozen=True, slots=True)
class QueueStatus:
    """Read-only queue and worker summary at an explicit timestamp."""

    pending_count: int
    available_pending_count: int
    delayed_pending_count: int
    running_count: int
    succeeded_count: int
    failed_count: int
    cancelled_count: int
    expired_running_lease_count: int
    active_worker_count: int
    stale_worker_count: int
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class LeaseRecoveryResult:
    """Typed result of one expired-lease recovery pass."""

    scanned_count: int
    requeued_count: int
    failed_count: int
    requeued_job_ids: tuple[str, ...]
    failed_job_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StaleWorkerReconciliationResult:
    """Typed result of marking heartbeat-expired workers failed."""

    scanned_count: int
    failed_count: int
    failed_worker_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class WorkerRunResult:
    """Summary returned when a sequential worker loop exits."""

    worker_id: str
    jobs_processed: int
    stop_reason: str
    status: WorkerStatus
