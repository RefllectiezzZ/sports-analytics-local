"""Fixed Betclic Portugal public route catalog."""

from __future__ import annotations

from typing import Final

from sports_analytics.sources.bookmaker_catalog import BookmakerProviderCatalog

PROVIDER_ID: Final[str] = "betclic-pt"
ADAPTER_VERSION: Final[str] = "betclic-pt-adapter-v1"
PARSER_VERSION: Final[str] = "betclic-pt-parser-v1"
ALLOWED_HOSTNAMES: Final[frozenset[str]] = frozenset({"www.betclic.pt", "betclic.pt"})

BETCLIC_CATALOG: Final[BookmakerProviderCatalog] = BookmakerProviderCatalog(
    provider_id=PROVIDER_ID,
    display_name="Betclic Portugal",
    adapter_version=ADAPTER_VERSION,
    parser_version=PARSER_VERSION,
    allowed_hostnames=ALLOWED_HOSTNAMES,
    locale="pt-PT",
    jurisdiction="PT",
    starting_route_id="home",
    starting_url="https://www.betclic.pt/",
    sport_routes={
        "football": (
            ("football-prematch", "https://www.betclic.pt/futebol"),
        ),
        "basketball": (
            ("basketball-prematch", "https://www.betclic.pt/basquetebol"),
        ),
        "tennis": (
            ("tennis-prematch", "https://www.betclic.pt/tenis"),
        ),
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
