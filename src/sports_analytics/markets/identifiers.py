"""Deterministic market quote series and observation identifiers.

``quote_series_id``
    stable identity for one canonical event, market selection, and provider;
``quote_observation_id``
    one concrete source observation of that series, distinguished by provenance
    and time dimensions.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Final

from sports_analytics.data.codec import format_utc_timestamp
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


def build_quote_series_id(
    *,
    canonical_event_id: str,
    selection: MarketSelection,
    provider_type: str,
    provider_id: str,
) -> str:
    """Return a stable UUIDv5 for one provider's series of a market selection."""
    definition = selection.definition
    line = "none" if definition.line_value is None else format(definition.line_value, "f")
    participant = definition.canonical_participant_id or "none"
    key = "|".join(
        (
            "quote-series",
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
        )
    )
    return str(uuid.uuid5(MARKET_ENTITY_NAMESPACE, key))


def build_quote_observation_id(
    *,
    quote_series_id: str,
    source_name: str,
    source_event_id: str,
    selection: MarketSelection,
    provider_type: str,
    provider_id: str,
    quote_phase: str,
    source_observed_at_utc: datetime,
    quoted_at_utc: datetime | None,
    source_file_sha256: str,
    source_field: str | None,
) -> str:
    """Return a UUIDv5 for one concrete observation of a quote series."""
    quoted = "none" if quoted_at_utc is None else format_utc_timestamp(quoted_at_utc)
    key = "|".join(
        (
            "quote-observation",
            quote_series_id,
            source_name,
            source_event_id,
            selection.source_market_id or "none",
            selection.source_selection_id or "none",
            provider_type,
            provider_id,
            quote_phase,
            format_utc_timestamp(source_observed_at_utc),
            quoted,
            source_file_sha256,
            source_field or "none",
        )
    )
    return str(uuid.uuid5(MARKET_ENTITY_NAMESPACE, key))


def build_quote_id(
    *,
    canonical_event_id: str,
    selection: MarketSelection,
    provider_type: str,
    provider_id: str,
    quote_phase: str,
    source_field: str | None,
) -> str:
    """Deprecated compatibility wrapper around series identity.

    Prefer :func:`build_quote_series_id` and :func:`build_quote_observation_id`.
    Retained only for transitional call sites that still pass phase/field; those
    dimensions belong on the observation, not the series.
    """
    _ = (quote_phase, source_field)
    return build_quote_series_id(
        canonical_event_id=canonical_event_id,
        selection=selection,
        provider_type=provider_type,
        provider_id=provider_id,
    )
