"""In-process job handler registry."""

from __future__ import annotations

from sports_analytics.core.exceptions import JobRegistryError, RepositoryError
from sports_analytics.data.types import validate_identifier
from sports_analytics.jobs.handlers import JobHandler, system_noop_handler
from sports_analytics.jobs.types import SYSTEM_NOOP_JOB_TYPE


class HandlerRegistry:
    """Mutable-until-frozen mapping from durable job type to handler."""

    def __init__(self) -> None:
        self._handlers: dict[str, JobHandler] = {}
        self._frozen = False

    def register(self, job_type: str, handler: JobHandler) -> None:
        """Register ``handler`` for ``job_type``."""
        if self._frozen:
            msg = "handler registry is frozen"
            raise JobRegistryError(msg)
        normalized_type = self._validate_job_type(job_type)
        if normalized_type in self._handlers:
            msg = f"handler already registered for job type {normalized_type!r}"
            raise JobRegistryError(msg)
        self._handlers[normalized_type] = handler

    def get(self, job_type: str) -> JobHandler:
        """Return the handler registered for ``job_type``."""
        normalized_type = self._validate_job_type(job_type)
        try:
            return self._handlers[normalized_type]
        except KeyError as exc:
            msg = f"no handler registered for job type {normalized_type!r}"
            raise JobRegistryError(msg) from exc

    def list_job_types(self) -> tuple[str, ...]:
        """Return registered job types in deterministic order."""
        return tuple(sorted(self._handlers))

    def freeze(self) -> None:
        """Prevent further registry mutation."""
        self._frozen = True

    @staticmethod
    def _validate_job_type(job_type: str) -> str:
        try:
            return validate_identifier(job_type, field_name="job_type")
        except RepositoryError as exc:
            msg = f"invalid job type: {exc}"
            raise JobRegistryError(msg) from exc


def build_default_registry() -> HandlerRegistry:
    """Return a frozen registry with built-in system handlers."""
    registry = HandlerRegistry()
    registry.register(SYSTEM_NOOP_JOB_TYPE, system_noop_handler)
    registry.freeze()
    return registry
