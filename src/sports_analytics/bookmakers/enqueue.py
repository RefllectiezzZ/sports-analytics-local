"""Enqueue helpers for bookmaker acquisition jobs."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from sports_analytics.bookmakers.types import (
    DEFAULT_BOOKMAKER_INGESTION_MAXIMUM_ATTEMPTS,
    INGEST_BOOKMAKER_CURRENT_ODDS_JOB_TYPE,
    PROVIDER_BETANO_PT,
    PROVIDER_BETCLIC_PT,
)
from sports_analytics.core.exceptions import ConfigurationError, PermanentSourceError
from sports_analytics.core.settings import BookmakersSettings
from sports_analytics.data.codec import format_utc_timestamp
from sports_analytics.data.database import connect_database, transaction
from sports_analytics.data.repositories.jobs import JobRepository
from sports_analytics.data.types import (
    DEFAULT_JOB_PRIORITY,
    JobRecord,
    JsonValue,
    validate_identifier,
    validate_strict_int,
)
from sports_analytics.sources.bookmaker_catalog import SUPPORTED_BOOKMAKER_SPORTS


def enqueue_bookmaker_acquisition(
    *,
    database_path: Path,
    bookmakers: BookmakersSettings,
    provider_id: str,
    sport: str,
    observed_at_utc: datetime | None = None,
    acquisition_cycle_id: str | None = None,
    priority: int = DEFAULT_JOB_PRIORITY,
    maximum_attempts: int = DEFAULT_BOOKMAKER_INGESTION_MAXIMUM_ATTEMPTS,
    actor: str = "bookmaker-cli",
    created_at: datetime | None = None,
    idempotency_key: str | None = None,
) -> JobRecord:
    """Create one pending ``ingest.bookmaker-current-odds`` job."""
    if not bookmakers.enabled:
        msg = "bookmakers.enabled must be true to enqueue bookmaker acquisition"
        raise ConfigurationError(msg)
    try:
        provider = validate_identifier(provider_id, field_name="provider_id")
        sport_code = validate_identifier(sport, field_name="sport")
    except Exception as exc:  # noqa: BLE001
        raise ConfigurationError(str(exc)) from exc
    if provider not in {PROVIDER_BETANO_PT, PROVIDER_BETCLIC_PT}:
        msg = f"unsupported bookmaker provider: {provider}"
        raise PermanentSourceError(msg)
    if sport_code not in SUPPORTED_BOOKMAKER_SPORTS:
        msg = f"unsupported bookmaker sport: {sport_code}"
        raise PermanentSourceError(msg)
    provider_settings = bookmakers.betano if provider == PROVIDER_BETANO_PT else bookmakers.betclic
    if not provider_settings.enabled:
        msg = f"bookmaker provider {provider} is disabled"
        raise ConfigurationError(msg)
    try:
        priority = validate_strict_int(priority, field_name="priority")
        maximum_attempts = validate_strict_int(
            maximum_attempts,
            field_name="maximum_attempts",
            minimum=1,
        )
    except Exception as exc:  # noqa: BLE001
        raise ConfigurationError(str(exc)) from exc

    payload: dict[str, JsonValue] = {
        "provider_id": provider,
        "sport": sport_code,
        "observed_at_utc": (
            None if observed_at_utc is None else format_utc_timestamp(observed_at_utc)
        ),
        "acquisition_cycle_id": acquisition_cycle_id,
    }
    with connect_database(database_path) as connection:
        with transaction(connection, immediate=True):
            jobs = JobRepository(connection)
            return jobs.create_job(
                job_type=INGEST_BOOKMAKER_CURRENT_ODDS_JOB_TYPE,
                payload=payload,
                maximum_attempts=maximum_attempts,
                actor=actor,
                priority=priority,
                created_at=created_at,
                idempotency_key=idempotency_key,
            )
