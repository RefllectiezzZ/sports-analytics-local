"""Project-owned Betclic native market code mappings."""

from __future__ import annotations

from typing import Final

from sports_analytics.sources.bookmaker_contracts import CanonicalOutcomeKey

BETCLIC_NATIVE_SCHEMA: Final[str] = "betclic-offering-v1"

BETCLIC_MARKET_TYPE_MAPPINGS: Final[dict[str, str]] = {
    "1N2": "football-match-result-1x2",
    "TOTAL_GOALS": "football-total-goals",
    "BOTH_TEAMS_SCORE": "football-btts",
    "MATCH_WINNER_OT": "basketball-match-winner-with-ot",
    "TOTAL_POINTS_OT": "basketball-total-points-with-ot",
    "HANDICAP_OT": "basketball-spread-with-ot",
    "MATCH_WINNER": "tennis-match-winner",
}

BETCLIC_SPORT_CODE_MAPPINGS: Final[dict[str, str]] = {
    "football": "football",
    "basketball": "basketball",
    "tennis": "tennis",
}

BETCLIC_PERIOD_MAPPINGS: Final[dict[str, str]] = {
    "FULL_TIME": "full-match",
    "REGULATION": "regular-time",
    "WITH_OT": "including-overtime",
}

BETCLIC_PARTICIPANT_ROLE_MAPPINGS: Final[dict[str, str]] = {
    "HOME": "home",
    "AWAY": "away",
    "PLAYER_A": "player-1",
    "PLAYER_B": "player-2",
}

BETCLIC_OUTCOME_MAPPINGS: Final[dict[tuple[str, str], CanonicalOutcomeKey]] = {
    ("1N2", "1"): CanonicalOutcomeKey.HOME,
    ("1N2", "Home"): CanonicalOutcomeKey.HOME,
    ("1N2", "X"): CanonicalOutcomeKey.DRAW,
    ("1N2", "Draw"): CanonicalOutcomeKey.DRAW,
    ("1N2", "2"): CanonicalOutcomeKey.AWAY,
    ("1N2", "Away"): CanonicalOutcomeKey.AWAY,
    ("TOTAL_GOALS", "Over"): CanonicalOutcomeKey.OVER,
    ("TOTAL_GOALS", "Under"): CanonicalOutcomeKey.UNDER,
    ("BOTH_TEAMS_SCORE", "Yes"): CanonicalOutcomeKey.YES,
    ("BOTH_TEAMS_SCORE", "No"): CanonicalOutcomeKey.NO,
    ("MATCH_WINNER_OT", "Home"): CanonicalOutcomeKey.HOME,
    ("MATCH_WINNER_OT", "Away"): CanonicalOutcomeKey.AWAY,
    ("TOTAL_POINTS_OT", "Over"): CanonicalOutcomeKey.OVER,
    ("TOTAL_POINTS_OT", "Under"): CanonicalOutcomeKey.UNDER,
    ("HANDICAP_OT", "Home"): CanonicalOutcomeKey.HOME,
    ("HANDICAP_OT", "Away"): CanonicalOutcomeKey.AWAY,
    ("MATCH_WINNER", "Home"): CanonicalOutcomeKey.HOME,
    ("MATCH_WINNER", "Away"): CanonicalOutcomeKey.AWAY,
}


def map_betclic_market_type(code: str) -> str | None:
    """Map a provider market type code to a canonical market definition id."""
    return BETCLIC_MARKET_TYPE_MAPPINGS.get(code.upper())


def map_betclic_sport_code(code: str, *, default_sport: str) -> str:
    """Map a provider sport code to the project sport identifier."""
    return BETCLIC_SPORT_CODE_MAPPINGS.get(code.lower(), default_sport)


def map_betclic_selection_outcome(
    market_type_code: str,
    selection_label: str,
) -> CanonicalOutcomeKey | None:
    """Map only exact reviewed provider market/selection label pairs."""
    return BETCLIC_OUTCOME_MAPPINGS.get((market_type_code.upper(), selection_label.strip()))
