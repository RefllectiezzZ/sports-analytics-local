"""Job handler for bookmaker current-odds acquisition."""

from __future__ import annotations

from sports_analytics.bookmakers.service import (
    BookmakerIngestionService,
    validate_bookmaker_ingest_payload,
)
from sports_analytics.bookmakers.types import INGEST_BOOKMAKER_CURRENT_ODDS_JOB_TYPE
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
from sports_analytics.data.types import JsonValue
from sports_analytics.jobs.context import JobExecutionContext


def _require_payload_object(payload: JsonValue) -> dict[str, JsonValue]:
    if not isinstance(payload, dict):
        msg = "payload must be a JSON object"
        raise PermanentJobError(msg)
    return payload


def ingest_bookmaker_current_odds_handler(
    context: JobExecutionContext,
    payload: JsonValue,
) -> JsonValue:
    """Execute ``ingest.bookmaker-current-odds`` using runtime paths bound by the worker."""
    if (
        context._database_path is None
        or context._raw_directory is None
        or context._snapshots_directory is None
        or context._bookmakers is None
    ):
        msg = "bookmaker ingestion handler requires runtime context binding"
        raise PermanentJobError(msg)
    try:
        payload_object = _require_payload_object(payload)
        provider_id, sport, observed_at, cycle_id = validate_bookmaker_ingest_payload(
            {key: value for key, value in payload_object.items()}
        )
        service = BookmakerIngestionService(
            database_path=context._database_path,
            raw_directory=context._raw_directory,
            snapshots_directory=context._snapshots_directory,
            bookmakers=context._bookmakers,
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


JOB_TYPE = INGEST_BOOKMAKER_CURRENT_ODDS_JOB_TYPE
