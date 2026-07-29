"""Strict provider-native inventory rows preserved before canonical mapping."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Final

import pyarrow as pa

from sports_analytics.data.codec import dumps_canonical_json
from sports_analytics.snapshots.arrow import dataset_metadata, dictionary_string, utc_timestamp
from sports_analytics.sources.bookmaker_contracts import (
    ProviderAcquisitionBundle,
    provider_native_markets,
)

DATASET_PROVIDER_NATIVE_EVENTS: Final[str] = "provider_native_events"
DATASET_PROVIDER_NATIVE_MARKETS: Final[str] = "provider_native_markets"
DATASET_PROVIDER_NATIVE_SELECTIONS: Final[str] = "provider_native_selections"
NATIVE_INVENTORY_SCHEMA_V1: Final[str] = "bookmaker-provider-native-inventory-v1"
_DOMAIN: Final[str] = "bookmakers"


def provider_native_events_schema(*, schema_version: str) -> pa.Schema:
    """Return the typed provider-native event inventory schema."""
    return pa.schema(
        [
            pa.field("provider_id", dictionary_string(), nullable=False),
            pa.field("sport", dictionary_string(), nullable=False),
            pa.field("source_event_id", pa.string(), nullable=False),
            pa.field("source_competition_id", pa.string(), nullable=False),
            pa.field("competition_display_name", pa.string(), nullable=True),
            pa.field("scheduled_start_utc", utc_timestamp(), nullable=False),
            pa.field("source_participant_ids", pa.string(), nullable=False),
            pa.field("participant_labels", pa.string(), nullable=False),
            pa.field("participant_roles", pa.string(), nullable=False),
            pa.field("pre_match_state", dictionary_string(), nullable=False),
            pa.field("source_page_route_id", pa.string(), nullable=False),
            pa.field("source_capture_ids", pa.string(), nullable=False),
            pa.field("provider_declared_market_references", pa.int32(), nullable=True),
            pa.field("market_groups_observed", pa.int32(), nullable=False),
            pa.field("markets_observed", pa.int32(), nullable=False),
            pa.field("markets_parsed", pa.int32(), nullable=False),
            pa.field("markets_rejected", pa.int32(), nullable=False),
            pa.field("selections_observed", pa.int32(), nullable=False),
            pa.field("selections_parsed", pa.int32(), nullable=False),
            pa.field("selections_rejected", pa.int32(), nullable=False),
            pa.field("markets_with_valid_price", pa.int32(), nullable=False),
            pa.field("source_responses_contributing", pa.int32(), nullable=False),
            pa.field("event_detail_surface_visited", pa.bool_(), nullable=False),
            pa.field("event_detail_readiness_reached", pa.bool_(), nullable=False),
            pa.field("truncated_response_count", pa.int32(), nullable=False),
            pa.field("bounded_response_rejection_count", pa.int32(), nullable=False),
            pa.field("missing_chunk_count", pa.int32(), nullable=False),
            pa.field("event_limit_truncated_count", pa.int32(), nullable=False),
            pa.field("reviewed_payload_completeness_permitted", pa.bool_(), nullable=False),
            pa.field("completeness_state", dictionary_string(), nullable=False),
            pa.field("schema_version", dictionary_string(), nullable=False),
        ],
        metadata=dataset_metadata(
            dataset_name=DATASET_PROVIDER_NATIVE_EVENTS,
            schema_version=schema_version,
            domain=_DOMAIN,
        ),
    )


def provider_native_markets_schema(*, schema_version: str) -> pa.Schema:
    """Return the typed provider-native market inventory schema."""
    return pa.schema(
        [
            pa.field("provider_id", dictionary_string(), nullable=False),
            pa.field("sport", dictionary_string(), nullable=False),
            pa.field("source_event_id", pa.string(), nullable=False),
            pa.field("source_market_id", pa.string(), nullable=False),
            pa.field("provider_market_type", pa.string(), nullable=True),
            pa.field("provider_market_name", pa.string(), nullable=False),
            pa.field("provider_market_group", pa.string(), nullable=True),
            pa.field("market_status", dictionary_string(), nullable=False),
            pa.field("period_identifier", pa.string(), nullable=True),
            pa.field("participant_scope", pa.string(), nullable=True),
            pa.field("source_participant_id", pa.string(), nullable=True),
            pa.field("market_line", pa.string(), nullable=True),
            pa.field("overtime_scope", pa.string(), nullable=True),
            pa.field("rules_scope", pa.string(), nullable=True),
            pa.field("provider_order", pa.int32(), nullable=True),
            pa.field("source_capture_id", pa.string(), nullable=True),
            pa.field("schema_version", dictionary_string(), nullable=False),
        ],
        metadata=dataset_metadata(
            dataset_name=DATASET_PROVIDER_NATIVE_MARKETS,
            schema_version=schema_version,
            domain=_DOMAIN,
        ),
    )


def provider_native_selections_schema(*, schema_version: str) -> pa.Schema:
    """Return the typed provider-native selection inventory schema."""
    return pa.schema(
        [
            pa.field("provider_id", dictionary_string(), nullable=False),
            pa.field("sport", dictionary_string(), nullable=False),
            pa.field("source_event_id", pa.string(), nullable=False),
            pa.field("source_market_id", pa.string(), nullable=False),
            pa.field("source_selection_id", pa.string(), nullable=False),
            pa.field("canonical_outcome_key", dictionary_string(), nullable=True),
            pa.field("provider_selection_type", pa.string(), nullable=True),
            pa.field("selection_label", pa.string(), nullable=False),
            pa.field("decimal_odds", pa.string(), nullable=True),
            pa.field("price_state", dictionary_string(), nullable=False),
            pa.field("selection_status", dictionary_string(), nullable=False),
            pa.field("selection_line", pa.string(), nullable=True),
            pa.field("source_participant_id", pa.string(), nullable=True),
            pa.field("provider_order", pa.int32(), nullable=True),
            pa.field("source_capture_id", pa.string(), nullable=True),
            pa.field("schema_version", dictionary_string(), nullable=False),
        ],
        metadata=dataset_metadata(
            dataset_name=DATASET_PROVIDER_NATIVE_SELECTIONS,
            schema_version=schema_version,
            domain=_DOMAIN,
        ),
    )


def provider_native_rows(
    bundle: ProviderAcquisitionBundle,
    *,
    schema_version: str,
) -> dict[str, list[dict[str, Any]]]:
    """Flatten a typed bundle without filtering unknown provider markets."""
    event_rows: list[dict[str, Any]] = []
    market_rows: list[dict[str, Any]] = []
    selection_rows: list[dict[str, Any]] = []
    ordered_events = sorted(
        bundle.events,
        key=lambda item: (item.scheduled_start_utc, item.source_event_id),
    )
    for event in ordered_events:
        if event.competition_display_name is not None and len(event.competition_display_name) > 200:
            msg = "provider-native competition display name exceeds the fixed bound"
            raise ValueError(msg)
        evidence = event.completeness
        participants = tuple(
            sorted(
                event.participants,
                key=lambda item: (item.role, item.source_participant_id),
            )
        )
        event_rows.append(
            {
                "provider_id": bundle.provider_id,
                "sport": bundle.sport,
                "source_event_id": event.source_event_id,
                "source_competition_id": event.source_competition_id,
                "competition_display_name": event.competition_display_name,
                "scheduled_start_utc": event.scheduled_start_utc,
                "source_participant_ids": dumps_canonical_json(
                    [item.source_participant_id for item in participants]
                ),
                "participant_labels": dumps_canonical_json(
                    [item.display_name for item in participants]
                ),
                "participant_roles": dumps_canonical_json([item.role for item in participants]),
                "pre_match_state": event.event_state.value,
                "source_page_route_id": event.source_page_route_id,
                "source_capture_ids": dumps_canonical_json(list(event.source_capture_ids)),
                "provider_declared_market_references": (
                    evidence.provider_declared_market_references
                ),
                "market_groups_observed": evidence.market_groups_observed,
                "markets_observed": evidence.markets_observed,
                "markets_parsed": evidence.markets_parsed,
                "markets_rejected": evidence.markets_rejected,
                "selections_observed": evidence.selections_observed,
                "selections_parsed": evidence.selections_parsed,
                "selections_rejected": evidence.selections_rejected,
                "markets_with_valid_price": evidence.markets_with_valid_price,
                "source_responses_contributing": evidence.source_responses_contributing,
                "event_detail_surface_visited": evidence.event_detail_surface_visited,
                "event_detail_readiness_reached": evidence.event_detail_readiness_reached,
                "truncated_response_count": evidence.truncated_response_count,
                "bounded_response_rejection_count": evidence.bounded_response_rejection_count,
                "missing_chunk_count": evidence.missing_chunk_count,
                "event_limit_truncated_count": evidence.event_limit_truncated_count,
                "reviewed_payload_completeness_permitted": (
                    evidence.reviewed_payload_completeness_permitted
                ),
                "completeness_state": evidence.completeness_state.value,
                "schema_version": schema_version,
            }
        )
        for market_index, market in enumerate(provider_native_markets(event)):
            market_order = market.provider_order
            if market_order is None:
                market_order = market_index
            market_rows.append(
                {
                    "provider_id": bundle.provider_id,
                    "sport": bundle.sport,
                    "source_event_id": event.source_event_id,
                    "source_market_id": market.source_market_id,
                    "provider_market_type": market.provider_market_type,
                    "provider_market_name": market.display_label,
                    "provider_market_group": market.provider_market_group,
                    "market_status": market.market_status.value,
                    "period_identifier": market.period,
                    "participant_scope": market.participant_scope,
                    "source_participant_id": market.source_participant_id,
                    "market_line": _decimal_text(market.line),
                    "overtime_scope": market.overtime_scope,
                    "rules_scope": market.rules_scope,
                    "provider_order": market_order,
                    "source_capture_id": market.source_capture_id,
                    "schema_version": schema_version,
                }
            )
            for selection_index, selection in enumerate(market.selections):
                selection_order = selection.provider_order
                if selection_order is None:
                    selection_order = selection_index
                selection_rows.append(
                    {
                        "provider_id": bundle.provider_id,
                        "sport": bundle.sport,
                        "source_event_id": event.source_event_id,
                        "source_market_id": market.source_market_id,
                        "source_selection_id": selection.source_selection_id,
                        "canonical_outcome_key": (
                            None
                            if selection.canonical_outcome_key is None
                            else selection.canonical_outcome_key.value
                        ),
                        "provider_selection_type": selection.provider_selection_type,
                        "selection_label": selection.display_label,
                        "decimal_odds": _decimal_text(selection.decimal_odds),
                        "price_state": selection.price_state.value,
                        "selection_status": selection.selection_status.value,
                        "selection_line": _decimal_text(selection.line),
                        "source_participant_id": selection.source_participant_id,
                        "provider_order": selection_order,
                        "source_capture_id": selection.source_capture_id,
                        "schema_version": schema_version,
                    }
                )
    market_rows.sort(
        key=lambda row: (
            str(row["source_event_id"]),
            int(row["provider_order"]),
            str(row["source_market_id"]),
        )
    )
    selection_rows.sort(
        key=lambda row: (
            str(row["source_event_id"]),
            str(row["source_market_id"]),
            int(row["provider_order"]),
            str(row["source_selection_id"]),
        )
    )
    return {
        DATASET_PROVIDER_NATIVE_EVENTS: event_rows,
        DATASET_PROVIDER_NATIVE_MARKETS: market_rows,
        DATASET_PROVIDER_NATIVE_SELECTIONS: selection_rows,
    }


def _decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")
