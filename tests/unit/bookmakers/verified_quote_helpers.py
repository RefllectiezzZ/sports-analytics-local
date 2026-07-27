"""Test helpers for verified bookmaker quote evidence."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from sports_analytics.bookmakers.verified_evidence import (
    BookmakerQuoteIdentity,
    VerifiedBookmakerQuote,
    VerifiedQuoteCatalogue,
)


def verified_quote(
    *,
    provider_id: str,
    odds: str,
    leg_key: str = "a",
    age_seconds: int = 0,
    observed_at: datetime,
    snapshot_id: str = "snap-1",
    snapshot_checksum_sha256: str = "a" * 64,
    selection_id: str = "home",
    market_key: str = "football.match-result.1x2.full-match",
    canonical_market_definition_id: str = "football-match-result-1x2",
    line_type: str = "none",
    line_value: Decimal | None = None,
    source_file_sha256: str = "b" * 64,
) -> VerifiedBookmakerQuote:
    observed = observed_at - timedelta(seconds=age_seconds)
    identity = BookmakerQuoteIdentity(
        canonical_event_id=f"event-{leg_key}",
        provider_id=provider_id,
        market_key=market_key,
        market_period="full-match",
        participant_scope="event",
        canonical_participant_id=None,
        line_type=line_type,
        line_value=line_value,
        outcome_key=selection_id,
        quote_phase="current",
        quote_observation_id=f"obs-{provider_id}-{leg_key}-{selection_id}",
        overtime_scope=None,
        rules_scope="regulation-only",
    )
    return VerifiedBookmakerQuote(
        snapshot_id=snapshot_id,
        snapshot_checksum_sha256=snapshot_checksum_sha256,
        provider_id=provider_id,
        sport="football",
        identity=identity,
        decimal_odds=Decimal(odds),
        observed_at_utc=observed,
        market_status="open",
        selection_status="active",
        source_file_sha256=source_file_sha256,
        canonical_market_definition_id=canonical_market_definition_id,
        canonical_selection_id=selection_id,
        source_event_id=f"source-{leg_key}",
        comparable=True,
    )


def catalogue_for(*quotes: VerifiedBookmakerQuote) -> VerifiedQuoteCatalogue:
    """Build a synthetic loader-style catalogue from exact quote objects."""
    if not quotes:
        msg = "catalogue_for requires at least one quote"
        raise ValueError(msg)
    first = quotes[0]
    return VerifiedQuoteCatalogue(
        snapshot_id=first.snapshot_id,
        snapshot_checksum_sha256=first.snapshot_checksum_sha256,
        provider_id=first.provider_id,
        sport=first.sport,
        quotes_by_observation_id=tuple(
            (quote.identity.quote_observation_id, quote) for quote in quotes
        ),
        quotes_by_semantic_identity=(),
    )


def empty_catalogue(
    *,
    provider_id: str,
    sport: str = "football",
    snapshot_id: str = "snap-empty",
    snapshot_checksum_sha256: str = "c" * 64,
) -> VerifiedQuoteCatalogue:
    """Empty catalogue for a provider side with no quotes in a comparison."""
    return VerifiedQuoteCatalogue(
        snapshot_id=snapshot_id,
        snapshot_checksum_sha256=snapshot_checksum_sha256,
        provider_id=provider_id,
        sport=sport,
        quotes_by_observation_id=(),
        quotes_by_semantic_identity=(),
    )
