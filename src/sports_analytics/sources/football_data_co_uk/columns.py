"""Known Football-Data.co.uk CSV column classifications."""

from __future__ import annotations

from typing import Final

from sports_analytics.sports.football.normalization import SUPPORTED_ODDS_COLUMNS

REQUIRED_COLUMNS: Final[tuple[str, ...]] = (
    "Div",
    "Date",
    "HomeTeam",
    "AwayTeam",
    "FTHG",
    "FTAG",
    "FTR",
)

OPTIONAL_COLUMNS: Final[frozenset[str]] = frozenset(
    {
        "Time",
        "HTHG",
        "HTAG",
        "HTR",
        "Referee",
        "HS",
        "AS",
        "HST",
        "AST",
        "HF",
        "AF",
        "HC",
        "AC",
        "HY",
        "AY",
        "HR",
        "AR",
    }
)

SUPPORTED_OPTIONAL_AND_ODDS: Final[frozenset[str]] = OPTIONAL_COLUMNS | SUPPORTED_ODDS_COLUMNS

MAX_FIELD_LENGTH: Final[int] = 4_096
MAX_ROW_COUNT: Final[int] = 5_000
MAX_LINE_LENGTH: Final[int] = 65_536
