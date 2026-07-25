"""Source adapter shared type constants."""

from __future__ import annotations

from typing import Final

SOURCE_FOOTBALL_DATA_CO_UK: Final[str] = "football-data-co-uk"
ALLOWED_FOOTBALL_DATA_HOST: Final[str] = "www.football-data.co.uk"
FOOTBALL_DATA_URL_TEMPLATE: Final[str] = (
    "https://www.football-data.co.uk/mmz4281/{source_season_code}/{division_code}.csv"
)
DEFAULT_USER_AGENT: Final[str] = (
    "sports-analytics-local/0.1 (+local football-data-co-uk ingestion; no browser)"
)
