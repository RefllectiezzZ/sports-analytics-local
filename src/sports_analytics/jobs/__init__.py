"""Durable local job-worker package.

Public symbols are imported lazily from submodules to avoid circular imports
with data repositories.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sports_analytics.jobs.context import JobExecutionContext
    from sports_analytics.jobs.registry import HandlerRegistry
    from sports_analytics.jobs.runner import LeaseHeartbeatController, LocalWorker
    from sports_analytics.jobs.service import WorkerService
    from sports_analytics.jobs.types import (
        JobClaim,
        JobExecutionOutcome,
        JobExecutionState,
        LeaseRecoveryResult,
        QueueStatus,
        WorkerRecord,
        WorkerRunResult,
        WorkerStatus,
    )

__all__ = [
    "HandlerRegistry",
    "JobClaim",
    "JobExecutionContext",
    "JobExecutionOutcome",
    "JobExecutionState",
    "LeaseHeartbeatController",
    "LeaseRecoveryResult",
    "LocalWorker",
    "QueueStatus",
    "WorkerRecord",
    "WorkerRunResult",
    "WorkerService",
    "WorkerStatus",
]


def __getattr__(name: str) -> object:
    if name in {
        "JobClaim",
        "JobExecutionOutcome",
        "JobExecutionState",
        "LeaseRecoveryResult",
        "QueueStatus",
        "WorkerRecord",
        "WorkerRunResult",
        "WorkerStatus",
    }:
        from sports_analytics.jobs import types as types_module

        return getattr(types_module, name)
    if name == "JobExecutionContext":
        from sports_analytics.jobs.context import JobExecutionContext

        return JobExecutionContext
    if name == "HandlerRegistry":
        from sports_analytics.jobs.registry import HandlerRegistry

        return HandlerRegistry
    if name in {"LeaseHeartbeatController", "LocalWorker"}:
        from sports_analytics.jobs import runner as runner_module

        return getattr(runner_module, name)
    if name == "WorkerService":
        from sports_analytics.jobs.service import WorkerService

        return WorkerService
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)
