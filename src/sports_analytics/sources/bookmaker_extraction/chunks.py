"""Provider-neutral deterministic merge contract for reviewed market chunks."""

from __future__ import annotations

from dataclasses import dataclass

from sports_analytics.core.exceptions import ParserError
from sports_analytics.data.types import validate_identifier, validate_sha256_checksum
from sports_analytics.sources.bookmaker_contracts import ProviderMarketObservation


@dataclass(frozen=True, slots=True)
class BrowserObservedMarketChunk:
    """One parser-produced chunk linked to a browser-observed response."""

    source_event_id: str
    chunk_id: str
    sequence: int
    expected_chunk_count: int | None
    contributing_capture_checksum: str
    markets: tuple[ProviderMarketObservation, ...]

    def __post_init__(self) -> None:
        validate_identifier(self.source_event_id, field_name="source_event_id")
        validate_identifier(self.chunk_id, field_name="chunk_id")
        if isinstance(self.sequence, bool) or self.sequence < 0:
            msg = "chunk sequence must be a non-negative integer"
            raise ParserError(msg)
        if self.expected_chunk_count is not None and (
            isinstance(self.expected_chunk_count, bool)
            or self.expected_chunk_count < 1
            or self.sequence >= self.expected_chunk_count
        ):
            msg = "expected chunk count is inconsistent with sequence"
            raise ParserError(msg)
        object.__setattr__(
            self,
            "contributing_capture_checksum",
            validate_sha256_checksum(self.contributing_capture_checksum),
        )


@dataclass(frozen=True, slots=True)
class MergedBrowserObservedMarkets:
    """Deterministic result with an explicit denominator-based completeness flag."""

    source_event_id: str
    markets: tuple[ProviderMarketObservation, ...]
    contributing_capture_checksums: tuple[str, ...]
    duplicate_chunk_count: int
    missing_chunk_sequences: tuple[int, ...]
    complete_by_chunk_reference: bool


def merge_browser_observed_market_chunks(
    chunks: tuple[BrowserObservedMarketChunk, ...],
) -> MergedBrowserObservedMarkets:
    """Merge reviewed chunks; duplicate evidence is idempotent, conflicts fail."""
    if not chunks:
        msg = "market chunk merge requires at least one chunk"
        raise ParserError(msg)
    event_ids = {chunk.source_event_id for chunk in chunks}
    if len(event_ids) != 1:
        msg = "market chunks must belong to exactly one event"
        raise ParserError(msg)

    by_chunk_id: dict[str, BrowserObservedMarketChunk] = {}
    duplicate_count = 0
    for chunk in chunks:
        existing = by_chunk_id.get(chunk.chunk_id)
        if existing is None:
            by_chunk_id[chunk.chunk_id] = chunk
            continue
        if existing != chunk:
            msg = "duplicate chunk identity carries conflicting evidence"
            raise ParserError(msg)
        duplicate_count += 1

    unique_chunks = tuple(
        sorted(
            by_chunk_id.values(),
            key=lambda item: (item.sequence, item.chunk_id),
        )
    )
    expected_values = {
        item.expected_chunk_count for item in unique_chunks if item.expected_chunk_count is not None
    }
    if len(expected_values) > 1:
        msg = "market chunks disagree on expected chunk count"
        raise ParserError(msg)
    expected_count = next(iter(expected_values), None)
    observed_sequences = {item.sequence for item in unique_chunks}
    missing = (
        tuple(index for index in range(expected_count) if index not in observed_sequences)
        if expected_count is not None
        else ()
    )

    markets_by_id: dict[str, ProviderMarketObservation] = {}
    for chunk in unique_chunks:
        for market in chunk.markets:
            existing_market = markets_by_id.get(market.source_market_id)
            if existing_market is not None and existing_market != market:
                msg = "duplicate market identity carries conflicting chunk evidence"
                raise ParserError(msg)
            markets_by_id[market.source_market_id] = market
    ordered_markets = tuple(
        sorted(
            markets_by_id.values(),
            key=lambda item: (
                item.provider_order if item.provider_order is not None else 2**31,
                item.source_market_id,
            ),
        )
    )
    complete = (
        expected_count is not None and not missing and len(observed_sequences) == expected_count
    )
    return MergedBrowserObservedMarkets(
        source_event_id=next(iter(event_ids)),
        markets=ordered_markets,
        contributing_capture_checksums=tuple(
            sorted({item.contributing_capture_checksum for item in unique_chunks})
        ),
        duplicate_chunk_count=duplicate_count,
        missing_chunk_sequences=missing,
        complete_by_chunk_reference=complete,
    )
