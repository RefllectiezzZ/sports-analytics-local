"""Football normalization, odds, statistics, and identifier tests."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from sports_analytics.core.exceptions import NormalizationError
from sports_analytics.sources.football_data_co_uk.catalog import get_competition
from sports_analytics.sources.football_data_co_uk.parser import parse_football_data_csv
from sports_analytics.sources.types import SOURCE_FOOTBALL_DATA_CO_UK
from sports_analytics.sports.football.identifiers import (
    build_game_id,
    build_quote_id,
    build_source_game_key,
    build_team_id,
    parse_canonical_season,
)
from sports_analytics.sports.football.normalization import (
    MARKET_TYPE_1X2,
    PINNACLE_CAUTION_CUTOFF,
    NormalizedFootballBundle,
    normalize_football_rows,
)

SOURCE_FILE_SHA256 = "c" * 64
OBSERVED_AT = datetime(2025, 1, 2, 3, 4, 5, tzinfo=UTC)


def _fixture_rows(name: str, *, expected_division_code: str) -> list[dict[str, str]]:
    content = (Path(__file__).parents[2] / "fixtures" / "football_data_co_uk" / name).read_bytes()
    parsed = parse_football_data_csv(content, expected_division_code=expected_division_code)
    return list(parsed.rows)


def _normalize(
    rows: list[dict[str, str]],
    *,
    competition_id: str = "eng-premier-league",
    season: str = "2023-2024",
) -> NormalizedFootballBundle:
    competition = get_competition(competition_id)
    label, start_year, end_year, source_season_code = parse_canonical_season(season)
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


def test_normalize_synthetic_fixture_builds_canonical_bundle() -> None:
    rows = _fixture_rows("epl_2023_2024_synthetic.csv", expected_division_code="E0")

    bundle = _normalize(rows)

    assert bundle.competitions[0].competition_id == "eng-premier-league"
    assert bundle.competitions[0].source_competition_code == "E0"
    assert bundle.seasons[0].season_id == "eng-premier-league:2023-2024"
    assert bundle.seasons[0].source_season_code == "2324"
    assert {team.display_name for team in bundle.teams} == {
        "Northbridge FC",
        "Southport Athletic",
    }
    assert len(bundle.games) == 3
    assert len(bundle.odds_1x2) == 48
    assert len(bundle.post_match_statistics) == 2
    assert bundle.duplicate_rows_discarded == 0
    assert bundle.warnings == ()
    assert bundle.pinnacle_caution_quote_count == 0


def test_normalize_fixture_games_have_expected_statuses_and_times() -> None:
    rows = _fixture_rows("epl_2023_2024_synthetic.csv", expected_division_code="E0")

    bundle = _normalize(rows)

    first, second, third = bundle.games
    assert first.status == "finished"
    assert first.full_time_result == "home"
    assert first.scheduled_start_utc == datetime(2023, 8, 12, 14, 0, tzinfo=UTC)
    assert first.start_time_precision == "minute"
    assert second.full_time_result == "draw"
    assert second.scheduled_start_utc == datetime(2023, 8, 12, 16, 30, tzinfo=UTC)
    assert third.status == "scheduled"
    assert third.full_time_home_goals is None
    assert third.scheduled_start_utc is None
    assert third.start_time_precision == "date-only"


def test_normalize_fixture_odds_are_decimal_and_deterministically_identified() -> None:
    rows = _fixture_rows("epl_2023_2024_synthetic.csv", expected_division_code="E0")
    bundle = _normalize(rows)
    first_game = bundle.games[0]

    quote = next(
        item
        for item in bundle.odds_1x2
        if item.game_id == first_game.game_id
        and item.provider_id == "bet365"
        and item.quote_phase == "opening"
        and item.selection == "home"
    )

    assert quote.market_type == MARKET_TYPE_1X2
    assert quote.decimal_odds == Decimal("1.8000")
    assert quote.source_column == "B365H"
    assert quote.quoted_at_utc is None
    assert quote.source_observed_at_utc == OBSERVED_AT
    assert quote.quote_id == build_quote_id(
        game_id=first_game.game_id,
        market_type=MARKET_TYPE_1X2,
        selection="home",
        provider_type="bookmaker",
        provider_id="bet365",
        quote_phase="opening",
        source_column_family="b365-opening",
    )


def test_normalize_fixture_statistics_are_post_match_only() -> None:
    rows = _fixture_rows("epl_2023_2024_synthetic.csv", expected_division_code="E0")

    bundle = _normalize(rows)

    stats = bundle.post_match_statistics[0]
    assert stats.game_id == bundle.games[0].game_id
    assert stats.referee == "A Official"
    assert stats.home_shots == 10
    assert stats.away_shots == 8
    assert stats.home_shots_on_target == 5
    assert stats.away_shots_on_target == 3
    assert stats.home_corners == 5
    assert stats.away_corners == 4
    assert stats.home_fouls == 12
    assert stats.away_fouls == 14
    assert stats.home_yellow_cards == 2
    assert stats.away_yellow_cards == 1
    assert stats.home_red_cards == 0
    assert stats.away_red_cards == 0
    assert stats.availability_stage == "post-match"


def test_normalize_accepts_minimal_primeira_liga_fixture_without_statistics() -> None:
    rows = _fixture_rows("prt_2023_2024_synthetic.csv", expected_division_code="P1")

    bundle = _normalize(rows, competition_id="prt-primeira-liga")

    assert bundle.competitions[0].competition_id == "prt-primeira-liga"
    assert len(bundle.games) == 2
    assert len(bundle.odds_1x2) == 6
    assert bundle.post_match_statistics == ()


def test_normalize_discards_exact_duplicate_rows() -> None:
    row = _finished_row()

    bundle = _normalize([row, dict(row)])

    assert len(bundle.games) == 1
    assert bundle.duplicate_rows_discarded == 1
    assert bundle.warnings == ("discarded_exact_duplicate_rows=1",)


def test_normalize_rejects_conflicting_duplicate_source_game_key() -> None:
    with pytest.raises(NormalizationError, match="conflicting duplicate source game key"):
        _normalize([_finished_row(), _finished_row(FTHG="3", FTAG="1")])


def test_normalize_requires_timezone_aware_observation_time() -> None:
    competition = get_competition("eng-premier-league")
    label, start_year, end_year, source_season_code = parse_canonical_season("2023-2024")

    with pytest.raises(NormalizationError, match="timezone-aware"):
        normalize_football_rows(
            rows=[_finished_row()],
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
            source_observed_at_utc=datetime(2025, 1, 2, 3, 4, 5),
        )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"FTHG": "1", "FTAG": "0", "FTR": "A"}, "FTR inconsistent"),
        ({"FTHG": "", "FTAG": "", "FTR": "H"}, "FTR must be empty"),
        ({"HTHG": "1", "HTAG": "0", "HTR": "A"}, "HTR inconsistent"),
        ({"HomeTeam": "Northbridge FC", "AwayTeam": "Northbridge\tFC"}, "must differ"),
        ({"Date": "2023-08-12"}, "Date must use"),
        ({"Time": "25:00"}, "Time must use"),
    ],
)
def test_normalize_rejects_invalid_game_rows(
    overrides: dict[str, str],
    message: str,
) -> None:
    with pytest.raises(NormalizationError, match=message):
        _normalize([_finished_row(**overrides)])


def test_normalize_rejects_partial_odds_triples() -> None:
    row = _finished_row(B365H="1.80", B365D="", B365A="4.50")

    with pytest.raises(NormalizationError, match="requires a complete H/D/A triple"):
        _normalize([row])


@pytest.mark.parametrize("bad_odds", ["true", "NaN", "1.00", "1,80"])
def test_normalize_rejects_invalid_decimal_odds(bad_odds: str) -> None:
    row = _finished_row(B365H=bad_odds, B365D="3.50", B365A="4.50")

    with pytest.raises(NormalizationError):
        _normalize([row])


@pytest.mark.parametrize(
    ("event_date", "expected_status", "expected_count"),
    [
        ("22/07/2025", "source-provided", 0),
        ("23/07/2025", "caution", 3),
    ],
)
def test_pinnacle_odds_quality_changes_at_documented_cutoff(
    event_date: str,
    expected_status: str,
    expected_count: int,
) -> None:
    row = _finished_row(Date=event_date, PSH="1.80", PSD="3.50", PSA="4.50")

    bundle = _normalize([row], season="2025-2026")

    pinnacle_quotes = [quote for quote in bundle.odds_1x2 if quote.provider_id == "pinnacle"]
    assert len(pinnacle_quotes) == 3
    assert {quote.quality_status for quote in pinnacle_quotes} == {expected_status}
    assert bundle.pinnacle_caution_quote_count == expected_count
    if expected_status == "caution":
        assert all(
            str(PINNACLE_CAUTION_CUTOFF) in (quote.quality_reason or "")
            for quote in pinnacle_quotes
        )
    else:
        assert all(quote.quality_reason is None for quote in pinnacle_quotes)


def test_statistics_referee_is_normalized() -> None:
    row = _finished_row(Referee="  A   Official  ")

    bundle = _normalize([row])

    assert bundle.post_match_statistics[0].referee == "A Official"


def test_statistics_reject_partial_home_away_pairs() -> None:
    row = _finished_row(HS="10", AS="")

    with pytest.raises(NormalizationError, match="HS/AS must both be present"):
        _normalize([row])


def test_scheduled_game_with_no_post_match_fields_has_no_statistics() -> None:
    row = {
        "Div": "E0",
        "Date": "01/05/2024",
        "Time": "",
        "HomeTeam": "Northbridge FC",
        "AwayTeam": "Southport Athletic",
        "FTHG": "",
        "FTAG": "",
        "FTR": "",
    }

    bundle = _normalize([row])

    assert bundle.games[0].status == "scheduled"
    assert bundle.post_match_statistics == ()


def test_team_game_and_quote_identifiers_are_deterministic_uuids() -> None:
    team_id = build_team_id(
        source_name=SOURCE_FOOTBALL_DATA_CO_UK,
        normalized_source_team_key="northbridge fc",
    )
    same_team_id = build_team_id(
        source_name=SOURCE_FOOTBALL_DATA_CO_UK,
        normalized_source_team_key="northbridge fc",
    )
    other_team_id = build_team_id(
        source_name=SOURCE_FOOTBALL_DATA_CO_UK,
        normalized_source_team_key="southport athletic",
    )
    source_game_key = build_source_game_key(
        source_name=SOURCE_FOOTBALL_DATA_CO_UK,
        competition_id="eng-premier-league",
        season_id="eng-premier-league:2023-2024",
        event_date=date(2023, 8, 12).isoformat(),
        home_team_key="northbridge fc",
        away_team_key="southport athletic",
    )
    game_id = build_game_id(source_game_key=source_game_key)
    quote_id = build_quote_id(
        game_id=game_id,
        market_type=MARKET_TYPE_1X2,
        selection="home",
        provider_type="bookmaker",
        provider_id="bet365",
        quote_phase="opening",
        source_column_family="b365-opening",
    )
    away_quote_id = build_quote_id(
        game_id=game_id,
        market_type=MARKET_TYPE_1X2,
        selection="away",
        provider_type="bookmaker",
        provider_id="bet365",
        quote_phase="opening",
        source_column_family="b365-opening",
    )

    assert team_id == same_team_id
    assert team_id != other_team_id
    assert quote_id != away_quote_id
    uuid.UUID(team_id)
    uuid.UUID(game_id)
    uuid.UUID(quote_id)
