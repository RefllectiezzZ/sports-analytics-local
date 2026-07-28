"""Strict raw provider observation contracts for bookmaker acquisition."""

from __future__ import annotations

from dataclasses import dataclass
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
MIN_DECIMAL_ODDS_EXCLUSIVE: Final[Decimal] = Decimal("1")


class ProviderEventState(StrEnum):
    """Pre-match / live state as observed from the provider."""

    PRE_MATCH = "pre-match"
    LIVE = "live"
    UNKNOWN = "unknown"


class ParserDriftSeverity(StrEnum):
    """How severely a parser observation diverges from the expected schema."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


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
    """One priced selection from a provider market."""

    source_selection_id: str
    display_label: str
    decimal_odds: Decimal
    selection_status: SelectionStatus
    line: Decimal | None = None

    def __post_init__(self) -> None:
        if not self.source_selection_id.strip():
            msg = "missing source selection identity"
            raise NormalizationError(msg)
        if not self.display_label.strip() or len(self.display_label) > MAX_LABEL_LENGTH:
            msg = "selection display_label must be non-empty and bounded"
            raise NormalizationError(msg)
        if not self.decimal_odds.is_finite() or self.decimal_odds <= MIN_DECIMAL_ODDS_EXCLUSIVE:
            msg = "decimal odds must be finite and greater than 1"
            raise NormalizationError(msg)
        object.__setattr__(self, "decimal_odds", validate_decimal_odds(self.decimal_odds))


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
