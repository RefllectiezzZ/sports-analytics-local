"""Generic market quote Arrow schema and row builder."""

from __future__ import annotations

from typing import Any, Final

import pyarrow as pa

from sports_analytics.markets.contracts import OddsQuote
from sports_analytics.snapshots.arrow import (
    dataset_metadata,
    dictionary_string,
    line_decimal,
    price_decimal,
    utc_timestamp,
)

DATASET_MARKET_QUOTES: Final[str] = "market_quotes"
MARKETS_DOMAIN: Final[str] = "markets"


def market_quotes_schema(*, schema_version: str) -> pa.Schema:
    """Return the generic market quote dataset schema.

    Nullable fields and the reason absence is permitted:

    ``line_value``
        outright markets have no handicap or total;
    ``canonical_participant_id``
        event-scoped markets are not about one competitor;
    ``quoted_at_utc``
        the source published no original quote timestamp;
    ``quote_valid_from_utc`` / ``quote_valid_to_utc``
        the source supplied no validity window;
    ``source_market_id`` / ``source_selection_id`` / ``source_field``
        the source exposes no such identifier or column;
    ``quality_reason``
        no quality caveat applies.
    """
    return pa.schema(
        [
            pa.field("quote_series_id", pa.string(), nullable=False),
            pa.field("quote_observation_id", pa.string(), nullable=False),
            pa.field("canonical_event_id", pa.string(), nullable=False),
            pa.field("source_name", dictionary_string(), nullable=False),
            pa.field("source_event_id", pa.string(), nullable=False),
            pa.field("sport_code", dictionary_string(), nullable=False),
            pa.field("provider_type", dictionary_string(), nullable=False),
            pa.field("provider_id", dictionary_string(), nullable=False),
            pa.field("market_family", dictionary_string(), nullable=False),
            pa.field("market_key", dictionary_string(), nullable=False),
            pa.field("market_period", dictionary_string(), nullable=False),
            pa.field("participant_scope", dictionary_string(), nullable=False),
            pa.field("canonical_participant_id", pa.string(), nullable=True),
            pa.field("line_type", dictionary_string(), nullable=False),
            pa.field("line_value", line_decimal(), nullable=True),
            pa.field("outcome_key", dictionary_string(), nullable=False),
            pa.field("decimal_odds", price_decimal(), nullable=False),
            pa.field("quote_phase", dictionary_string(), nullable=False),
            pa.field("source_observed_at_utc", utc_timestamp(), nullable=False),
            pa.field("quoted_at_utc", utc_timestamp(), nullable=True),
            pa.field("quote_timestamp_precision", dictionary_string(), nullable=False),
            pa.field("quote_valid_from_utc", utc_timestamp(), nullable=True),
            pa.field("quote_valid_to_utc", utc_timestamp(), nullable=True),
            pa.field("market_status", dictionary_string(), nullable=False),
            pa.field("selection_status", dictionary_string(), nullable=False),
            pa.field("source_market_id", pa.string(), nullable=True),
            pa.field("source_selection_id", pa.string(), nullable=True),
            pa.field("source_field", pa.string(), nullable=True),
            pa.field("quality_status", dictionary_string(), nullable=False),
            pa.field("quality_reason", pa.string(), nullable=True),
            pa.field("source_file_sha256", pa.string(), nullable=False),
            pa.field("schema_version", dictionary_string(), nullable=False),
        ],
        metadata=dataset_metadata(
            dataset_name=DATASET_MARKET_QUOTES,
            schema_version=schema_version,
            domain=MARKETS_DOMAIN,
        ),
    )


def market_quote_rows(quotes: tuple[OddsQuote, ...]) -> list[dict[str, Any]]:
    """Build market quote rows in the caller's deterministic order."""
    rows: list[dict[str, Any]] = []
    for quote in quotes:
        selection = quote.selection
        definition = selection.definition
        rows.append(
            {
                "quote_series_id": quote.quote_series_id,
                "quote_observation_id": quote.quote_observation_id,
                "canonical_event_id": quote.canonical_event_id,
                "source_name": quote.source_name,
                "source_event_id": quote.source_event_id,
                "sport_code": definition.sport_code,
                "provider_type": quote.provider_type,
                "provider_id": quote.provider_id,
                "market_family": definition.market_family,
                "market_key": definition.market_key,
                "market_period": definition.market_period,
                "participant_scope": definition.participant_scope,
                "canonical_participant_id": definition.canonical_participant_id,
                "line_type": definition.line_type,
                "line_value": definition.line_value,
                "outcome_key": selection.outcome_key,
                "decimal_odds": quote.decimal_odds,
                "quote_phase": quote.quote_phase,
                "source_observed_at_utc": quote.source_observed_at_utc,
                "quoted_at_utc": quote.quoted_at_utc,
                "quote_timestamp_precision": quote.quote_timestamp_precision,
                "quote_valid_from_utc": quote.quote_valid_from_utc,
                "quote_valid_to_utc": quote.quote_valid_to_utc,
                "market_status": quote.market_status,
                "selection_status": quote.selection_status,
                "source_market_id": selection.source_market_id,
                "source_selection_id": selection.source_selection_id,
                "source_field": quote.source_field,
                "quality_status": quote.quality_status,
                "quality_reason": quote.quality_reason,
                "source_file_sha256": quote.source_file_sha256,
                "schema_version": quote.schema_version,
            }
        )
    return rows


def quote_sort_key(quote: OddsQuote) -> tuple[str, ...]:
    """Return the deterministic ordering key for market quotes."""
    definition = quote.selection.definition
    line = "" if definition.line_value is None else format(definition.line_value, "f")
    return (
        quote.canonical_event_id,
        definition.market_key,
        definition.market_period,
        definition.participant_scope,
        definition.line_type,
        line,
        quote.provider_type,
        quote.provider_id,
        quote.quote_phase,
        quote.selection.outcome_key,
        quote.quote_observation_id,
    )
