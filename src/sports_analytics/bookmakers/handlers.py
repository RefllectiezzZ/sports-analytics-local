"""Job handlers for bookmaker acquisition."""

from __future__ import annotations

from pathlib import Path

from sports_analytics.bookmakers.orchestration import BookmakerAcquisitionOrchestrator
from sports_analytics.bookmakers.service import (
    BookmakerIngestionService,
    validate_bookmaker_ingest_payload,
)
from sports_analytics.bookmakers.types import (
    INGEST_BOOKMAKER_AUTONOMOUS_CYCLE_JOB_TYPE,
    INGEST_BOOKMAKER_CURRENT_ODDS_JOB_TYPE,
)
from sports_analytics.core.exceptions import (
    NormalizationError,
    ParserError,
    PermanentJobError,
    PermanentSourceError,
    RetryableJobError,
    RetryableSourceError,
    SnapshotBusyError,
    SnapshotIntegrityError,
    SourceNotFoundError,
)
from sports_analytics.core.settings import BookmakersSettings
from sports_analytics.data.codec import parse_utc_timestamp
from sports_analytics.data.types import JsonValue, validate_identifier
from sports_analytics.jobs.context import JobExecutionContext
from sports_analytics.sources.bookmaker_catalog import (
    SUPPORTED_BOOKMAKER_SPORTS,
    reject_forbidden_job_controls,
)


def _require_payload_object(payload: JsonValue) -> dict[str, JsonValue]:
    if not isinstance(payload, dict):
        msg = "payload must be a JSON object"
        raise PermanentJobError(msg)
    return payload


def _require_runtime(context: JobExecutionContext) -> tuple[Path, Path, Path, BookmakersSettings]:
    if (
        context._database_path is None
        or context._raw_directory is None
        or context._snapshots_directory is None
        or context._bookmakers is None
    ):
        msg = "bookmaker ingestion handler requires runtime context binding"
        raise PermanentJobError(msg)
    return (
        context._database_path,
        context._raw_directory,
        context._snapshots_directory,
        context._bookmakers,
    )


def ingest_bookmaker_current_odds_handler(
    context: JobExecutionContext,
    payload: JsonValue,
) -> JsonValue:
    """Execute ``ingest.bookmaker-current-odds`` using runtime paths bound by the worker."""
    database_path, raw_directory, snapshots_directory, bookmakers = _require_runtime(context)
    try:
        payload_object = _require_payload_object(payload)
        provider_id, sport, observed_at, cycle_id = validate_bookmaker_ingest_payload(
            {key: value for key, value in payload_object.items()}
        )
        service = BookmakerIngestionService(
            database_path=database_path,
            raw_directory=raw_directory,
            snapshots_directory=snapshots_directory,
            bookmakers=bookmakers,
            clock=context._clock,
            session=context._browser_session,
        )
        result = service.ingest(
            provider_id=provider_id,
            sport=sport,
            observed_at_utc=observed_at,
            acquisition_cycle_id=cycle_id,
            actor="worker",
            correlation_id=context.job_id,
            checkpoint=context.checkpoint,
            attempt_number=context.attempt,
            maximum_attempts=context.maximum_attempts,
        )
        context.logger.info(
            "bookmaker acquisition complete job_id=%s provider=%s sport=%s "
            "status=%s snapshot_id=%s block_reason=%s events=%s quotes=%s",
            context.job_id,
            result.provider_id,
            result.sport,
            result.status,
            result.snapshot_id,
            result.block_reason,
            result.events_observed,
            result.valid_quotes_observed,
        )
        return result.to_json()
    except SnapshotBusyError as exc:
        raise RetryableJobError(str(exc)) from exc
    except RetryableSourceError as exc:
        raise RetryableJobError(str(exc)) from exc
    except (
        PermanentSourceError,
        SourceNotFoundError,
        ParserError,
        NormalizationError,
        SnapshotIntegrityError,
    ) as exc:
        raise PermanentJobError(str(exc)) from exc
    except PermanentJobError:
        raise
    except RetryableJobError:
        raise
    except OSError as exc:
        errno_name = getattr(exc, "errno", None)
        if errno_name in {28, 112}:
            raise PermanentJobError(f"filesystem capacity error: {exc}") from exc
        if errno_name in {13, 1}:
            raise PermanentJobError(f"filesystem permission error: {exc}") from exc
        raise PermanentJobError(f"filesystem error: {exc}") from exc


