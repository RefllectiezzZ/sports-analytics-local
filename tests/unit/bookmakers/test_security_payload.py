"""Security and payload validation for bookmaker acquisition jobs."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from sports_analytics.bookmakers.enqueue import enqueue_bookmaker_acquisition
from sports_analytics.bookmakers.service import (
    BookmakerIngestionService,
    validate_bookmaker_ingest_payload,
)
from sports_analytics.core.exceptions import PermanentJobError, PermanentSourceError
from sports_analytics.core.settings import BookmakersSettings
from sports_analytics.data.migrations import ensure_database_ready
from sports_analytics.sources.betano.catalog import BETANO_CATALOG
from sports_analytics.sources.bookmaker_catalog import (
    FORBIDDEN_JOB_PAYLOAD_KEYS,
    reject_forbidden_job_controls,
)


@pytest.mark.parametrize("key", sorted(FORBIDDEN_JOB_PAYLOAD_KEYS))
def test_forbidden_job_payload_keys_rejected(key: str) -> None:
    with pytest.raises(PermanentSourceError, match="forbidden control keys"):
        reject_forbidden_job_controls({"provider_id": "betano-pt", "sport": "football", key: "x"})


def test_validate_payload_rejects_unknown_and_forbidden_keys() -> None:
    with pytest.raises(PermanentJobError, match="unknown payload keys"):
        validate_bookmaker_ingest_payload(
            {"provider_id": "betano-pt", "sport": "football", "extra": 1}
        )
    with pytest.raises(PermanentSourceError, match="forbidden control keys"):
        reject_forbidden_job_controls(
            {"provider_id": "betano-pt", "sport": "football", "cookies": {}}
        )


def test_unknown_provider_rejected_by_service(tmp_path: Path) -> None:
    database = tmp_path / "operational.sqlite3"
    ensure_database_ready(database)
    service = BookmakerIngestionService(
        database_path=database,
        raw_directory=tmp_path / "raw",
        snapshots_directory=tmp_path / "snapshots",
        bookmakers=BookmakersSettings(enabled=True),
    )
    with pytest.raises(PermanentJobError, match="unsupported bookmaker provider"):
        service.ingest(
            provider_id="unknown-bookie",
            sport="football",
            observed_at_utc=datetime(2026, 7, 26, 12, 0, tzinfo=UTC),
            acquisition_cycle_id="cycle-1",
        )


def test_unsupported_sport_rejected() -> None:
    with pytest.raises(PermanentSourceError, match="unsupported sport"):
        BETANO_CATALOG.routes_for_sport("handball")


def test_enqueue_rejects_unknown_provider_and_sport(tmp_path: Path) -> None:
    database = tmp_path / "operational.sqlite3"
    ensure_database_ready(database)
    settings = BookmakersSettings(enabled=True)
    with pytest.raises(PermanentSourceError, match="unsupported bookmaker provider"):
        enqueue_bookmaker_acquisition(
            database_path=database,
            bookmakers=settings,
            provider_id="pinnacle",
            sport="football",
        )
    with pytest.raises(PermanentSourceError, match="unsupported bookmaker sport"):
        enqueue_bookmaker_acquisition(
            database_path=database,
            bookmakers=settings,
            provider_id="betano-pt",
            sport="handball",
        )
