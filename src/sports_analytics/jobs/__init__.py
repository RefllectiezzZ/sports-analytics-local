"""Public durable-job worker APIs."""

from sports_analytics.core.exceptions import (
    JobLeaseError,
    JobRegistryError,
    PermanentJobError,
    RetryableJobError,
    WorkerError,
    WorkerShutdownError,
)
from sports_analytics.jobs.context import JobExecutionContext
from sports_analytics.jobs.handlers import JobHandler, system_noop_handler
from sports_analytics.jobs.registry import HandlerRegistry, build_default_registry
from sports_analytics.jobs.runner import LeaseHeartbeatController, LocalWorker
from sports_analytics.jobs.service import JobQueueService, WorkerRegistrationService, WorkerService
from sports_analytics.jobs.types import (
    DEFAULT_WORKER_CAPABILITIES,
    DEFAULT_WORKER_NAME,
    SYSTEM_NOOP_JOB_TYPE,
    JobClaim,
    JobExecutionOutcome,
    JobExecutionState,
    JobFinalizationKind,
    LeaseRecoveryResult,
    QueueStatus,
    StaleWorkerReconciliationResult,
    WorkerRecord,
    WorkerRunResult,
    WorkerStatus,
)

__all__ = [
    "DEFAULT_WORKER_CAPABILITIES",
    "DEFAULT_WORKER_NAME",
    "SYSTEM_NOOP_JOB_TYPE",
    "HandlerRegistry",
    "JobClaim",
    "JobExecutionContext",
    "JobExecutionOutcome",
    "JobExecutionState",
    "JobFinalizationKind",
    "JobHandler",
    "JobLeaseError",
    "JobQueueService",
    "JobRegistryError",
    "LeaseHeartbeatController",
    "LeaseRecoveryResult",
    "LocalWorker",
    "PermanentJobError",
    "QueueStatus",
    "RetryableJobError",
    "StaleWorkerReconciliationResult",
    "WorkerError",
    "WorkerRecord",
    "WorkerRegistrationService",
    "WorkerRunResult",
    "WorkerService",
    "WorkerShutdownError",
    "WorkerStatus",
    "build_default_registry",
    "system_noop_handler",
]
