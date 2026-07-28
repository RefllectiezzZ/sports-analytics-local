"""Fixed Betano Portugal public route catalog."""

from __future__ import annotations

from typing import Final

from sports_analytics.sources.bookmaker_catalog import BookmakerProviderCatalog

PROVIDER_ID: Final[str] = "betano-pt"
ADAPTER_VERSION: Final[str] = "betano-pt-adapter-v1"
PARSER_VERSION: Final[str] = "betano-pt-parser-v1"
ALLOWED_HOSTNAMES: Final[frozenset[str]] = frozenset({"www.betano.pt", "betano.pt"})

BETANO_CATALOG: Final[BookmakerProviderCatalog] = BookmakerProviderCatalog(
    provider_id=PROVIDER_ID,
    display_name="Betano Portugal",
    adapter_version=ADAPTER_VERSION,
    parser_version=PARSER_VERSION,
    allowed_hostnames=ALLOWED_HOSTNAMES,
    locale="pt-PT",
    jurisdiction="PT",
    starting_route_id="home",
    starting_url="https://www.betano.pt/",
    sport_routes={
        "football": (("football-prematch", "https://www.betano.pt/sport/futebol/"),),
        "basketball": (("basketball-prematch", "https://www.betano.pt/sport/basquetebol/"),),
        "tennis": (("tennis-prematch", "https://www.betano.pt/sport/tenis/"),),
    },
)

SUPPORTED_MARKET_DEFINITION_IDS: Final[tuple[str, ...]] = (
    "basketball-match-winner-with-ot",
    "basketball-total-points-with-ot",
    "basketball-spread-with-ot",
    "football-btts",
    "football-match-result-1x2",
    "football-total-goals",
    "tennis-match-winner",
)
