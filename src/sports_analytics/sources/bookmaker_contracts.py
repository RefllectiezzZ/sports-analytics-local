"""Strict raw provider observation contracts for bookmaker acquisition."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Final

from sports_analytics.core.exceptions import NormalizationError, PermanentSourceError
from sports_analytics.data.types import validate_identifier
from sports_analytics.markets.contracts import (
    MarketStatus,
    SelectionStatus,
    validate_decimal_odds,
)
from sports_analytics.sports.contracts import require_utc

MAX_LABEL_LENGTH: Final[int] = 200
MAX_PROVIDER_CODE_LENGTH: Final[int] = 100
MAX_CATEGORY_LENGTH: Final[int] = 160
MAX_EVIDENCE_REFERENCES: Final[int] = 256
MIN_DECIMAL_ODDS_EXCLUSIVE: Final[Decimal] = Decimal("1")


class ProviderEventState(StrEnum):
    """Pre-match / live state as observed from the provider."""

    PRE_MATCH = "pre-match"
    LIVE = "live"
    UNKNOWN = "unknown"


class ProviderSelectionPriceState(StrEnum):
    """Whether a provider-native selection currently carries a valid price."""

    PRICED = "priced"
    UNPRICED = "unpriced"


class CanonicalOutcomeKey(StrEnum):
    """Closed canonical outcome semantics assignable only by reviewed parsers."""

    HOME = "home"
    DRAW = "draw"
    AWAY = "away"
    OVER = "over"
    UNDER = "under"
    YES = "yes"
    NO = "no"


class ParserDriftSeverity(StrEnum):
    """How severely a parser observation diverges from the expected schema."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class CompletenessState(StrEnum):
    """Evidence-backed exhaustive-capture classification for one event."""

    COMPLETE_BY_PROVIDER_REFERENCE = "complete-by-provider-reference"
    COMPLETE_BY_REVIEWED_EVENT_PAYLOAD = "complete-by-reviewed-event-payload"
    PARTIAL_DEADLINE = "partial-deadline"
    PARTIAL_TRUNCATED_RESPONSE = "partial-truncated-response"
    PARTIAL_NAVIGATION_FAILURE = "partial-navigation-failure"
    PARTIAL_PARSER_REJECTION = "partial-parser-rejection"
    PARTIAL_EVENT_LIMIT = "partial-event-limit"
    UNKNOWN = "unknown-completeness"


