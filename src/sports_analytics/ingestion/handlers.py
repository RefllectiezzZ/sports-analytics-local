"""Job handler for football-data CSV ingestion."""

from __future__ import annotations

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
from sports_analytics.ingestion.service import FootballIngestionService
from sports_analytics.ingestion.types import INGEST_FOOTBALL_DATA_CSV_JOB_TYPE
from sports_analytics.jobs.context import JobExecutionContext
from sports_analytics.sources.football_data_co_uk.adapter import reject_arbitrary_source_controls


def _require_payload_object(payload: JsonValue) -> dict[str, JsonValue]:
    if not isinstance(payload, dict):
        msg = "payload must be a JSON object"
        raise PermanentJobError(msg)
    return payload


def _validate_ingest_payload(payload: dict[str, JsonValue]) -> tuple[str, str, str | None]:
    allowed = {"competition_id", "season", "raw_sha256"}
    unknown = sorted(set(payload) - allowed)
    if unknown:
        msg = f"unknown payload keys: {', '.join(unknown)}"
        raise PermanentJobError(msg)
    reject_arbitrary_source_controls({key: value for key, value in payload.items()})
    if "competition_id" not in payload or "season" not in payload:
        msg = "payload requires competition_id and season"
        raise PermanentJobError(msg)
    competition_id = payload["competition_id"]
    season = payload["season"]
    if not isinstance(competition_id, str) or not isinstance(season, str):
        msg = "competition_id and season must be strings"
        raise PermanentJobError(msg)
    raw_sha256 = payload.get("raw_sha256")
    if raw_sha256 is not None and not isinstance(raw_sha256, str):
        msg = "raw_sha256 must be a string or null"
        raise PermanentJobError(msg)
    return competition_id, season, raw_sha256


def ingest_football_data_csv_handler(
    context: JobExecutionContext,
    payload: JsonValue,
) -> JsonValue:
    """Execute ``ingest.football-data-csv`` using runtime paths bound by the worker."""
    if (
        context._database_path is None
        or context._raw_directory is None
        or context._snapshots_directory is None
        or context._scraping is None
    ):
        msg = "ingestion handler requires runtime context binding"
        raise PermanentJobError(msg)
    try:
        payload_object = _require_payload_object(payload)
        competition_id, season, raw_sha256 = _validate_ingest_payload(payload_object)
        service = FootballIngestionService(
            database_path=context._database_path,
            raw_directory=context._raw_directory,
            snapshots_directory=context._snapshots_directory,
            scraping=context._scraping,
            transport=context._http_transport,
            monotonic_clock=context._monotonic_clock,
            sleeper=context._sleeper,
            clock=context._clock,
        )
        result = service.ingest(
            competition_id=competition_id,
            season=season,
            raw_sha256=raw_sha256,
            actor="worker",
            correlation_id=context.job_id,
            checkpoint=context.checkpoint,
        )
        context.logger.info(
            "ingestion complete job_id=%s snapshot_id=%s competition_id=%s "
            "season_id=%s events=%s quotes=%s unresolved=%s reused=%s",
            context.job_id,
            result.snapshot_id,
            result.competition_id,
            result.season_id,
            result.events_count,
            result.market_quotes_count,
            result.unresolved_event_count,
            result.snapshot_reused,
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


JOB_TYPE = INGEST_FOOTBALL_DATA_CSV_JOB_TYPE
