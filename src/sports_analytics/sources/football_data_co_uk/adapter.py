"""Football-Data.co.uk download/cache adapter."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sports_analytics.core.exceptions import PermanentSourceError
from sports_analytics.core.settings import ScrapingSettings
from sports_analytics.data.types import validate_sha256_checksum
from sports_analytics.sources.football_data_co_uk.catalog import build_csv_url, get_competition
from sports_analytics.sources.football_data_co_uk.parser import (
    ParsedFootballCsv,
    parse_football_data_csv,
)
from sports_analytics.sources.http import (
    Clock,
    HttpTransport,
    MonotonicClock,
    Sleeper,
    UrllibHttpTransport,
    download_bounded_bytes,
)
from sports_analytics.sources.raw_store import RawSourceArtifact, RawSourceStore
from sports_analytics.sources.types import SOURCE_FOOTBALL_DATA_CO_UK
from sports_analytics.sports.football.identifiers import parse_canonical_season


@dataclass(frozen=True, slots=True)
class FootballDataAcquisition:
    """Raw artifact plus parsed CSV for one competition/season request."""

    competition_id: str
    season_label: str
    start_year: int
    end_year: int
    source_season_code: str
    source_url: str
    artifact: RawSourceArtifact
    parsed: ParsedFootballCsv
    source_observed_at_utc: datetime


def acquire_football_data_csv(
    *,
    competition_id: str,
    season: str,
    raw_directory: Path,
    scraping: ScrapingSettings,
    source_observed_at_utc: datetime,
    raw_sha256: str | None = None,
    transport: HttpTransport | None = None,
    monotonic_clock: MonotonicClock | None = None,
    sleeper: Sleeper | None = None,
    clock: Clock | None = None,
) -> FootballDataAcquisition:
    """Resolve catalog entry, obtain raw bytes, store them, and parse CSV."""
    del clock  # observation time is injected explicitly by the caller
    competition = get_competition(competition_id)
    label, start_year, end_year, source_season_code = parse_canonical_season(season)
    source_url = build_csv_url(
        division_code=competition.division_code,
        source_season_code=source_season_code,
    )
    store = RawSourceStore(raw_directory)
    if raw_sha256 is not None:
        digest = validate_sha256_checksum(raw_sha256)
        artifact, content = store.load_verified(
            source_name=SOURCE_FOOTBALL_DATA_CO_UK,
            checksum_sha256=digest,
            source_url=source_url,
            retrieved_at=source_observed_at_utc,
        )
        http_meta = None
    else:
        active_transport = transport if transport is not None else UrllibHttpTransport()
        mono = monotonic_clock if monotonic_clock is not None else __import__("time").monotonic
        sleep = sleeper if sleeper is not None else __import__("time").sleep
        download, _ = download_bounded_bytes(
            url=source_url,
            transport=active_transport,
            timeout_seconds=scraping.request_timeout_seconds,
            maximum_bytes=scraping.maximum_download_bytes,
            maximum_retries=scraping.maximum_retries,
            retry_backoff_base_seconds=scraping.retry_backoff_base_seconds,
            retry_backoff_max_seconds=scraping.retry_backoff_max_seconds,
            minimum_request_interval_seconds=scraping.minimum_request_interval_seconds,
            monotonic_clock=mono,
            sleeper=sleep,
        )
        content = download.content
        artifact = store.store_bytes(
            source_name=SOURCE_FOOTBALL_DATA_CO_UK,
            source_url=source_url,
            content=content,
            retrieved_at=source_observed_at_utc,
            content_type=download.metadata.content_type,
            etag=download.metadata.etag,
            last_modified=download.metadata.last_modified,
            maximum_bytes=scraping.maximum_download_bytes,
        )
        http_meta = download.metadata
        del http_meta

    parsed = parse_football_data_csv(
        content,
        expected_division_code=competition.division_code,
    )
    if artifact.encoding is None:
        artifact = RawSourceArtifact(
            source_name=artifact.source_name,
            source_url=artifact.source_url,
            checksum_sha256=artifact.checksum_sha256,
            byte_count=artifact.byte_count,
            relative_path=artifact.relative_path,
            content_type=artifact.content_type,
            retrieved_at=artifact.retrieved_at,
            etag=artifact.etag,
            last_modified=artifact.last_modified,
            encoding=parsed.encoding,
        )
    return FootballDataAcquisition(
        competition_id=competition.competition_id,
        season_label=label,
        start_year=start_year,
        end_year=end_year,
        source_season_code=source_season_code,
        source_url=source_url,
        artifact=artifact,
        parsed=parsed,
        source_observed_at_utc=source_observed_at_utc,
    )


def reject_arbitrary_source_controls(payload: dict[str, object]) -> None:
    """Reject payload keys that attempt to bypass the static catalog/URL policy."""
    forbidden = {
        "url",
        "source_url",
        "division_code",
        "host",
        "path",
        "file_path",
        "local_path",
        "import_path",
    }
    present = sorted(set(payload) & forbidden)
    if present:
        msg = f"payload contains forbidden source-control keys: {', '.join(present)}"
        raise PermanentSourceError(msg)
