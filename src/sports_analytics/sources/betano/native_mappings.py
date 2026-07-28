"""Project-owned Betano native market code mappings.

Provider payloads supply market type codes only. Canonical market definition IDs
are resolved exclusively through this mapping table.
"""

from __future__ import annotations

from typing import Final

BETANO_NATIVE_SCHEMA: Final[str] = "betano-offering-v1"

#: Provider market type code -> project canonical market definition id.
BETANO_MARKET_TYPE_MAPPINGS: Final[dict[str, str]] = {
    "MRES": "football-match-result-1x2",
    "TOTG": "football-total-goals",
    "BTTS": "football-btts",
    "MWIN": "basketball-match-winner-with-ot",
    "TOTP": "basketball-total-points-with-ot",
    "SPRD": "basketball-spread-with-ot",
    "TMWN": "tennis-match-winner",
}

BETANO_SPORT_CODE_MAPPINGS: Final[dict[str, str]] = {
    "FOOT": "football",
    "BASK": "basketball",
    "TENN": "tennis",
}

BETANO_PERIOD_MAPPINGS: Final[dict[str, str]] = {
    "FT": "full-match",
    "REG": "regular-time",
    "OT": "including-overtime",
}

BETANO_PARTICIPANT_ROLE_MAPPINGS: Final[dict[str, str]] = {
    "HOME": "home",
    "AWAY": "away",
    "PLAYER1": "player-1",
    "PLAYER2": "player-2",
}


def map_betano_market_type(code: str) -> str | None:
    """Map a provider market type code to a canonical market definition id."""
    return BETANO_MARKET_TYPE_MAPPINGS.get(code.upper())


def map_betano_sport_code(code: str, *, default_sport: str) -> str:
    """Map a provider sport code to the project sport identifier."""
    return BETANO_SPORT_CODE_MAPPINGS.get(code.upper(), default_sport)