def validate_autonomous_cycle_payload(
    payload: dict[str, object],
) -> tuple[str, str | None, str | None]:
    """Validate an autonomous sport acquisition job payload."""
    allowed = {"sport", "observed_at_utc", "acquisition_cycle_id"}
    unknown = sorted(set(payload) - allowed)
    if unknown:
        msg = f"unknown payload keys: {', '.join(unknown)}"
        raise PermanentJobError(msg)
    reject_forbidden_job_controls(payload)
    if "sport" not in payload:
        msg = "payload requires sport"
        raise PermanentJobError(msg)
    sport = payload["sport"]
    if not isinstance(sport, str):
        msg = "sport must be a string"
        raise PermanentJobError(msg)
    sport_code = validate_identifier(sport, field_name="sport")
    if sport_code not in SUPPORTED_BOOKMAKER_SPORTS:
        msg = f"unsupported bookmaker sport: {sport_code}"
        raise PermanentJobError(msg)
    observed_raw = payload.get("observed_at_utc")
    observed_at: str | None = None
    if observed_raw is not None:
        if not isinstance(observed_raw, str):
            msg = "observed_at_utc must be a string or null"
            raise PermanentJobError(msg)
        parse_utc_timestamp(observed_raw)
        observed_at = observed_raw
    cycle_raw = payload.get("acquisition_cycle_id")
    cycle_id: str | None = None
    if cycle_raw is not None:
        if not isinstance(cycle_raw, str):
            msg = "acquisition_cycle_id must be a string or null"
            raise PermanentJobError(msg)
        cycle_id = validate_identifier(cycle_raw, field_name="acquisition_cycle_id")
    return sport_code, observed_at, cycle_id


def ingest_bookmaker_autonomous_cycle_handler(
    context: JobExecutionContext,
    payload: JsonValue,
) -> JsonValue:
    """Execute one autonomous Betano-first / Betclic-fallback sport cycle."""
    database_path, raw_directory, snapshots_directory, bookmakers = _require_runtime(context)
    payload_object = _require_payload_object(payload)
    sport, _observed_raw, cycle_id = validate_autonomous_cycle_payload(
        {key: value for key, value in payload_object.items()}
    )
    service = BookmakerIngestionService(
        database_path=database_path,
        raw_directory=raw_directory,
        snapshots_directory=snapshots_directory,
        bookmakers=bookmakers,
        clock=context._clock,
        session=context._browser_session,
    )
    orchestrator = BookmakerAcquisitionOrchestrator(
        service=service,
        bookmakers=bookmakers,
        database_path=database_path,
        raw_directory=raw_directory,
        snapshots_directory=snapshots_directory,
        clock=context._clock,
    )
    try:
        result = orchestrator.run_autonomous_cycle(
            sport=sport,
            acquisition_cycle_id=cycle_id,
            actor="worker",
            attempt_number=context.attempt,
            maximum_attempts=context.maximum_attempts,
        )
    except RetryableJobError:
        raise
    selected = result.selected_result
    context.logger.info(
        "bookmaker autonomous cycle complete job_id=%s sport=%s selected=%s reason=%s",
        context.job_id,
        sport,
        result.fallback_decision.selected_provider,
        result.fallback_decision.reason_code.value,
    )
    return {
        "sport": sport,
        "acquisition_cycle_id": result.acquisition_cycle_id,
        "selected_provider": result.fallback_decision.selected_provider,
        "cached_used": result.fallback_decision.cached_used,
        "reason_code": result.fallback_decision.reason_code.value,
        "betano_status": None if result.betano_result is None else result.betano_result.status,
        "betclic_status": None if result.betclic_result is None else result.betclic_result.status,
        "selected_snapshot_id": None if selected is None else selected.snapshot_id,
    }


CURRENT_ODDS_JOB_TYPE = INGEST_BOOKMAKER_CURRENT_ODDS_JOB_TYPE
AUTONOMOUS_CYCLE_JOB_TYPE = INGEST_BOOKMAKER_AUTONOMOUS_CYCLE_JOB_TYPE