@dataclass(frozen=True, slots=True)
class EventCompletenessEvidence:
    """Bounded structural counts proving or limiting event-market completeness."""

    provider_declared_market_references: int | None = None
    market_groups_observed: int = 0
    markets_observed: int = 0
    markets_parsed: int = 0
    markets_rejected: int = 0
    selections_observed: int = 0
    selections_parsed: int = 0
    selections_rejected: int = 0
    markets_with_valid_price: int = 0
    source_responses_contributing: int = 0
    event_detail_surface_visited: bool = False
    event_detail_readiness_reached: bool = False
    truncated_response_count: int = 0
    bounded_response_rejection_count: int = 0
    missing_chunk_count: int = 0
    event_limit_truncated_count: int = 0
    reviewed_payload_completeness_permitted: bool = False
    completeness_state: CompletenessState = CompletenessState.UNKNOWN

    def __post_init__(self) -> None:
        for name in (
            "market_groups_observed",
            "markets_observed",
            "markets_parsed",
            "markets_rejected",
            "selections_observed",
            "selections_parsed",
            "selections_rejected",
            "markets_with_valid_price",
            "source_responses_contributing",
            "truncated_response_count",
            "bounded_response_rejection_count",
            "missing_chunk_count",
            "event_limit_truncated_count",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 0 or value > 1_000_000:
                msg = f"{name} must be a bounded non-negative int"
                raise PermanentSourceError(msg)
        declared = self.provider_declared_market_references
        if declared is not None and (
            type(declared) is not int or declared < 0 or declared > 1_000_000
        ):
            msg = "provider_declared_market_references must be a bounded non-negative int"
            raise PermanentSourceError(msg)
        if (
            type(self.event_detail_surface_visited) is not bool
            or type(self.event_detail_readiness_reached) is not bool
            or type(self.reviewed_payload_completeness_permitted) is not bool
        ):
            msg = "event-detail evidence flags must be booleans"
            raise PermanentSourceError(msg)
        if self.markets_parsed + self.markets_rejected > self.markets_observed:
            msg = "market parsed/rejected counts exceed markets observed"
            raise PermanentSourceError(msg)
        if self.selections_parsed + self.selections_rejected > self.selections_observed:
            msg = "selection parsed/rejected counts exceed selections observed"
            raise PermanentSourceError(msg)
        if self.markets_with_valid_price > self.markets_parsed:
            msg = "priced market count exceeds parsed market count"
            raise PermanentSourceError(msg)
        if self.event_detail_readiness_reached and not self.event_detail_surface_visited:
            msg = "event-detail readiness requires a visited detail surface"
            raise PermanentSourceError(msg)
        if self.completeness_state in {
            CompletenessState.COMPLETE_BY_PROVIDER_REFERENCE,
            CompletenessState.COMPLETE_BY_REVIEWED_EVENT_PAYLOAD,
        }:
            if not (
                self.event_detail_surface_visited
                and self.event_detail_readiness_reached
                and self.truncated_response_count == 0
                and self.bounded_response_rejection_count == 0
                and self.missing_chunk_count == 0
                and self.event_limit_truncated_count == 0
                and self.markets_rejected == 0
                and self.selections_rejected == 0
                and self.markets_parsed == self.markets_observed
                and self.selections_parsed == self.selections_observed
            ):
                msg = "complete state requires clean event-detail evidence"
                raise PermanentSourceError(msg)
        if self.completeness_state is CompletenessState.COMPLETE_BY_PROVIDER_REFERENCE:
            if (
                declared is None
                or declared != self.markets_observed
                or declared != self.markets_parsed
            ):
                msg = "provider-reference completeness requires an exact market denominator"
                raise PermanentSourceError(msg)
        if (
            self.completeness_state is CompletenessState.COMPLETE_BY_REVIEWED_EVENT_PAYLOAD
            and not self.reviewed_payload_completeness_permitted
        ):
            msg = "reviewed-payload completeness requires explicit profile permission"
            raise PermanentSourceError(msg)


@dataclass(frozen=True, slots=True)
class ProviderParserWarning:
    """Auditable parser warning or drift note."""

    code: str
    message: str
    severity: ParserDriftSeverity
    source_path: str | None = None

    def __post_init__(self) -> None:
        validate_identifier(self.code, field_name="warning_code")
        if not self.message or len(self.message) > 500:
            msg = "warning message must be non-empty and bounded"
            raise PermanentSourceError(msg)


@dataclass(frozen=True, slots=True)
class ProviderParticipantObservation:
    """Source-scoped participant identity from one provider observation."""

    source_participant_id: str
    display_name: str
    role: str
    normalized_name: str | None = None

    def __post_init__(self) -> None:
        if not self.source_participant_id.strip():
            msg = "missing participant identity"
            raise NormalizationError(msg)
        if not self.display_name.strip():
            msg = "participant display_name must be non-empty"
            raise NormalizationError(msg)
        validate_identifier(self.role, field_name="participant_role")


@dataclass(frozen=True, slots=True)
class ProviderSelectionObservation:
    """One provider-native selection, priced or explicitly unpriced."""

    source_selection_id: str
    display_label: str
    decimal_odds: Decimal | None
    selection_status: SelectionStatus
    price_state: ProviderSelectionPriceState = ProviderSelectionPriceState.PRICED
    canonical_outcome_key: CanonicalOutcomeKey | None = None
    line: Decimal | None = None
    provider_selection_type: str | None = None
    source_participant_id: str | None = None
    provider_order: int | None = None
    source_capture_id: str | None = None

    def __post_init__(self) -> None:
        if not self.source_selection_id.strip():
            msg = "missing source selection identity"
            raise NormalizationError(msg)
        if not self.display_label.strip() or len(self.display_label) > MAX_LABEL_LENGTH:
            msg = "selection display_label must be non-empty and bounded"
            raise NormalizationError(msg)
        if not isinstance(self.price_state, ProviderSelectionPriceState):
            msg = "selection price_state must use the typed contract"
            raise NormalizationError(msg)
        if self.canonical_outcome_key is not None and not isinstance(
            self.canonical_outcome_key,
            CanonicalOutcomeKey,
        ):
            msg = "canonical_outcome_key must use the reviewed typed contract"
            raise NormalizationError(msg)
        if self.price_state is ProviderSelectionPriceState.PRICED:
            if (
                not isinstance(self.decimal_odds, Decimal)
                or not self.decimal_odds.is_finite()
                or self.decimal_odds <= MIN_DECIMAL_ODDS_EXCLUSIVE
            ):
                msg = "priced selection requires finite decimal odds greater than 1"
                raise NormalizationError(msg)
            object.__setattr__(self, "decimal_odds", validate_decimal_odds(self.decimal_odds))
        elif self.decimal_odds is not None:
            msg = "unpriced selection requires decimal_odds to be None"
            raise NormalizationError(msg)
        if self.line is not None and (
            not isinstance(self.line, Decimal) or not self.line.is_finite()
        ):
            msg = "selection line must be a finite Decimal"
            raise NormalizationError(msg)
        if self.provider_order is not None and (
            type(self.provider_order) is not int or self.provider_order < 0
        ):
            msg = "selection provider_order must be a non-negative int"
            raise NormalizationError(msg)
        _validate_optional_bounded(self.provider_selection_type, "provider_selection_type")
        _validate_optional_bounded(self.source_participant_id, "source_participant_id")
        _validate_optional_bounded(self.source_capture_id, "source_capture_id")


@dataclass(frozen=True, slots=True)
class ProviderMarketObservation:
    """One provider market with selections."""

    source_market_id: str
    display_label: str
    market_status: MarketStatus
    selections: tuple[ProviderSelectionObservation, ...]
    period: str | None = None
    line: Decimal | None = None
    overtime_scope: str | None = None
    rules_scope: str | None = None
    canonical_market_definition_id: str | None = None
    provider_market_type: str | None = None
    provider_market_group: str | None = None
    participant_scope: str | None = None
    source_participant_id: str | None = None
    provider_order: int | None = None
    source_capture_id: str | None = None

    def __post_init__(self) -> None:
        if not self.source_market_id.strip():
            msg = "missing source market identity"
            raise NormalizationError(msg)
        if not self.display_label.strip():
            msg = "market display_label must be non-empty"
            raise NormalizationError(msg)
        if not self.selections:
            msg = "market must include at least one selection"
            raise NormalizationError(msg)
        selection_ids = [item.source_selection_id for item in self.selections]
        if len(selection_ids) != len(set(selection_ids)):
            msg = "duplicate source selection identities"
            raise NormalizationError(msg)
        if len(self.display_label) > MAX_LABEL_LENGTH:
            msg = "market display_label must be bounded"
            raise NormalizationError(msg)
        if self.line is not None and (
            not isinstance(self.line, Decimal) or not self.line.is_finite()
        ):
            msg = "market line must be a finite Decimal"
            raise NormalizationError(msg)
        if self.provider_order is not None and (
            type(self.provider_order) is not int or self.provider_order < 0
        ):
            msg = "market provider_order must be a non-negative int"
            raise NormalizationError(msg)
        _validate_optional_bounded(self.provider_market_type, "provider_market_type")
        _validate_optional_bounded(
            self.provider_market_group,
            "provider_market_group",
            maximum=MAX_CATEGORY_LENGTH,
        )
        _validate_optional_bounded(self.participant_scope, "participant_scope")
        _validate_optional_bounded(self.source_participant_id, "source_participant_id")
        _validate_optional_bounded(self.source_capture_id, "source_capture_id")


@dataclass(frozen=True, slots=True)
class ProviderEventObservation:
    """One provider event with participants and markets."""

    source_event_id: str
    source_competition_id: str
    sport: str
    scheduled_start_utc: datetime
    event_state: ProviderEventState
    participants: tuple[ProviderParticipantObservation, ...]
    markets: tuple[ProviderMarketObservation, ...]
    source_page_route_id: str
    competition_display_name: str | None = None
    source_capture_ids: tuple[str, ...] = ()
    completeness: EventCompletenessEvidence = field(default_factory=EventCompletenessEvidence)
    native_markets: tuple[ProviderMarketObservation, ...] = ()

    def __post_init__(self) -> None:
        if not self.source_event_id.strip():
            msg = "missing event identity"
            raise NormalizationError(msg)
        if not self.source_competition_id.strip():
            msg = "missing competition identity"
            raise NormalizationError(msg)
        validate_identifier(self.sport, field_name="sport")
        object.__setattr__(
            self,
            "scheduled_start_utc",
            require_utc(self.scheduled_start_utc, field_name="scheduled_start_utc"),
        )
        if self.event_state is ProviderEventState.LIVE:
            msg = "live events are not supported in bookmaker acquisition PR #11"
            raise NormalizationError(msg)
        if len(self.participants) < 2:
            msg = "event requires at least two participants"
            raise NormalizationError(msg)
        participant_ids = [item.source_participant_id for item in self.participants]
        if len(participant_ids) != len(set(participant_ids)):
            msg = "duplicate source participant identities"
            raise NormalizationError(msg)
        roles = {item.role for item in self.participants}
        if "home" in roles and "away" in roles:
            homes = [item for item in self.participants if item.role == "home"]
            aways = [item for item in self.participants if item.role == "away"]
            if len(homes) != 1 or len(aways) != 1:
                msg = "contradictory home/away participant identities"
                raise NormalizationError(msg)
        market_ids = [item.source_market_id for item in self.markets]
        if len(market_ids) != len(set(market_ids)):
            msg = "duplicate source market identities with different semantics"
            raise NormalizationError(msg)
        native_market_ids = [item.source_market_id for item in self.native_markets]
        if len(native_market_ids) != len(set(native_market_ids)):
            msg = "duplicate provider-native market identities"
            raise NormalizationError(msg)
        if self.native_markets and not set(market_ids).issubset(set(native_market_ids)):
            msg = "canonical market observations must be a subset of native markets"
            raise NormalizationError(msg)
        if self.competition_display_name is not None and not self.competition_display_name.strip():
            msg = "competition display name must be non-empty"
            raise NormalizationError(msg)
        if (
            not isinstance(self.source_capture_ids, tuple)
            or len(self.source_capture_ids) > MAX_EVIDENCE_REFERENCES
            or tuple(sorted(set(self.source_capture_ids))) != self.source_capture_ids
        ):
            msg = "source_capture_ids must be sorted, unique, and bounded"
            raise NormalizationError(msg)
        for capture_id in self.source_capture_ids:
            _validate_optional_bounded(capture_id, "source_capture_id")
        if not isinstance(self.completeness, EventCompletenessEvidence):
            msg = "event completeness evidence must use the typed contract"
            raise NormalizationError(msg)


@dataclass(frozen=True, slots=True)
class ProviderAcquisitionBundle:
    """Parsed provider-domain records from one acquisition cycle."""

    provider_id: str
    adapter_version: str
    acquisition_cycle_id: str
    observed_at_utc: datetime
    sport: str
    events: tuple[ProviderEventObservation, ...]
    warnings: tuple[ProviderParserWarning, ...]
    drift_codes: tuple[str, ...]
    provenance: tuple[str, ...]
    recognized_profile_response_count: int = 0

    def __post_init__(self) -> None:
        validate_identifier(self.provider_id, field_name="provider_id")
        validate_identifier(self.adapter_version, field_name="adapter_version")
        validate_identifier(self.acquisition_cycle_id, field_name="acquisition_cycle_id")
        validate_identifier(self.sport, field_name="sport")
        object.__setattr__(
            self,
            "observed_at_utc",
            require_utc(self.observed_at_utc, field_name="observed_at_utc"),
        )
        if tuple(sorted(self.drift_codes)) != self.drift_codes:
            msg = "drift_codes must be sorted"
            raise PermanentSourceError(msg)
        if tuple(sorted(self.provenance)) != self.provenance:
            msg = "provenance must be sorted"
            raise PermanentSourceError(msg)
        if not 0 <= self.recognized_profile_response_count <= 1_000_000:
            msg = "recognized_profile_response_count is outside the fixed bound"
            raise PermanentSourceError(msg)
        event_ids = [item.source_event_id for item in self.events]
        if len(event_ids) != len(set(event_ids)):
            msg = "duplicate source event identities"
            raise NormalizationError(msg)


def _validate_optional_bounded(
    value: str | None,
    field_name: str,
    *,
    maximum: int = MAX_PROVIDER_CODE_LENGTH,
) -> None:
    if value is not None and (not value.strip() or len(value) > maximum):
        msg = f"{field_name} must be non-empty and bounded when present"
        raise NormalizationError(msg)


def provider_native_markets(
    event: ProviderEventObservation,
) -> tuple[ProviderMarketObservation, ...]:
    """Return native markets, falling back for backward-compatible v1 observations."""
    return event.native_markets if event.native_markets else event.markets
