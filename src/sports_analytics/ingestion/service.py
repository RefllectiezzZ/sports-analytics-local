"""Football ingestion service composing source, normalize, and snapshot stages."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from sports_analytics.core.settings import ScrapingSettings
from sports_analytics.ingestion.types import FootballIngestionResult, published_to_result
from sports_analytics.snapshots.service import SnapshotPublicationService
from sports_analytics.snapshots.writer import prepare_snapshot_directory
from sports_analytics.sources.football_data_co_uk.adapter import acquire_football_data_csv
from sports_analytics.sources.football_data_co_uk.catalog import get_competition
from sports_analytics.sources.http import HttpTransport, MonotonicClock, Sleeper
from sports_analytics.sports.football.normalization import normalize_football_rows


class FootballIngestionService:
    """Orchestrate one football-data-co-uk CSV ingestion without holding SQLite open."""

    def __init__(
        self,
        *,
        database_path: Path,
        raw_directory: Path,
        snapshots_directory: Path,
        scraping: ScrapingSettings,
        transport: HttpTransport | None = None,
        monotonic_clock: MonotonicClock | None = None,
        sleeper: Sleeper | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._database_path = Path(database_path)
        self._raw_directory = Path(raw_directory)
        self._snapshots_directory = Path(snapshots_directory)
        self._scraping = scraping
        self._transport = transport
        self._monotonic_clock = monotonic_clock
        self._sleeper = sleeper
        self._clock = clock if clock is not None else (lambda: datetime.now(tz=UTC))

    def ingest(
        self,
        *,
        competition_id: str,
        season: str,
        raw_sha256: str | None = None,
        actor: str = "ingestion-service",
        correlation_id: str | None = None,
        checkpoint: Callable[[], None] | None = None,
    ) -> FootballIngestionResult:
        """Run acquisition, normalization, preparation, and publication."""

        def _checkpoint() -> None:
            if checkpoint is not None:
                checkpoint()

        observed_at = self._clock()
        if observed_at.tzinfo is None:
            observed_at = observed_at.replace(tzinfo=UTC)
        else:
            observed_at = observed_at.astimezone(UTC)

        _checkpoint()
        acquisition = acquire_football_data_csv(
            competition_id=competition_id,
            season=season,
            raw_directory=self._raw_directory,
            scraping=self._scraping,
            source_observed_at_utc=observed_at,
            raw_sha256=raw_sha256,
            transport=self._transport,
            monotonic_clock=self._monotonic_clock,
            sleeper=self._sleeper,
        )
        _checkpoint()
        competition = get_competition(acquisition.competition_id)
        # Parsing already completed inside acquire; checkpoint marks that stage.
        _checkpoint()
        bundle = normalize_football_rows(
            rows=list(acquisition.parsed.rows),
            competition_id=competition.competition_id,
            competition_display_name=competition.display_name,
            country_code=competition.country_code,
            source_competition_code=competition.division_code,
            timezone_name=competition.timezone,
            season_label=acquisition.season_label,
            start_year=acquisition.start_year,
            end_year=acquisition.end_year,
            source_season_code=acquisition.source_season_code,
            source_name=acquisition.artifact.source_name,
            source_file_sha256=acquisition.artifact.checksum_sha256,
            source_observed_at_utc=acquisition.source_observed_at_utc,
        )
        _checkpoint()
        prepared = prepare_snapshot_directory(
            snapshots_directory=self._snapshots_directory,
            bundle=bundle,
            artifact=acquisition.artifact,
            competition_id=competition.competition_id,
            season_label=acquisition.season_label,
            source_competition_code=competition.division_code,
            source_season_code=acquisition.source_season_code,
            source_url=acquisition.source_url,
            source_observed_at_utc=acquisition.source_observed_at_utc,
            unknown_source_columns=acquisition.parsed.unknown_headers,
            missing_optional_source_columns=acquisition.parsed.missing_optional_headers,
            http_content_type=acquisition.artifact.content_type,
            http_etag=acquisition.artifact.etag,
            http_last_modified=acquisition.artifact.last_modified,
        )
        _checkpoint()
        publisher = SnapshotPublicationService(
            database_path=self._database_path,
            snapshots_directory=self._snapshots_directory,
        )
        published = publisher.publish_or_reuse(
            prepared,
            actor=actor,
            correlation_id=correlation_id,
        )
        _checkpoint()
        return published_to_result(published)
