"""Priced bookmaker quote contracts shared by selection and verified evidence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from sports_analytics.bookmakers.types import (
    PROVIDER_BETANO_PT,
    PROVIDER_BETCLIC_PT,
    QUOTE_EQUIVALENCE_POLICY_ID,
)
from sports_analytics.core.exceptions import PermanentSourceError
from sports_analytics.markets.contracts import validate_decimal_odds
from sports_analytics.sports.contracts import require_utc


@dataclass(frozen=True, slots=True)
class QuoteEquivalenceIdentity:
    """Verified identity for comparing two bookmaker quotes as the same bet."""

    canonical_event_id: str
    canonical_market_definition_id: str
    canonical_selection_id: str
    line: str | None = None
    period: str | None = None
    participant_scope: str | None = None
    overtime_scope: str | None = None
    rules_scope: str | None = None
    pre_match_state: str = "pre-match"
    comparison_policy_version: str = QUOTE_EQUIVALENCE_POLICY_ID


@dataclass(frozen=True, slots=True)
class BookmakerPricedQuote:
    """One priced selection from exactly one bookmaker provider."""

    provider_id: str
    decimal_odds: Decimal
    observed_at_utc: datetime
    canonical_event_id: str
    canonical_market_definition_id: str
    canonical_selection_id: str
    fresh: bool
    line: str | None = None
    period: str | None = None
    participant_scope: str | None = None
    overtime_scope: str | None = None
    rules_scope: str | None = None
    market_status: str = "open"
    selection_status: str = "active"
    snapshot_id: str | None = None
    snapshot_checksum_sha256: str | None = None

    def equivalence_identity(self) -> QuoteEquivalenceIdentity:
        return QuoteEquivalenceIdentity(
            canonical_event_id=self.canonical_event_id,
            canonical_market_definition_id=self.canonical_market_definition_id,
            canonical_selection_id=self.canonical_selection_id,
            line=self.line,
            period=self.period,
            participant_scope=self.participant_scope,
            overtime_scope=self.overtime_scope,
            rules_scope=self.rules_scope,
        )

    def __post_init__(self) -> None:
        object.__setattr__(self, "decimal_odds", validate_decimal_odds(self.decimal_odds))
        object.__setattr__(
            self,
            "observed_at_utc",
            require_utc(self.observed_at_utc, field_name="observed_at_utc"),
        )
        if self.provider_id not in {PROVIDER_BETANO_PT, PROVIDER_BETCLIC_PT}:
            msg = f"unsupported bookmaker provider_id: {self.provider_id}"
            raise PermanentSourceError(msg)
        if self.market_status != "open":
            msg = "only open markets may be selected for comparison"
            raise PermanentSourceError(msg)
        if self.selection_status != "active":
            msg = "only active selections may be selected for comparison"
            raise PermanentSourceError(msg)


def quote_is_fresh_at(
    quote: BookmakerPricedQuote,
    *,
    compared_at: datetime,
    maximum_age_seconds: int,
) -> bool:
    observed = require_utc(quote.observed_at_utc, field_name="observed_at_utc")
    current = require_utc(compared_at, field_name="compared_at")
    if observed > current:
        return False
    age = current - observed
    return timedelta(seconds=0) <= age <= timedelta(seconds=maximum_age_seconds)


def quote_is_fresh(
    quote: BookmakerPricedQuote,
    *,
    now: datetime,
    maximum_age_seconds: int,
) -> bool:
    """Return whether ``quote`` is within the configured freshness window."""
    return quote_is_fresh_at(
        quote,
        compared_at=now,
        maximum_age_seconds=maximum_age_seconds,
    )
