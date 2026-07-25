"""Static Football-Data.co.uk competition catalog."""

from __future__ import annotations

from sports_analytics.core.exceptions import PermanentSourceError, RepositoryError
from sports_analytics.data.types import validate_identifier
from sports_analytics.sources.football_data_co_uk.types import (
    SEASON_FORMAT_CROSS_YEAR,
    FootballDataCompetition,
)
from sports_analytics.sources.types import FOOTBALL_DATA_URL_TEMPLATE, SOURCE_FOOTBALL_DATA_CO_UK
from sports_analytics.sports.types import CompetitionType

_COMPETITIONS: tuple[FootballDataCompetition, ...] = (
    FootballDataCompetition(
        competition_id="eng-premier-league",
        display_name="Premier League",
        country_code="ENG",
        competition_type=CompetitionType.DOMESTIC_LEAGUE,
        division_code="E0",
        timezone="Europe/London",
        season_format=SEASON_FORMAT_CROSS_YEAR,
    ),
    FootballDataCompetition(
        competition_id="prt-primeira-liga",
        display_name="Primeira Liga",
        country_code="PRT",
        competition_type=CompetitionType.DOMESTIC_LEAGUE,
        division_code="P1",
        timezone="Europe/Lisbon",
        season_format=SEASON_FORMAT_CROSS_YEAR,
    ),
)


def _validate_catalog(
    entries: tuple[FootballDataCompetition, ...],
) -> tuple[FootballDataCompetition, ...]:
    ids: set[str] = set()
    divisions: set[str] = set()
    ordered = tuple(sorted(entries, key=lambda item: item.competition_id))
    for entry in ordered:
        try:
            validate_identifier(entry.competition_id, field_name="competition_id")
        except RepositoryError as exc:
            raise PermanentSourceError(str(exc)) from exc
        if entry.competition_id in ids:
            msg = f"duplicate competition_id in catalog: {entry.competition_id}"
            raise PermanentSourceError(msg)
        if entry.division_code in divisions:
            msg = f"duplicate source division code in catalog: {entry.division_code}"
            raise PermanentSourceError(msg)
        ids.add(entry.competition_id)
        divisions.add(entry.division_code)
    return ordered


COMPETITIONS: tuple[FootballDataCompetition, ...] = _validate_catalog(_COMPETITIONS)


def list_competitions() -> tuple[FootballDataCompetition, ...]:
    """Return the static competition catalog in deterministic order."""
    return COMPETITIONS


def get_competition(competition_id: str) -> FootballDataCompetition:
    """Resolve a competition through the static catalog only."""
    try:
        normalized = validate_identifier(competition_id, field_name="competition_id")
    except RepositoryError as exc:
        raise PermanentSourceError(str(exc)) from exc
    for entry in COMPETITIONS:
        if entry.competition_id == normalized:
            return entry
    msg = f"unsupported competition_id: {normalized}"
    raise PermanentSourceError(msg)


def build_csv_url(*, division_code: str, source_season_code: str) -> str:
    """Construct the fixed Football-Data.co.uk CSV URL for a catalog entry."""
    if "/" in division_code or "\\" in division_code or ".." in division_code:
        msg = "invalid division_code"
        raise PermanentSourceError(msg)
    if "/" in source_season_code or "\\" in source_season_code or ".." in source_season_code:
        msg = "invalid source_season_code"
        raise PermanentSourceError(msg)
    # Catalog division codes are uppercase (E0, P1); season codes are digits.
    return FOOTBALL_DATA_URL_TEMPLATE.format(
        source_season_code=source_season_code,
        division_code=division_code,
    )


def source_name() -> str:
    """Return the static source identifier."""
    return SOURCE_FOOTBALL_DATA_CO_UK
