"""Generic betting market contracts shared by every sport and provider.

The canonical market representation is deliberately not limited to football 1X2.
It is built from a small set of validated, extensible dimensions:

``market_family``
    coarse grouping such as ``match-result``, ``totals``, ``handicap``;
``market_key``
    the canonical market type, unique per family/period/scope combination;
``market_period``
    the segment of play the market settles on (``full-match``, ``first-half``,
    ``set-1``, ``map-1``, ...);
``participant_scope``
    whether the market is about the event, one side, a specific team, or a
    player;
``line_type`` / ``line_value``
    handicap and total semantics, absent for outright markets;
``outcome_key``
    the specific selection (``home``, ``draw``, ``away``, ``over``, ``under``,
    ``yes``, ``no``, ...).

Market families and keys are validated extensible identifiers rather than a
closed enum, so a new bookmaker market only needs a new canonical string plus
the shared dimensions above. Structural rules (for example "a total requires a
line value") are enforced, which is what keeps the taxonomy safe.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Final

from sports_analytics.core.exceptions import NormalizationError
from sports_analytics.snapshots.arrow import LINE_DECIMAL_SCALE, PRICE_DECIMAL_SCALE
from sports_analytics.sports.contracts import require_utc, validate_domain_identifier

MIN_DECIMAL_ODDS: Final[Decimal] = Decimal("1.0001")
MAX_DECIMAL_ODDS: Final[Decimal] = Decimal("100000")
MAX_ABS_LINE_VALUE: Final[Decimal] = Decimal("999.99")
MAX_SOURCE_REFERENCE_LENGTH: Final[int] = 200


class ProviderType(StrEnum):
    """Kind of price provider behind a quote."""

    BOOKMAKER = "bookmaker"
    EXCHANGE = "exchange"
    SOURCE_MARKET_AVERAGE = "source-market-average"
    SOURCE_MARKET_MAXIMUM = "source-market-maximum"


class ParticipantScope(StrEnum):
    """Which competitor a market refers to."""

    EVENT = "event"
    HOME = "home"
    AWAY = "away"
    TEAM = "team"
    PLAYER = "player"


class LineType(StrEnum):
    """Handicap/total semantics of a market line."""

    NONE = "none"
    TOTAL = "total"
    HANDICAP = "handicap"
    SPREAD = "spread"


class QuotePhase(StrEnum):
    """When in the market lifecycle a price was captured."""

    OPENING = "opening"
    CLOSING = "closing"
    CURRENT = "current"
    UNKNOWN = "unknown"


class QuoteTimestampPrecision(StrEnum):
    """How precisely the original quote time is known.

    ``SNAPSHOT_OBSERVATION_ONLY`` means the source published no quote timestamp;
    only the application observation time is known, so the contract must not
    imply the price was available at that instant.
    """

    EXACT = "exact"
    MINUTE = "minute"
    SNAPSHOT_OBSERVATION_ONLY = "snapshot-observation-only"
    UNKNOWN = "unknown"


class MarketStatus(StrEnum):
    """Availability of a market as reported by the source."""

    UNKNOWN = "unknown"
    OPEN = "open"
    SUSPENDED = "suspended"
    CLOSED = "closed"
    SETTLED = "settled"


class SelectionStatus(StrEnum):
    """Availability of one selection as reported by the source."""

    UNKNOWN = "unknown"
    ACTIVE = "active"
    SUSPENDED = "suspended"
    REMOVED = "removed"


class QuoteQualityStatus(StrEnum):
    """Project assessment of quote trustworthiness."""

    SOURCE_PROVIDED = "source-provided"
    SOURCE_PROVIDED_AGGREGATE = "source-provided-aggregate"
    CAUTION = "caution"


# Documented known families. New families are permitted: they only need to be
# valid identifiers and satisfy the structural rules below. This registry exists
# for documentation, tests, and tooling, not as a closed enum.
KNOWN_MARKET_FAMILIES: Final[frozenset[str]] = frozenset(
    {
        "match-result",
        "double-chance",
        "draw-no-bet",
        "totals",
        "team-totals",
        "handicap",
        "asian-handicap",
        "spread",
        "both-teams-to-score",
        "correct-score",
        "winning-margin",
        "total-goals-odd-even",
        "result-and-total-goals",
        "result-and-btts",
        "double-chance-and-total-goals",
        "double-chance-and-btts",
        "result-or-total-goals",
        "result-or-btts",
        "btts-or-total-goals",
        "period-result",
        "corners",
        "corner-totals",
        "team-corner-totals",
        "corner-handicap",
        "team-most-corners",
        "first-half-corner-totals",
        "shots",
        "shots-on-target",
        "team-shots",
        "team-shots-on-target",
        "cards",
        "player-props",
        "scorer",
        "player-shots",
        "player-shots-on-target",
        "next-goal",
        "next-corner",
        "map-winner",
        "set-winner",
    }
)

KNOWN_MARKET_PERIODS: Final[frozenset[str]] = frozenset(
    {
        "full-match",
        "first-half",
        "second-half",
        "set-1",
        "set-2",
        "map-1",
        "map-2",
        "quarter-1",
        "regulation",
    }
)


def validate_decimal_odds(value: Decimal, *, field_name: str = "decimal_odds") -> Decimal:
    """Validate and quantize decimal odds to the shared price scale."""
    if not isinstance(value, Decimal):
        msg = f"{field_name} must be a Decimal"
        raise NormalizationError(msg)
    if not value.is_finite():
        msg = f"{field_name} must be finite"
        raise NormalizationError(msg)
    if value < MIN_DECIMAL_ODDS or value > MAX_DECIMAL_ODDS:
        msg = f"{field_name} must be between {MIN_DECIMAL_ODDS} and {MAX_DECIMAL_ODDS}"
        raise NormalizationError(msg)
    try:
        return value.quantize(Decimal(1).scaleb(-PRICE_DECIMAL_SCALE))
    except InvalidOperation as exc:
        msg = f"{field_name} cannot be represented at the canonical price scale"
        raise NormalizationError(msg) from exc


def validate_line_value(value: Decimal, *, field_name: str = "line_value") -> Decimal:
    """Validate and quantize a market line to the shared line scale."""
    if not isinstance(value, Decimal):
        msg = f"{field_name} must be a Decimal"
        raise NormalizationError(msg)
    if not value.is_finite():
        msg = f"{field_name} must be finite"
        raise NormalizationError(msg)
    if abs(value) > MAX_ABS_LINE_VALUE:
        msg = f"{field_name} magnitude must not exceed {MAX_ABS_LINE_VALUE}"
        raise NormalizationError(msg)
    try:
        return value.quantize(Decimal(1).scaleb(-LINE_DECIMAL_SCALE))
    except InvalidOperation as exc:
        msg = f"{field_name} cannot be represented at the canonical line scale"
        raise NormalizationError(msg) from exc


def _validate_optional_reference(value: str | None, *, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        msg = f"{field_name} must be a string or null"
        raise NormalizationError(msg)
    if value != value.strip() or not value:
        msg = f"{field_name} must be non-empty without surrounding whitespace"
        raise NormalizationError(msg)
    if len(value) > MAX_SOURCE_REFERENCE_LENGTH:
        msg = f"{field_name} exceeds maximum length of {MAX_SOURCE_REFERENCE_LENGTH}"
        raise NormalizationError(msg)
    return value


def _validate_enum(value: str, enum_type: type[StrEnum], *, field_name: str) -> str:
    try:
        return enum_type(value).value
    except ValueError as exc:
        allowed = ", ".join(member.value for member in enum_type)
        msg = f"{field_name} must be one of: {allowed}"
        raise NormalizationError(msg) from exc


@dataclass(frozen=True, slots=True)
class MarketDefinition:
    """A canonical market: what is being bet on, for which segment and scope."""

    sport_code: str
    market_family: str
    market_key: str
    market_period: str
    participant_scope: str
    line_type: str
    line_value: Decimal | None
    canonical_participant_id: str | None

    def __post_init__(self) -> None:
        validate_domain_identifier(self.sport_code, field_name="sport_code")
        validate_domain_identifier(self.market_family, field_name="market_family")
        validate_domain_identifier(self.market_key, field_name="market_key")
        validate_domain_identifier(self.market_period, field_name="market_period")
        _validate_enum(
            self.participant_scope,
            ParticipantScope,
            field_name="participant_scope",
        )
        _validate_enum(self.line_type, LineType, field_name="line_type")
        if self.line_type == LineType.NONE.value:
            if self.line_value is not None:
                msg = "line_value must be null when line_type is none"
                raise NormalizationError(msg)
        else:
            if self.line_value is None:
                msg = f"line_value is required when line_type is {self.line_type}"
                raise NormalizationError(msg)
            object.__setattr__(
                self,
                "line_value",
                validate_line_value(self.line_value),
            )
        scoped = {ParticipantScope.TEAM.value, ParticipantScope.PLAYER.value}
        if self.participant_scope in scoped and self.canonical_participant_id is None:
            msg = f"canonical_participant_id is required for {self.participant_scope} markets"
            raise NormalizationError(msg)
        if self.participant_scope == ParticipantScope.EVENT.value and (
            self.canonical_participant_id is not None
        ):
            msg = "event-scoped markets must not name a participant"
            raise NormalizationError(msg)


@dataclass(frozen=True, slots=True)
class MarketSelection:
    """One outcome of a canonical market, optionally carrying source identity."""

    definition: MarketDefinition
    outcome_key: str
    source_market_id: str | None = None
    source_selection_id: str | None = None

    def __post_init__(self) -> None:
        validate_domain_identifier(self.outcome_key, field_name="outcome_key")
        _validate_optional_reference(self.source_market_id, field_name="source_market_id")
        _validate_optional_reference(self.source_selection_id, field_name="source_selection_id")


@dataclass(frozen=True, slots=True)
class OddsQuote:
    """A priced market selection observed from one provider at one moment.

    Identity is split deliberately:

    ``quote_series_id``
        stable identity for the canonical event, selection, and provider;
    ``quote_observation_id``
        one concrete observation distinguished by source provenance and time.

    Temporal semantics are explicit and never conflated:

    ``source_observed_at_utc``
        when this application retrieved or observed the source data;
    ``quoted_at_utc``
        when the provider's price was published/valid, ``None`` when unknown;
    ``quote_timestamp_precision``
        how precisely ``quoted_at_utc`` is known;
    ``quote_phase``
        whether the price is an opening, closing, or currently observed price;
    ``quote_valid_from_utc`` / ``quote_valid_to_utc``
        source-supplied validity window, ``None`` when the source supplies none.
    """

    quote_series_id: str
    quote_observation_id: str
    canonical_event_id: str
    source_name: str
    source_event_id: str
    selection: MarketSelection
    provider_type: str
    provider_id: str
    decimal_odds: Decimal
    quote_phase: str
    source_observed_at_utc: datetime
    quoted_at_utc: datetime | None
    quote_timestamp_precision: str
    quote_valid_from_utc: datetime | None
    quote_valid_to_utc: datetime | None
    market_status: str
    selection_status: str
    source_field: str | None
    quality_status: str
    quality_reason: str | None
    source_file_sha256: str
    schema_version: str

    def __post_init__(self) -> None:
        validate_domain_identifier(self.source_name, field_name="source_name")
        validate_domain_identifier(self.provider_id, field_name="provider_id")
        _validate_enum(self.provider_type, ProviderType, field_name="provider_type")
        _validate_enum(self.quote_phase, QuotePhase, field_name="quote_phase")
        _validate_enum(
            self.quote_timestamp_precision,
            QuoteTimestampPrecision,
            field_name="quote_timestamp_precision",
        )
        _validate_enum(self.market_status, MarketStatus, field_name="market_status")
        _validate_enum(self.selection_status, SelectionStatus, field_name="selection_status")
        _validate_enum(self.quality_status, QuoteQualityStatus, field_name="quality_status")
        _validate_optional_reference(self.source_field, field_name="source_field")
        object.__setattr__(self, "decimal_odds", validate_decimal_odds(self.decimal_odds))
        object.__setattr__(
            self,
            "source_observed_at_utc",
            require_utc(self.source_observed_at_utc, field_name="source_observed_at_utc"),
        )
        if self.quoted_at_utc is not None:
            object.__setattr__(
                self,
                "quoted_at_utc",
                require_utc(self.quoted_at_utc, field_name="quoted_at_utc"),
            )
        precision = self.quote_timestamp_precision
        if self.quoted_at_utc is None and precision in {
            QuoteTimestampPrecision.EXACT.value,
            QuoteTimestampPrecision.MINUTE.value,
        }:
            msg = "quote_timestamp_precision claims a known quote time but quoted_at_utc is null"
            raise NormalizationError(msg)
        if (
            self.quoted_at_utc is not None
            and precision == QuoteTimestampPrecision.SNAPSHOT_OBSERVATION_ONLY.value
        ):
            msg = "observation-only precision must not be paired with a quote timestamp"
            raise NormalizationError(msg)
        if self.quote_valid_from_utc is not None:
            object.__setattr__(
                self,
                "quote_valid_from_utc",
                require_utc(self.quote_valid_from_utc, field_name="quote_valid_from_utc"),
            )
        if self.quote_valid_to_utc is not None:
            object.__setattr__(
                self,
                "quote_valid_to_utc",
                require_utc(self.quote_valid_to_utc, field_name="quote_valid_to_utc"),
            )
        if (
            self.quote_valid_from_utc is not None
            and self.quote_valid_to_utc is not None
            and self.quote_valid_to_utc < self.quote_valid_from_utc
        ):
            msg = "quote_valid_to_utc must not precede quote_valid_from_utc"
            raise NormalizationError(msg)
        if self.quality_status == QuoteQualityStatus.CAUTION.value and self.quality_reason is None:
            msg = "caution quality status requires an explicit reason"
            raise NormalizationError(msg)
