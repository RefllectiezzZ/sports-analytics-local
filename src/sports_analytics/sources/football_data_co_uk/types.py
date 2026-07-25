"""Football-Data.co.uk adapter-specific types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from sports_analytics.sports.types import CompetitionType

SEASON_FORMAT_CROSS_YEAR: Final[str] = "cross-year"


@dataclass(frozen=True, slots=True)
class FootballDataCompetition:
    """Static catalog entry for one Football-Data.co.uk competition."""

    competition_id: str
    display_name: str
    country_code: str
    competition_type: CompetitionType
    division_code: str
    timezone: str
    season_format: str
