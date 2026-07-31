"""In-process job handler registry."""

from __future__ import annotations

from sports_analytics.bookmakers.handlers import (
    ingest_bookmaker_autonomous_cycle_handler,
    ingest_bookmaker_current_odds_handler,
)
from sports_analytics.bookmakers.types import (
    INGEST_BOOKMAKER_AUTONOMOUS_CYCLE_JOB_TYPE,
    INGEST_BOOKMAKER_CURRENT_ODDS_JOB_TYPE,
)
from sports_analytics.core.exceptions import JobRegistryError, RepositoryError
from sports_analytics.data.types import validate_identifier
from sports_analytics.ingestion.handlers import ingest_football_data_csv_handler
from sports_analytics.ingestion.types import INGEST_FOOTBALL_DATA_CSV_JOB_TYPE
from sports_analytics.jobs.handlers import JobHandler, system_noop_handler
from sports_analytics.jobs.types import SYSTEM_NOOP_JOB_TYPE
from sports_analytics.learning.jobs import (
    EVALUATE_RETRAINING_TRIGGER_JOB_TYPE,
    REFRESH_MONITORING_JOB_TYPE,
    REGISTER_RESULTS_JOB_TYPE,
    RUN_CHALLENGER_CYCLE_JOB_TYPE,
    SETTLE_NEW_RESULTS_JOB_TYPE,
    evaluate_retraining_trigger_handler,
    refresh_monitoring_handler,
    register_results_handler,
    run_challenger_cycle_handler,
    settle_new_results_handler,
)
from sports_analytics.mvp.automatic_market_data import (
    AUTOMATIC_MARKET_DATA_JOB_TYPE,
    automatic_market_data_handler,
)
from sports_analytics.operations.handlers import (
    RUN_MONITORING_JOB_TYPE,
    SETTLE_ANALYSIS_JOB_TYPE,
    run_monitoring_handler,
    settle_analysis_handler,
)
from sports_analytics.services.football_product_jobs import (
    RUN_FOOTBALL_PRODUCT_JOB_TYPE,
    run_football_product_handler,
)


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

    @property
    def frozen(self) -> bool:
        """Return whether the registry rejects further registration."""
        return self._frozen

    @staticmethod
    def _validate_job_type(job_type: str) -> str:
        try:
            return validate_identifier(job_type, field_name="job_type")
        except RepositoryError as exc:
            msg = f"invalid job type: {exc}"
            raise JobRegistryError(msg) from exc


def build_default_registry() -> HandlerRegistry:
    """Return a frozen registry with built-in system and ingestion handlers."""
    registry = HandlerRegistry()
    registry.register(SYSTEM_NOOP_JOB_TYPE, system_noop_handler)
    registry.register(INGEST_FOOTBALL_DATA_CSV_JOB_TYPE, ingest_football_data_csv_handler)
    registry.register(
        INGEST_BOOKMAKER_CURRENT_ODDS_JOB_TYPE,
        ingest_bookmaker_current_odds_handler,
    )
    registry.register(
        INGEST_BOOKMAKER_AUTONOMOUS_CYCLE_JOB_TYPE,
        ingest_bookmaker_autonomous_cycle_handler,
    )
    registry.register(SETTLE_ANALYSIS_JOB_TYPE, settle_analysis_handler)
    registry.register(RUN_MONITORING_JOB_TYPE, run_monitoring_handler)
    registry.register(RUN_FOOTBALL_PRODUCT_JOB_TYPE, run_football_product_handler)
    registry.register(AUTOMATIC_MARKET_DATA_JOB_TYPE, automatic_market_data_handler)
    registry.register(REGISTER_RESULTS_JOB_TYPE, register_results_handler)
    registry.register(SETTLE_NEW_RESULTS_JOB_TYPE, settle_new_results_handler)
    registry.register(REFRESH_MONITORING_JOB_TYPE, refresh_monitoring_handler)
    registry.register(
        EVALUATE_RETRAINING_TRIGGER_JOB_TYPE,
        evaluate_retraining_trigger_handler,
    )
    registry.register(RUN_CHALLENGER_CYCLE_JOB_TYPE, run_challenger_cycle_handler)
    registry.freeze()
    return registry
