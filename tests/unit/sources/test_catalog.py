"""Static source catalog and football season identifier tests."""

from __future__ import annotations

import pytest

from sports_analytics.core.exceptions import NormalizationError, PermanentSourceError
from sports_analytics.sources.catalog import (
    FOOTBALL_DATA_ADAPTER_VERSION,
    get_source_descriptor,
    list_source_descriptors,
    list_source_names,
)
from sports_analytics.sources.contracts import SourceRole
from sports_analytics.sources.football_data_co_uk.catalog import (
    build_csv_url,
    get_competition,
    list_competitions,
    source_name,
)
from sports_analytics.sources.types import SOURCE_FOOTBALL_DATA_CO_UK
from sports_analytics.sports.football.identifiers import parse_canonical_season
from sports_analytics.sports.types import CompetitionType


def test_top_level_source_catalog_lists_football_data_source() -> None:
    assert SOURCE_FOOTBALL_DATA_CO_UK in list_source_names()
    assert source_name() == SOURCE_FOOTBALL_DATA_CO_UK
    assert (
        tuple(descriptor.source_id for descriptor in list_source_descriptors())
        == list_source_names()
    )
    assert list_source_names() == tuple(sorted(list_source_names()))


def test_football_data_descriptor_describes_the_implemented_adapter() -> None:
    descriptor = get_source_descriptor(SOURCE_FOOTBALL_DATA_CO_UK)

    assert descriptor.display_name == "Football-Data.co.uk"
    assert descriptor.role is SourceRole.HISTORICAL_DATA
    assert descriptor.adapter_version == FOOTBALL_DATA_ADAPTER_VERSION
    assert descriptor.supported_sports == ("football",)
    assert descriptor.requires_network is True
    assert "historical" in descriptor.notes.lower()


def test_get_source_descriptor_rejects_unknown_source() -> None:
    with pytest.raises(PermanentSourceError, match="unsupported source_id"):
        get_source_descriptor("football-data-co-uk-mirror")


def test_competition_catalog_is_sorted_and_unique() -> None:
    competitions = list_competitions()

    assert [entry.competition_id for entry in competitions] == sorted(
        entry.competition_id for entry in competitions
    )
    assert len({entry.competition_id for entry in competitions}) == len(competitions)
    assert len({entry.division_code for entry in competitions}) == len(competitions)


def test_get_competition_returns_expected_premier_league_entry() -> None:
    competition = get_competition("eng-premier-league")

    assert competition.display_name == "Premier League"
    assert competition.country_code == "ENG"
    assert competition.competition_type is CompetitionType.DOMESTIC_LEAGUE
    assert competition.division_code == "E0"
    assert competition.timezone == "Europe/London"
    assert competition.season_format == "cross-year"


def test_get_competition_returns_expected_primeira_liga_entry() -> None:
    competition = get_competition("prt-primeira-liga")

    assert competition.display_name == "Primeira Liga"
    assert competition.country_code == "PRT"
    assert competition.division_code == "P1"
    assert competition.timezone == "Europe/Lisbon"


@pytest.mark.parametrize("competition_id", ["ENG-premier-league", " eng-premier-league", ""])
def test_get_competition_rejects_invalid_identifiers(competition_id: str) -> None:
    with pytest.raises(PermanentSourceError):
        get_competition(competition_id)


def test_get_competition_rejects_unsupported_identifier() -> None:
    with pytest.raises(PermanentSourceError, match="unsupported competition_id"):
        get_competition("deu-bundesliga")


def test_build_csv_url_uses_fixed_allowlisted_football_data_host() -> None:
    assert (
        build_csv_url(division_code="E0", source_season_code="2324")
        == "https://www.football-data.co.uk/mmz4281/2324/E0.csv"
    )


@pytest.mark.parametrize(
    ("division_code", "source_season_code"),
    [
        ("../E0", "2324"),
        ("E0", "../2324"),
        ("E0/../../secret", "2324"),
        ("E0", "23\\24"),
    ],
)
def test_build_csv_url_rejects_path_traversal(
    division_code: str,
    source_season_code: str,
) -> None:
    with pytest.raises(PermanentSourceError):
        build_csv_url(division_code=division_code, source_season_code=source_season_code)


@pytest.mark.parametrize(
    ("season", "expected"),
    [
        ("1993-1994", ("1993-1994", 1993, 1994, "9394")),
        ("2023-2024", ("2023-2024", 2023, 2024, "2324")),
        ("2092-2093", ("2092-2093", 2092, 2093, "9293")),
    ],
)
def test_parse_canonical_season_accepts_supported_cross_year_seasons(
    season: str,
    expected: tuple[str, int, int, str],
) -> None:
    assert parse_canonical_season(season) == expected


@pytest.mark.parametrize(
    "season",
    [
        "",
        " 2023-2024",
        "2023-2024 ",
        "23-24",
        "2023/2024",
        "2023-2023",
        "2023-2025",
        "1992-1993",
        "2093-2094",
    ],
)
def test_parse_canonical_season_rejects_non_canonical_values(season: str) -> None:
    with pytest.raises(NormalizationError):
        parse_canonical_season(season)
