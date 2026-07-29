"""Provider-native inventory structural depth and completeness tests."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from sports_analytics.bookmakers.native_inventory import (
    DATASET_PROVIDER_NATIVE_MARKETS,
    DATASET_PROVIDER_NATIVE_SELECTIONS,
    provider_native_rows,
    provider_native_selections_schema,
)
from sports_analytics.bookmakers.types import BOOKMAKER_SCHEMA_VERSION_V2
from sports_analytics.core.exceptions import NormalizationError
from sports_analytics.markets.contracts import MarketStatus, SelectionStatus
from sports_analytics.sources.bookmaker_contracts import (
    CompletenessState,
    EventCompletenessEvidence,
    ProviderAcquisitionBundle,
    ProviderEventObservation,
    ProviderEventState,
    ProviderMarketObservation,
    ProviderParticipantObservation,
    ProviderSelectionObservation,
    ProviderSelectionPriceState,
)

OBSERVED = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
CAPTURE = "a" * 64


def test_fifty_unknown_markets_and_150_selections_survive_native_flattening() -> None:
    markets = tuple(
        ProviderMarketObservation(
            source_market_id=f"market-{market_index:03d}",
            display_label=f"Synthetic Market {market_index:03d}",
            market_status=MarketStatus.OPEN,
            selections=tuple(
                ProviderSelectionObservation(
                    source_selection_id=f"selection-{market_index:03d}-{selection_index}",
                    display_label=f"Synthetic Selection {selection_index}",
                    decimal_odds=Decimal("2.000"),
                    selection_status=SelectionStatus.ACTIVE,
                    line=Decimal(f"{market_index}.{selection_index}"),
                    provider_selection_type=f"native-selection-{selection_index}",
                    provider_order=selection_index,
                    source_capture_id=CAPTURE,
                )
                for selection_index in range(3)
            ),
            period=f"native-period-{market_index % 4}",
            line=Decimal(f"{market_index}.5"),
            provider_market_type=f"native-market-{market_index:03d}",
            provider_market_group=f"group-{market_index % 5}",
            provider_order=market_index,
            source_capture_id=CAPTURE,
        )
        for market_index in range(50)
    )
    event = ProviderEventObservation(
        source_event_id="event-1",
        source_competition_id="competition-1",
        sport="football",
        scheduled_start_utc=OBSERVED,
        event_state=ProviderEventState.PRE_MATCH,
        participants=(
            ProviderParticipantObservation("participant-1", "Synthetic One", "home"),
            ProviderParticipantObservation("participant-2", "Synthetic Two", "away"),
        ),
        markets=(),
        native_markets=markets,
        source_page_route_id="event-detail-1",
        source_capture_ids=(CAPTURE,),
        completeness=EventCompletenessEvidence(
            provider_declared_market_references=50,
            market_groups_observed=5,
            markets_observed=50,
            markets_parsed=50,
            selections_observed=150,
            selections_parsed=150,
            markets_with_valid_price=50,
            source_responses_contributing=1,
            event_detail_surface_visited=True,
            event_detail_readiness_reached=True,
            completeness_state=CompletenessState.COMPLETE_BY_PROVIDER_REFERENCE,
        ),
    )
    bundle = ProviderAcquisitionBundle(
        provider_id="betano-pt",
        adapter_version="adapter-v2",
        acquisition_cycle_id="cycle-1",
        observed_at_utc=OBSERVED,
        sport="football",
        events=(event,),
        warnings=(),
        drift_codes=("unknown-market",),
        provenance=("capture:a",),
    )
    rows = provider_native_rows(bundle, schema_version=BOOKMAKER_SCHEMA_VERSION_V2)
    assert len(rows[DATASET_PROVIDER_NATIVE_MARKETS]) == 50
    assert len(rows[DATASET_PROVIDER_NATIVE_SELECTIONS]) == 150
    assert {row["provider_market_type"] for row in rows[DATASET_PROVIDER_NATIVE_MARKETS]} == {
        f"native-market-{index:03d}" for index in range(50)
    }
    assert rows[DATASET_PROVIDER_NATIVE_SELECTIONS][0]["decimal_odds"] == "2.0000"


def test_unpriced_selection_schema_and_contract_are_strict() -> None:
    selection = ProviderSelectionObservation(
        source_selection_id="selection-unpriced",
        display_label="Suspended selection",
        decimal_odds=None,
        selection_status=SelectionStatus.SUSPENDED,
        price_state=ProviderSelectionPriceState.UNPRICED,
        source_capture_id=CAPTURE,
    )
    market = ProviderMarketObservation(
        source_market_id="market-unpriced",
        display_label="Suspended market",
        market_status=MarketStatus.SUSPENDED,
        selections=(selection,),
        source_capture_id=CAPTURE,
    )
    event = ProviderEventObservation(
        source_event_id="event-unpriced",
        source_competition_id="competition-1",
        sport="football",
        scheduled_start_utc=OBSERVED,
        event_state=ProviderEventState.PRE_MATCH,
        participants=(
            ProviderParticipantObservation("participant-1", "Synthetic One", "home"),
            ProviderParticipantObservation("participant-2", "Synthetic Two", "away"),
        ),
        markets=(),
        native_markets=(market,),
        source_page_route_id="event-detail-1",
        source_capture_ids=(CAPTURE,),
    )
    bundle = ProviderAcquisitionBundle(
        provider_id="betano-pt",
        adapter_version="adapter-v2",
        acquisition_cycle_id="cycle-unpriced",
        observed_at_utc=OBSERVED,
        sport="football",
        events=(event,),
        warnings=(),
        drift_codes=(),
        provenance=(),
    )
    rows = provider_native_rows(bundle, schema_version=BOOKMAKER_SCHEMA_VERSION_V2)
    assert rows[DATASET_PROVIDER_NATIVE_SELECTIONS][0]["decimal_odds"] is None
    assert rows[DATASET_PROVIDER_NATIVE_SELECTIONS][0]["price_state"] == "unpriced"
    schema = provider_native_selections_schema(schema_version=BOOKMAKER_SCHEMA_VERSION_V2)
    assert schema.field("decimal_odds").nullable
    assert not schema.field("price_state").nullable

    with pytest.raises(NormalizationError, match="priced selection requires"):
        ProviderSelectionObservation(
            source_selection_id="contradiction-priced",
            display_label="Contradiction",
            decimal_odds=None,
            selection_status=SelectionStatus.ACTIVE,
            price_state=ProviderSelectionPriceState.PRICED,
        )
    with pytest.raises(NormalizationError, match="unpriced selection requires"):
        ProviderSelectionObservation(
            source_selection_id="contradiction-unpriced",
            display_label="Contradiction",
            decimal_odds=Decimal("2.00"),
            selection_status=SelectionStatus.SUSPENDED,
            price_state=ProviderSelectionPriceState.UNPRICED,
        )
