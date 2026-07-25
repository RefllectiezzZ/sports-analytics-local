"""Football-Data.co.uk source adapter package."""

from sports_analytics.sources.football_data_co_uk.catalog import (
    get_competition,
    list_competitions,
)
from sports_analytics.sources.types import SOURCE_FOOTBALL_DATA_CO_UK

__all__ = [
    "SOURCE_FOOTBALL_DATA_CO_UK",
    "get_competition",
    "list_competitions",
]
