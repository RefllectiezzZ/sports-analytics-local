"""Verified bookmaker quote evidence derived from strict snapshot loading."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sports_analytics.bookmakers.priced_quote import BookmakerPricedQuote, quote_is_fresh_at
from sports_analytics.bookmakers.types import PROVIDER_BETANO_PT, PROVIDER_BETCLIC_PT
from sports_analytics.core.exceptions import PermanentSourceError, SnapshotVerificationError
from sports_analytics.sports.contracts import require_utc


@dataclass(frozen=True, slots=True)
class BookmakerQuoteIdentity:
    """Persisted semantic identity for one bookmaker quote observation."""

    canonical_event_id: str
    provider_id: str
    market_key: str
    market_period: str
    participant_scope: str
    canonical_participant_id: str | None
    line_type: str
    line_value: Decimal | None
    outcome_key: str
    quote_phase: str
    quote_observation_id: str
    overtime_scope: str | None = None
    rules_scope: str | None = None


@dataclass(frozen=True, slots=True)
class VerifiedBookmakerQuote:
    """Quote evidence verified against one loaded bookmaker snapshot.

    Instances are produced only by the strict snapshot loader. Production
    selection and multiples consume loader catalogue entries.
    """

    snapshot_id: str
    snapshot_checksum_sha256: str
    provider_id: str
    sport: str
    identity: BookmakerQuoteIdentity
    decimal_odds: Decimal
    observed_at_utc: datetime
    market_status: str
    selection_status: str
    source_file_sha256: str
    canonical_market_definition_id: str
    canonical_selection_id: str
    source_event_id: str
    comparable: bool = True

    def is_fresh_at(self, *, evaluated_at_utc: datetime, maximum_age_seconds: int) -> bool:
        """Recalculate freshness at an explicit evaluation timestamp."""
        evaluated = require_utc(evaluated_at_utc, field_name="evaluated_at_utc")
        priced = self.to_priced_quote(
            evaluated_at_utc=evaluated,
            maximum_age_seconds=maximum_age_seconds,
        )
        return quote_is_fresh_at(
            priced,
            compared_at=evaluated,
            maximum_age_seconds=maximum_age_seconds,
        )

    def to_priced_quote(
        self,
        *,
        evaluated_at_utc: datetime,
        maximum_age_seconds: int,
    ) -> BookmakerPricedQuote:
        """Materialize a priced quote with policy-evaluated freshness."""
        evaluated = require_utc(evaluated_at_utc, field_name="evaluated_at_utc")
        observed = require_utc(self.observed_at_utc, field_name="observed_at_utc")
        if observed > evaluated:
            fresh = False
        else:
            age = evaluated - observed
            fresh = timedelta(seconds=0) <= age <= timedelta(seconds=maximum_age_seconds)
        line = None if self.identity.line_value is None else format(self.identity.line_value, "f")
        return BookmakerPricedQuote(
            provider_id=self.provider_id,
            decimal_odds=self.decimal_odds,
            observed_at_utc=observed,
            canonical_event_id=self.identity.canonical_event_id,
            canonical_market_definition_id=self.canonical_market_definition_id,
            canonical_selection_id=self.canonical_selection_id,
            fresh=fresh,
            line=line,
            period=self.identity.market_period,
            participant_scope=self.identity.participant_scope,
            overtime_scope=self.identity.overtime_scope,
            rules_scope=self.identity.rules_scope,
            market_status=self.market_status,
            selection_status=self.selection_status,
            snapshot_id=self.snapshot_id,
            snapshot_checksum_sha256=self.snapshot_checksum_sha256,
        )

    def comparison_identity(self) -> tuple[object, ...]:
        """Full cross-provider comparable bet identity."""
        identity = self.identity
        line = None if identity.line_value is None else format(identity.line_value, "f")
        return (
            identity.canonical_event_id,
            self.canonical_market_definition_id,
            self.canonical_selection_id,
            line,
            identity.market_period,
            identity.participant_scope,
            identity.canonical_participant_id,
            identity.overtime_scope,
            identity.rules_scope,
            "pre-match",
        )


@dataclass(frozen=True, slots=True)
class VerifiedQuoteCatalogue:
    """Immutable verified quote catalogue produced by the strict loader."""

    snapshot_id: str
    snapshot_checksum_sha256: str
    provider_id: str
    sport: str
    quotes_by_observation_id: tuple[tuple[str, VerifiedBookmakerQuote], ...]
    quotes_by_semantic_identity: tuple[tuple[tuple[object, ...], VerifiedBookmakerQuote], ...]

    def get(self, quote_observation_id: str) -> VerifiedBookmakerQuote | None:
        for observation_id, quote in self.quotes_by_observation_id:
            if observation_id == quote_observation_id:
                return quote
        return None

    def all_quotes(self) -> tuple[VerifiedBookmakerQuote, ...]:
        return tuple(quote for _, quote in self.quotes_by_observation_id)


def bookmaker_quote_identity_from_row(
    row: dict[str, Any],
    *,
    overtime_scope: str | None = None,
    rules_scope: str | None = None,
) -> BookmakerQuoteIdentity:
    """Build the persisted quote identity tuple from one market quote row."""
    line_value_raw = row.get("line_value")
    line_value = None if line_value_raw is None else Decimal(str(line_value_raw))
    participant_raw = row.get("canonical_participant_id")
    return BookmakerQuoteIdentity(
        canonical_event_id=str(row["canonical_event_id"]),
        provider_id=str(row["provider_id"]),
        market_key=str(row["market_key"]),
        market_period=str(row["market_period"]),
        participant_scope=str(row["participant_scope"]),
        canonical_participant_id=None if participant_raw is None else str(participant_raw),
        line_type=str(row["line_type"]),
        line_value=line_value,
        outcome_key=str(row["outcome_key"]),
        quote_phase=str(row["quote_phase"]),
        quote_observation_id=str(row["quote_observation_id"]),
        overtime_scope=overtime_scope,
        rules_scope=rules_scope,
    )


def quote_semantic_identity_key(identity: BookmakerQuoteIdentity) -> tuple[object, ...]:
    """Return the semantic identity key excluding ``quote_observation_id``."""
    return (
        identity.canonical_event_id,
        identity.provider_id,
        identity.market_key,
        identity.market_period,
        identity.participant_scope,
        identity.canonical_participant_id,
        identity.line_type,
        identity.line_value,
        identity.outcome_key,
        identity.quote_phase,
        identity.overtime_scope,
        identity.rules_scope,
    )


def verify_quote_row_identity(row: dict[str, Any]) -> BookmakerQuoteIdentity:
    """Validate required quote identity columns are present."""
    required = (
        "canonical_event_id",
        "provider_id",
        "market_key",
        "market_period",
        "participant_scope",
        "line_type",
        "outcome_key",
        "quote_phase",
        "quote_observation_id",
    )
    for field in required:
        if field not in row or row[field] is None:
            raise SnapshotVerificationError(f"quote row missing required field: {field}")
    provider_id = str(row["provider_id"])
    if provider_id not in {PROVIDER_BETANO_PT, PROVIDER_BETCLIC_PT}:
        raise SnapshotVerificationError(f"unsupported quote provider_id: {provider_id}")
    return bookmaker_quote_identity_from_row(row)


def require_catalogue_quote(
    quote: VerifiedBookmakerQuote,
    *,
    catalogue: VerifiedQuoteCatalogue,
) -> VerifiedBookmakerQuote:
    """Reject quotes that are not members of a loader-produced catalogue."""
    found = catalogue.get(quote.identity.quote_observation_id)
    if found is None or found is not quote:
        msg = "quote is not admitted from the verified loader catalogue"
        raise PermanentSourceError(msg)
    if (
        quote.snapshot_id != catalogue.snapshot_id
        or quote.snapshot_checksum_sha256 != catalogue.snapshot_checksum_sha256
    ):
        msg = "quote snapshot metadata does not match verified catalogue"
        raise PermanentSourceError(msg)
    return quote


def leg_identity_from_verified_quote(quote: VerifiedBookmakerQuote) -> tuple[object, ...]:
    """Return the full multiple-leg identity dimensions for one verified quote."""
    identity = quote.identity
    return (
        quote.provider_id,
        identity.canonical_event_id,
        quote.canonical_market_definition_id,
        quote.canonical_selection_id,
        identity.canonical_participant_id,
        identity.line_type,
        identity.line_value,
        identity.market_period,
        identity.participant_scope,
        identity.overtime_scope,
        identity.rules_scope,
        identity.quote_observation_id,
        quote.snapshot_id,
        quote.snapshot_checksum_sha256,
    )
