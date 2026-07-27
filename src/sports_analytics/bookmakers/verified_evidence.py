"""Verified bookmaker quote evidence derived from strict snapshot loading."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sports_analytics.bookmakers.canonical_mapping import (
    canonical_market_definition_id_from_row,
    overtime_scope_from_definition_id,
    rules_scope_from_definition_id,
)
from sports_analytics.bookmakers.priced_quote import BookmakerPricedQuote, quote_is_fresh_at
from sports_analytics.bookmakers.types import PROVIDER_BETANO_PT, PROVIDER_BETCLIC_PT
from sports_analytics.core.exceptions import SnapshotVerificationError
from sports_analytics.markets.contracts import validate_decimal_odds
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
    """Quote evidence verified against one loaded bookmaker snapshot."""

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


def build_verified_quote_from_loaded_row(
    *,
    loaded_snapshot_id: str,
    loaded_checksum_sha256: str,
    loaded_provider_id: str,
    loaded_sport: str,
    quote_row: dict[str, Any],
) -> VerifiedBookmakerQuote:
    """Construct one verified quote from a loader-validated snapshot row.

    This function is only called by the strict snapshot loader after verification.
    """
    identity = bookmaker_quote_identity_from_row(quote_row)
    if identity.provider_id != loaded_provider_id:
        msg = "quote provider_id does not match verified snapshot registration"
        raise SnapshotVerificationError(msg)
    sport = str(quote_row.get("sport_code", loaded_sport))
    if sport != loaded_sport:
        msg = "quote sport does not match verified snapshot registration"
        raise SnapshotVerificationError(msg)
    observed_raw = quote_row.get("source_observed_at_utc")
    if not isinstance(observed_raw, datetime):
        msg = "quote row requires source_observed_at_utc timestamp"
        raise SnapshotVerificationError(msg)
    canonical_market_definition_id = canonical_market_definition_id_from_row(quote_row)
    overtime_scope = overtime_scope_from_definition_id(canonical_market_definition_id)
    rules_scope = rules_scope_from_definition_id(canonical_market_definition_id)
    identity = BookmakerQuoteIdentity(
        canonical_event_id=identity.canonical_event_id,
        provider_id=identity.provider_id,
        market_key=identity.market_key,
        market_period=identity.market_period,
        participant_scope=identity.participant_scope,
        canonical_participant_id=identity.canonical_participant_id,
        line_type=identity.line_type,
        line_value=identity.line_value,
        outcome_key=identity.outcome_key,
        quote_phase=identity.quote_phase,
        quote_observation_id=identity.quote_observation_id,
        overtime_scope=overtime_scope,
        rules_scope=rules_scope,
    )
    outcome_key = str(quote_row.get("outcome_key", ""))
    return VerifiedBookmakerQuote(
        snapshot_id=loaded_snapshot_id,
        snapshot_checksum_sha256=loaded_checksum_sha256,
        provider_id=loaded_provider_id,
        sport=loaded_sport,
        identity=identity,
        decimal_odds=validate_decimal_odds(Decimal(str(quote_row["decimal_odds"]))),
        observed_at_utc=require_utc(observed_raw, field_name="source_observed_at_utc"),
        market_status=str(quote_row.get("market_status", "open")),
        selection_status=str(quote_row.get("selection_status", "active")),
        source_file_sha256=str(quote_row.get("source_file_sha256", "")),
        canonical_market_definition_id=canonical_market_definition_id,
        canonical_selection_id=outcome_key,
        source_event_id=str(quote_row.get("source_event_id", "")),
    )


def bookmaker_quote_identity_from_row(row: dict[str, Any]) -> BookmakerQuoteIdentity:
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
