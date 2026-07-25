"""Football-Data.co.uk odds column families mapped onto generic market contracts.

Football-Data.co.uk publishes historical 1X2 prices only. Those columns are
mapped into the sport-agnostic market contract rather than into a bespoke
``odds_1x2`` abstraction, so future adapters can publish totals, handicaps,
period markets, or player markets through the same dataset.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from sports_analytics.markets.contracts import (
    LineType,
    MarketDefinition,
    MarketSelection,
    ParticipantScope,
    ProviderType,
    QuotePhase,
)
from sports_analytics.markets.identifiers import build_market_key
from sports_analytics.sports.identifiers import SPORT_FOOTBALL

MARKET_FAMILY_MATCH_RESULT: Final[str] = "match-result"
MARKET_PERIOD_FULL_MATCH: Final[str] = "full-match"
MARKET_VARIANT_1X2: Final[str] = "1x2"

MARKET_KEY_MATCH_RESULT_1X2: Final[str] = build_market_key(
    sport_code=SPORT_FOOTBALL,
    market_family=MARKET_FAMILY_MATCH_RESULT,
    variant=MARKET_VARIANT_1X2,
    market_period=MARKET_PERIOD_FULL_MATCH,
)

OUTCOME_HOME: Final[str] = "home"
OUTCOME_DRAW: Final[str] = "draw"
OUTCOME_AWAY: Final[str] = "away"

MATCH_RESULT_1X2_OUTCOMES: Final[tuple[str, ...]] = (OUTCOME_HOME, OUTCOME_DRAW, OUTCOME_AWAY)


def match_result_1x2_definition() -> MarketDefinition:
    """Return the canonical full-match 1X2 market definition for football."""
    return MarketDefinition(
        sport_code=SPORT_FOOTBALL,
        market_family=MARKET_FAMILY_MATCH_RESULT,
        market_key=MARKET_KEY_MATCH_RESULT_1X2,
        market_period=MARKET_PERIOD_FULL_MATCH,
        participant_scope=ParticipantScope.EVENT.value,
        line_type=LineType.NONE.value,
        line_value=None,
        canonical_participant_id=None,
    )


def match_result_1x2_selection(outcome_key: str) -> MarketSelection:
    """Return one canonical 1X2 selection.

    Football-Data.co.uk exposes no market or selection identifiers, so both stay
    ``None`` instead of being invented.
    """
    return MarketSelection(
        definition=match_result_1x2_definition(),
        outcome_key=outcome_key,
        source_market_id=None,
        source_selection_id=None,
    )


@dataclass(frozen=True, slots=True)
class OddsColumnFamily:
    """One provider/phase triple of Football-Data 1X2 columns."""

    provider_type: str
    provider_id: str
    quote_phase: str
    home_column: str
    draw_column: str
    away_column: str
    family_id: str

    def column_for(self, outcome_key: str) -> str:
        """Return the source column that carries ``outcome_key``."""
        mapping = {
            OUTCOME_HOME: self.home_column,
            OUTCOME_DRAW: self.draw_column,
            OUTCOME_AWAY: self.away_column,
        }
        return mapping[outcome_key]


# Explicit supported odds column families only; no dynamic column discovery.
SUPPORTED_ODDS_FAMILIES: Final[tuple[OddsColumnFamily, ...]] = (
    OddsColumnFamily(
        ProviderType.BOOKMAKER.value,
        "bet365",
        QuotePhase.OPENING.value,
        "B365H",
        "B365D",
        "B365A",
        "b365-opening",
    ),
    OddsColumnFamily(
        ProviderType.BOOKMAKER.value,
        "bet365",
        QuotePhase.CLOSING.value,
        "B365CH",
        "B365CD",
        "B365CA",
        "b365-closing",
    ),
    OddsColumnFamily(
        ProviderType.BOOKMAKER.value,
        "pinnacle",
        QuotePhase.OPENING.value,
        "PSH",
        "PSD",
        "PSA",
        "pinnacle-opening",
    ),
    OddsColumnFamily(
        ProviderType.BOOKMAKER.value,
        "pinnacle",
        QuotePhase.CLOSING.value,
        "PSCH",
        "PSCD",
        "PSCA",
        "pinnacle-closing",
    ),
    OddsColumnFamily(
        ProviderType.SOURCE_MARKET_AVERAGE.value,
        "market-average",
        QuotePhase.OPENING.value,
        "AvgH",
        "AvgD",
        "AvgA",
        "avg-opening",
    ),
    OddsColumnFamily(
        ProviderType.SOURCE_MARKET_AVERAGE.value,
        "market-average",
        QuotePhase.CLOSING.value,
        "AvgCH",
        "AvgCD",
        "AvgCA",
        "avg-closing",
    ),
    OddsColumnFamily(
        ProviderType.SOURCE_MARKET_MAXIMUM.value,
        "market-maximum",
        QuotePhase.OPENING.value,
        "MaxH",
        "MaxD",
        "MaxA",
        "max-opening",
    ),
    OddsColumnFamily(
        ProviderType.SOURCE_MARKET_MAXIMUM.value,
        "market-maximum",
        QuotePhase.CLOSING.value,
        "MaxCH",
        "MaxCD",
        "MaxCA",
        "max-closing",
    ),
)

SUPPORTED_ODDS_COLUMNS: Final[frozenset[str]] = frozenset(
    column
    for family in SUPPORTED_ODDS_FAMILIES
    for column in (family.home_column, family.draw_column, family.away_column)
)
