"""Leakage-boundary tests: post-event facts must never reach pre-event rows.

Every fact that only exists once a match has been played (scores, results,
half-time goals, shots, cards) has to be reachable only through a row that
explicitly declares its availability stage. These tests pin that boundary at
both ends: normalization output and the ``CanonicalEvent`` contract itself.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from sports_analytics.core.exceptions import NormalizationError
from sports_analytics.sources.football_data_co_uk.catalog import get_competition
from sports_analytics.sources.types import SOURCE_FOOTBALL_DATA_CO_UK
from sports_analytics.sports.contracts import CanonicalEvent
from sports_analytics.sports.football.contracts import FOOTBALL_CANONICAL_SCHEMA_VERSION
from sports_analytics.sports.football.identifiers import parse_canonical_season
from sports_analytics.sports.football.normalization import (
    NormalizedFootballBundle,
    normalize_football_rows,
)
from tests.helpers_snapshots import (
    OBSERVED_AT,
    SYNTHETIC_CSV_WITH_ODDS,
    build_bundle,
    store_artifact,
)

SOURCE_FILE_SHA256 = "d" * 64
COMPETITION_ID = "eng-premier-league"
SEASON_ID = "eng-premier-league:2023-2024"


def _normalize(rows: list[dict[str, str]]) -> NormalizedFootballBundle:
    competition = get_competition(COMPETITION_ID)
    label, start_year, end_year, source_season_code = parse_canonical_season("2023-2024")
    return normalize_football_rows(
        rows=rows,
        competition_id=competition.competition_id,
        competition_display_name=competition.display_name,
        country_code=competition.country_code,
        source_competition_code=competition.division_code,
        timezone_name=competition.timezone,
        season_label=label,
        start_year=start_year,
        end_year=end_year,
        source_season_code=source_season_code,
        source_name=SOURCE_FOOTBALL_DATA_CO_UK,
        source_file_sha256=SOURCE_FILE_SHA256,
        source_observed_at_utc=OBSERVED_AT,
    )


def _finished_row(**overrides: str) -> dict[str, str]:
    row = {
        "Div": "E0",
        "Date": "12/08/2023",
        "Time": "15:00",
        "HomeTeam": "Northbridge FC",
        "AwayTeam": "Southport Athletic",
        "FTHG": "2",
        "FTAG": "1",
        "FTR": "H",
        "HTHG": "1",
        "HTAG": "0",
        "HTR": "H",
        "Referee": "A Official",
        "HS": "10",
        "AS": "8",
        "HST": "5",
        "AST": "3",
        "HF": "12",
        "AF": "14",
        "HC": "5",
        "AC": "4",
        "HY": "2",
        "AY": "1",
        "HR": "0",
        "AR": "0",
    }
    row.update(overrides)
    return row


def _scheduled_row(**overrides: str) -> dict[str, str]:
    row = {
        "Div": "E0",
        "Date": "19/08/2023",
        "Time": "",
        "HomeTeam": "Eastvale United",
        "AwayTeam": "Westmoor Rangers",
        "FTHG": "",
        "FTAG": "",
        "FTR": "",
    }
    row.update(overrides)
    return row


def _canonical_event(
    *,
    status: str = "scheduled",
    home_score: int | None = None,
    away_score: int | None = None,
    result_code: str | None = None,
    outcome_availability_stage: str = "pre-event-unavailable",
) -> CanonicalEvent:
    return CanonicalEvent(
        canonical_event_id="9f1c0e64-1c1a-5f2a-9c62-1de1b0a0f001",
        sport_code="football",
        competition_id=COMPETITION_ID,
        season_id=SEASON_ID,
        event_date=date(2023, 8, 12),
        scheduled_start_utc=datetime(2023, 8, 12, 14, 0, tzinfo=UTC),
        start_time_precision="minute",
        status=status,
        home_canonical_participant_id="1c9a6f2b-0000-5000-8000-000000000001",
        away_canonical_participant_id="1c9a6f2b-0000-5000-8000-000000000002",
        home_score=home_score,
        away_score=away_score,
        result_code=result_code,
        outcome_availability_stage=outcome_availability_stage,
        schema_version=FOOTBALL_CANONICAL_SCHEMA_VERSION,
    )


def test_post_match_statistics_always_declare_the_post_match_stage(tmp_path: Path) -> None:
    artifact = store_artifact(tmp_path / "raw", content=SYNTHETIC_CSV_WITH_ODDS)

    bundle = build_bundle(
        artifact,
        competition=get_competition(COMPETITION_ID),
        content=SYNTHETIC_CSV_WITH_ODDS,
    )

    assert bundle.post_match_statistics
    assert {item.availability_stage for item in bundle.post_match_statistics} == {"post-match"}


def test_post_match_statistics_stage_holds_for_mixed_scheduled_and_finished_rows() -> None:
    bundle = _normalize([_finished_row(), _scheduled_row()])

    assert len(bundle.events) == 2
    assert len(bundle.post_match_statistics) == 1
    assert all(item.availability_stage == "post-match" for item in bundle.post_match_statistics)
    finished_event_ids = {
        event.canonical.canonical_event_id
        for event in bundle.events
        if event.canonical.status == "finished"
    }
    assert {item.canonical_event_id for item in bundle.post_match_statistics} == finished_event_ids


def test_scheduled_events_expose_no_outcome() -> None:
    bundle = _normalize([_scheduled_row()])

    canonical = bundle.events[0].canonical
    assert canonical.status == "scheduled"
    assert canonical.home_score is None
    assert canonical.away_score is None
    assert canonical.result_code is None
    assert canonical.outcome_availability_stage == "pre-event-unavailable"
    assert bundle.post_match_statistics == ()


def test_scheduled_event_discards_post_match_columns_supplied_by_the_source() -> None:
    # A source may carry half-time and match statistics on an unplayed fixture;
    # normalization must not surface them as post-match facts.
    row = _scheduled_row(
        HTHG="1",
        HTAG="0",
        HTR="H",
        Referee="A Official",
        HS="10",
        AS="8",
    )

    bundle = _normalize([row])

    assert bundle.events[0].canonical.status == "scheduled"
    assert bundle.events[0].canonical.outcome_availability_stage == "pre-event-unavailable"
    assert bundle.post_match_statistics == ()


def test_scheduled_event_still_carries_pre_event_market_quotes() -> None:
    row = _scheduled_row(B365H="1.90", B365D="3.40", B365A="4.10")

    bundle = _normalize([row])

    assert bundle.events[0].canonical.outcome_availability_stage == "pre-event-unavailable"
    assert len(bundle.market_quotes) == 3
    assert bundle.post_match_statistics == ()


def test_finished_events_are_marked_post_event() -> None:
    bundle = _normalize([_finished_row()])

    canonical = bundle.events[0].canonical
    assert canonical.status == "finished"
    assert (canonical.home_score, canonical.away_score) == (2, 1)
    assert canonical.result_code == "home"
    assert canonical.outcome_availability_stage == "post-event"


def test_canonical_event_rejects_scheduled_status_with_a_score() -> None:
    with pytest.raises(NormalizationError, match="only finished canonical events may carry scores"):
        _canonical_event(home_score=2, away_score=1, result_code="home")


def test_canonical_event_rejects_finished_event_without_scores() -> None:
    with pytest.raises(NormalizationError, match="finished canonical events require both scores"):
        _canonical_event(status="finished", outcome_availability_stage="post-event")


def test_canonical_event_rejects_finished_event_claiming_a_pre_event_stage() -> None:
    with pytest.raises(NormalizationError, match="must record a post-event outcome stage"):
        _canonical_event(
            status="finished",
            home_score=2,
            away_score=1,
            result_code="home",
            outcome_availability_stage="pre-event-unavailable",
        )


def test_canonical_event_rejects_unfinished_event_claiming_a_post_event_stage() -> None:
    with pytest.raises(NormalizationError, match="must mark the outcome unavailable"):
        _canonical_event(outcome_availability_stage="post-event")
