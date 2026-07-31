"""Strict The Odds API v4 integration."""

from sports_analytics.providers.the_odds_api.client import (
    THE_ODDS_API_HOST,
    THE_ODDS_API_PROVIDER_ID,
    TheOddsApiClient,
)
from sports_analytics.providers.the_odds_api.contracts import (
    ProviderEvent,
    ProviderOddsBatch,
    ProviderQuota,
)

__all__ = [
    "THE_ODDS_API_HOST",
    "THE_ODDS_API_PROVIDER_ID",
    "ProviderEvent",
    "ProviderOddsBatch",
    "ProviderQuota",
    "TheOddsApiClient",
]
