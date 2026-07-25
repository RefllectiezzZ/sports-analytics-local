"""Deterministic market quote identifiers.

Quote identity is derived from canonical event identity, the canonical market
dimensions, the provider, the phase, and the source field that produced the
price. It therefore stays stable across re-ingestion while still separating two
different provider prices for the same selection.
"""

from __future__ import annotations

import uuid
from typing import Final

from sports_analytics.markets.contracts import MarketSelection

MARKET_ENTITY_NAMESPACE: Final[uuid.UUID] = uuid.UUID("9d4c7f10-38b6-5c2f-8e77-5a1b3d9c6f42")


def build_market_key(
    *,
    sport_code: str,
    market_family: str,
    variant: str,
    market_period: str,
) -> str:
    """Compose a canonical market key from its dimensions."""
    return f"{sport_code}.{market_family}.{variant}.{market_period}"


def build_quote_id(
    *,
    canonical_event_id: str,
    selection: MarketSelection,
    provider_type: str,
    provider_id: str,
    quote_phase: str,
    source_field: str | None,
) -> str:
    """Return a deterministic UUIDv5 for one provider price of one selection."""
    definition = selection.definition
    line = "none" if definition.line_value is None else format(definition.line_value, "f")
    participant = definition.canonical_participant_id or "none"
    key = "|".join(
        (
            "quote",
            canonical_event_id,
            definition.market_key,
            definition.market_family,
            definition.market_period,
            definition.participant_scope,
            participant,
            definition.line_type,
            line,
            selection.outcome_key,
            provider_type,
            provider_id,
            quote_phase,
            source_field or "none",
        )
    )
    return str(uuid.uuid5(MARKET_ENTITY_NAMESPACE, key))
