"""Scraper CLI enqueue helpers for football ingestion jobs."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from sports_analytics.core.exceptions import ConfigurationError, PermanentSourceError
from sports_analytics.core.settings import ScrapingSettings
from sports_analytics.data.database import connect_database, transaction
from sports_analytics.data.repositories.jobs import JobRepository
from sports_analytics.data.types import DEFAULT_JOB_PRIORITY, JobRecord, validate_sha256_checksum, validate_strict_int
from sports_analytics.ingestion.types import (
    DEFAULT_INGESTION_MAXIMUM_ATTEMPTS,
    INGEST_FOOTBALL_DATA_CSV_JOB_TYPE,
)
from sports_analytics.sources.football_data_co_uk.catalog import get_competition
from sports_analytics.sports.football.identifiers import parse_canonical_season


def enqueue_football_data_ingestion(
    *,
    database_path: Path,
    scraping: ScrapingSettings,
    competition_id: str,
    season: str,
    raw_sha256: str | None = None,
    priority: int = DEFAULT_JOB_PRIORITY,
    maximum_attempts: int = DEFAULT_INGESTION_MAXIMUM_ATTEMPTS,
    actor: str = "scraper-cli",
    created_at: datetime | None = None,
) -> JobRecord:
    """Create one pending ``ingest.football-data-csv`` job in a short transaction."""
    if not scraping.enabled:
        msg = "scraping.enabled must be true to enqueue football-data ingestion"
        raise ConfigurationError(msg)
    competition = get_competition(competition_id)
    label, _start, _end, _code = parse_canonical_season(season)
    del label
    if raw_sha256 is not None:
        try:
            raw_sha256 = validate_sha256_checksum(raw_sha256)
        except Exception as exc:  # noqa: BLE001
            raise PermanentSourceError(str(exc)) from exc
    try:
        priority = validate_strict_int(priority, field_name="priority")
        maximum_attempts = validate_strict_int(
            maximum_attempts,
            field_name="maximum_attempts",
            minimum=1,
        )
    except Exception as exc:  # noqa: BLE001
        raise ConfigurationError(str(exc)) from exc

    payload = {
        "competition_id": competition.competition_id,
        "season": season,
        "raw_sha256": raw_sha256,
    }
    with connect_database(database_path) as connection:
        with transaction(connection, immediate=True):
            jobs = JobRepository(connection)
            return jobs.create_job(
                job_type=INGEST_FOOTBALL_DATA_CSV_JOB_TYPE,
                payload=payload,
                maximum_attempts=maximum_attempts,
                actor=actor,
                priority=priority,
                created_at=created_at,
            )
