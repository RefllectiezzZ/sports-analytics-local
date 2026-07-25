"""Typed job handler protocol and built-in handlers."""

from __future__ import annotations

from typing import Protocol

from sports_analytics.data.types import JsonValue
from sports_analytics.jobs.context import JobExecutionContext
from sports_analytics.jobs.types import SYSTEM_NOOP_JOB_TYPE


class JobHandler(Protocol):
    """Callable job handler contract used by the local worker."""

    def __call__(self, context: JobExecutionContext, payload: JsonValue) -> JsonValue:
        """Execute one claimed job and return JSON-serializable result data."""


def system_noop_handler(context: JobExecutionContext, payload: JsonValue) -> JsonValue:
    """Return a deterministic success payload without side effects."""
    del context, payload
    return {"ok": True, "handler": SYSTEM_NOOP_JOB_TYPE}
