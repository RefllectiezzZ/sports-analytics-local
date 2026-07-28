"""Deterministic browser-observed market chunk merge tests."""

from __future__ import annotations

from decimal import Decimal

from sports_analytics.markets.contracts import MarketStatus, SelectionStatus
from sports_analytics.sources.bookmaker_contracts import (
    ProviderMarketObservation,
    ProviderSelectionObservation,
)
from sports_analytics.sources.bookmaker_extraction.chunks import (
    BrowserObservedMarketChunk,
    merge_browser_observed_market_chunks,
)


def _market(identity: str, order: int) -> ProviderMarketObservation:
    return ProviderMarketObservation(
        source_market_id=identity,
        display_label=f"Synthetic {identity}",
        market_status=MarketStatus.OPEN,
        selections=(
            ProviderSelectionObservation(
                source_selection_id=f"{identity}-selection",
                display_label="Synthetic selection",
                decimal_odds=Decimal("2.125"),
                selection_status=SelectionStatus.ACTIVE,
            ),
        ),
        provider_order=order,
    )


def _chunk(
    identity: str,
    sequence: int,
    markets: tuple[ProviderMarketObservation, ...],
    *,
    expected: int = 2,
) -> BrowserObservedMarketChunk:
    checksum = f"{sequence + 1:064x}"
    return BrowserObservedMarketChunk(
        source_event_id="synthetic-event",
        chunk_id=identity,
        sequence=sequence,
        expected_chunk_count=expected,
        contributing_capture_checksum=checksum,
        markets=markets,
    )


def test_multiple_chunks_merge_deterministically_and_duplicates_are_idempotent() -> None:
    first = _chunk("chunk-0", 0, (_market("market-b", 2),))
    second = _chunk("chunk-1", 1, (_market("market-a", 1),))
    merged = merge_browser_observed_market_chunks((second, first, first))
    assert [market.source_market_id for market in merged.markets] == [
        "market-a",
        "market-b",
    ]
    assert merged.duplicate_chunk_count == 1
    assert merged.missing_chunk_sequences == ()
    assert merged.complete_by_chunk_reference


def test_missing_chunk_prevents_complete_classification() -> None:
    merged = merge_browser_observed_market_chunks(
        (_chunk("chunk-0", 0, (_market("market-a", 1),)),)
    )
    assert merged.missing_chunk_sequences == (1,)
    assert not merged.complete_by_chunk_reference
