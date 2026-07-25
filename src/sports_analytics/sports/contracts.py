"""Sport-agnostic canonical and source-scoped entity contracts.

The platform distinguishes four identity concepts that earlier revisions of the
ingestion pipeline conflated:

``CanonicalParticipant``
    A real-world competitor (team or player) that must resolve to the same
    identity for every source. Canonical identity never depends on
    ``source_name``.

``SourceParticipantReference``
    How one source names a participant, retained for provenance and adapter
    tracing. Source identity always depends on ``source_name``.

``CanonicalEvent``
    A real-world fixture that must resolve to the same identity for every
    source.

``SourceEventReference``
    How one source identifies that fixture, including row-level provenance.

``EventReconciliation``
    The auditable, versioned decision that links a source event reference to a
    canonical event, including an explicit state and bounded confidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from math import isfinite
from typing import Final

from sports_analytics.core.exceptions import NormalizationError, RepositoryError
from sports_analytics.data.types import validate_identifier

MAX_DISPLAY_NAME_LENGTH: Final[int] = 200
MAX_REASON_LENGTH: Final[int] = 500


class ParticipantType(StrEnum):
    """Kinds of competitor supported by canonical participant identity."""

    TEAM = "team"
    PLAYER = "player"


class EventStatus(StrEnum):
    """Lifecycle state of a canonical event."""

    SCHEDULED = "scheduled"
    LIVE = "live"
    FINISHED = "finished"
    POSTPONED = "postponed"
    CANCELLED = "cancelled"
    ABANDONED = "abandoned"
    UNKNOWN = "unknown"


class StartTimePrecision(StrEnum):
    """Known precision of an event start timestamp."""

    DATE_ONLY = "date-only"
    MINUTE = "minute"
    SECOND = "second"
    UNKNOWN = "unknown"


class OutcomeAvailability(StrEnum):
    """Whether an event outcome is known, and when it became knowable.

    ``PRE_EVENT_UNAVAILABLE`` marks events whose outcome cannot be used as a
    pre-match feature because it does not exist yet.
    """

    POST_EVENT = "post-event"
    PRE_EVENT_UNAVAILABLE = "pre-event-unavailable"


class ReconciliationState(StrEnum):
    """Auditable state of a source-to-canonical event reconciliation."""

    EXACT = "exact"
    PROBABLE = "probable"
    MANUAL = "manual"
    UNRESOLVED = "unresolved"


#: States whose events are safe for downstream prediction/odds comparison.
DOWNSTREAM_SAFE_RECONCILIATION_STATES: Final[frozenset[ReconciliationState]] = frozenset(
    {ReconciliationState.EXACT, ReconciliationState.MANUAL}
)


def validate_display_name(value: str, *, field_name: str) -> str:
    """Validate a human-readable display name for canonical/source entities."""
    if not isinstance(value, str):
        msg = f"{field_name} must be a string"
        raise NormalizationError(msg)
    if value != value.strip() or not value:
        msg = f"{field_name} must be non-empty without surrounding whitespace"
        raise NormalizationError(msg)
    if len(value) > MAX_DISPLAY_NAME_LENGTH:
        msg = f"{field_name} exceeds maximum length of {MAX_DISPLAY_NAME_LENGTH}"
        raise NormalizationError(msg)
    if "\x00" in value:
        msg = f"{field_name} must not contain NUL"
        raise NormalizationError(msg)
    return value


def validate_canonical_key(value: str, *, field_name: str) -> str:
    """Validate a canonical participant key (case-folded, source-independent)."""
    if not isinstance(value, str):
        msg = f"{field_name} must be a string"
        raise NormalizationError(msg)
    if value != value.strip() or not value:
        msg = f"{field_name} must be non-empty without surrounding whitespace"
        raise NormalizationError(msg)
    if value != value.casefold():
        msg = f"{field_name} must already be case-folded"
        raise NormalizationError(msg)
    if len(value) > MAX_DISPLAY_NAME_LENGTH:
        msg = f"{field_name} exceeds maximum length of {MAX_DISPLAY_NAME_LENGTH}"
        raise NormalizationError(msg)
    return value


def validate_domain_identifier(value: str, *, field_name: str) -> str:
    """Validate a lowercase domain identifier, raising ``NormalizationError``."""
    try:
        return validate_identifier(value, field_name=field_name)
    except RepositoryError as exc:
        raise NormalizationError(str(exc)) from exc


def validate_confidence(value: float, *, field_name: str = "reconciliation_confidence") -> float:
    """Require a reconciliation confidence bounded inclusively between 0.0 and 1.0."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        msg = f"{field_name} must be a number between 0.0 and 1.0"
        raise NormalizationError(msg)
    number = float(value)
    if not isfinite(number) or number < 0.0 or number > 1.0:
        msg = f"{field_name} must be between 0.0 and 1.0"
        raise NormalizationError(msg)
    return number


def require_utc(value: datetime, *, field_name: str) -> datetime:
    """Require a timezone-aware timestamp and normalize it to UTC."""
    if not isinstance(value, datetime):
        msg = f"{field_name} must be a datetime"
        raise NormalizationError(msg)
    if value.tzinfo is None:
        msg = f"{field_name} must be timezone-aware"
        raise NormalizationError(msg)
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class CompetitionRecord:
    """One competition as published by an ingestion snapshot."""

    competition_id: str
    sport_code: str
    display_name: str
    country_code: str
    competition_type: str
    source_name: str
    source_competition_code: str
    timezone: str
    schema_version: str


@dataclass(frozen=True, slots=True)
class SeasonRecord:
    """One season as published by an ingestion snapshot."""

    season_id: str
    competition_id: str
    label: str
    start_year: int
    end_year: int
    source_season_code: str
    schema_version: str


@dataclass(frozen=True, slots=True)
class CanonicalParticipant:
    """A source-independent competitor identity.

    ``competition_id`` scopes the identity for the current domestic-league exact
    policy so the same normalized name in two competitions cannot silently merge.
    """

    canonical_participant_id: str
    sport_code: str
    competition_id: str
    participant_type: str
    canonical_key: str
    display_name: str
    schema_version: str

    def __post_init__(self) -> None:
        validate_domain_identifier(self.sport_code, field_name="sport_code")
        validate_domain_identifier(self.competition_id, field_name="competition_id")
        validate_domain_identifier(self.participant_type, field_name="participant_type")
        validate_canonical_key(self.canonical_key, field_name="canonical_key")
        validate_display_name(self.display_name, field_name="display_name")


@dataclass(frozen=True, slots=True)
class SourceParticipantReference:
    """How one source names a participant.

    Representable before canonical resolution: ``canonical_participant_id`` is
    null until an exact, probable, or manual reconciliation succeeds.
    """

    source_participant_id: str
    source_name: str
    source_participant_key: str
    canonical_participant_id: str | None
    competition_id: str
    participant_type: str
    display_name: str
    normalized_name: str
    schema_version: str

    def __post_init__(self) -> None:
        validate_domain_identifier(self.source_name, field_name="source_name")
        validate_domain_identifier(self.competition_id, field_name="competition_id")
        validate_domain_identifier(self.participant_type, field_name="participant_type")
        validate_display_name(self.display_name, field_name="display_name")


@dataclass(frozen=True, slots=True)
class ParticipantReconciliation:
    """Auditable, versioned link between a source participant and a canonical one."""

    source_name: str
    source_participant_id: str
    source_participant_key: str
    canonical_participant_id: str | None
    state: str
    confidence: float
    policy_version: str
    match_key: str | None
    reason: str | None
    source_observed_at_utc: datetime
    schema_version: str

    def __post_init__(self) -> None:
        validate_domain_identifier(self.source_name, field_name="source_name")
        validate_domain_identifier(self.policy_version, field_name="reconciliation_policy_version")
        validate_reconciliation_state(self.state)
        validate_confidence(self.confidence)
        _validate_state_confidence(self.state, self.confidence)
        object.__setattr__(
            self,
            "source_observed_at_utc",
            require_utc(self.source_observed_at_utc, field_name="source_observed_at_utc"),
        )
        if self.reason is not None and len(self.reason) > MAX_REASON_LENGTH:
            msg = f"reconciliation reason exceeds maximum length of {MAX_REASON_LENGTH}"
            raise NormalizationError(msg)
        if self.state == ReconciliationState.UNRESOLVED.value:
            if self.canonical_participant_id is not None:
                msg = "unresolved participant reconciliation must not claim a canonical id"
                raise NormalizationError(msg)
            if self.reason is None:
                msg = "unresolved participant reconciliation requires an explicit reason"
                raise NormalizationError(msg)
        elif self.canonical_participant_id is None:
            msg = f"{self.state} participant reconciliation requires a canonical participant id"
            raise NormalizationError(msg)

    @property
    def is_downstream_safe(self) -> bool:
        """Whether downstream readers may treat the participant as reconciled."""
        return ReconciliationState(self.state) in DOWNSTREAM_SAFE_RECONCILIATION_STATES


@dataclass(frozen=True, slots=True)
class CanonicalEvent:
    """A source-independent fixture identity plus its canonical outcome.

    ``event_date`` and ``scheduled_start_utc`` are mutable scheduling metadata.
    Immutable identity is ``canonical_event_id``, derived from participants and
    ``event_occurrence_key`` rather than from the scheduled date.
    """

    canonical_event_id: str
    sport_code: str
    competition_id: str
    season_id: str
    event_occurrence_key: str
    event_date: date
    scheduled_start_utc: datetime | None
    start_time_precision: str
    status: str
    home_canonical_participant_id: str
    away_canonical_participant_id: str
    home_score: int | None
    away_score: int | None
    result_code: str | None
    outcome_availability_stage: str
    schema_version: str

    def __post_init__(self) -> None:
        validate_domain_identifier(self.sport_code, field_name="sport_code")
        validate_domain_identifier(self.competition_id, field_name="competition_id")
        validate_domain_identifier(self.season_id, field_name="season_id")
        validate_domain_identifier(self.event_occurrence_key, field_name="event_occurrence_key")
        if self.home_canonical_participant_id == self.away_canonical_participant_id:
            msg = "canonical event participants must differ"
            raise NormalizationError(msg)
        if self.scheduled_start_utc is not None:
            object.__setattr__(
                self,
                "scheduled_start_utc",
                require_utc(self.scheduled_start_utc, field_name="scheduled_start_utc"),
            )
        if self.status == EventStatus.FINISHED.value:
            if self.home_score is None or self.away_score is None:
                msg = "finished canonical events require both scores"
                raise NormalizationError(msg)
            if self.outcome_availability_stage != OutcomeAvailability.POST_EVENT.value:
                msg = "finished canonical events must record a post-event outcome stage"
                raise NormalizationError(msg)
        elif self.home_score is not None or self.away_score is not None:
            msg = "only finished canonical events may carry scores"
            raise NormalizationError(msg)
        elif self.outcome_availability_stage != OutcomeAvailability.PRE_EVENT_UNAVAILABLE.value:
            msg = "unfinished canonical events must mark the outcome unavailable"
            raise NormalizationError(msg)


@dataclass(frozen=True, slots=True)
class SourceEventReference:
    """Source-scoped fixture identity and row-level provenance.

    Representable before canonical resolution: ``canonical_event_id`` is null
    until event reconciliation succeeds.
    """

    source_event_id: str
    source_name: str
    source_event_key: str
    canonical_event_id: str | None
    source_row_number: int
    source_observed_at_utc: datetime
    source_file_sha256: str
    schema_version: str

    def __post_init__(self) -> None:
        validate_domain_identifier(self.source_name, field_name="source_name")
        object.__setattr__(
            self,
            "source_observed_at_utc",
            require_utc(self.source_observed_at_utc, field_name="source_observed_at_utc"),
        )


@dataclass(frozen=True, slots=True)
class EventReconciliation:
    """Auditable, versioned link between a source event and a canonical event."""

    source_name: str
    source_event_id: str
    source_event_key: str
    canonical_event_id: str | None
    state: str
    confidence: float
    policy_version: str
    match_key: str | None
    reason: str | None
    source_observed_at_utc: datetime
    schema_version: str

    def __post_init__(self) -> None:
        validate_domain_identifier(self.source_name, field_name="source_name")
        validate_domain_identifier(self.policy_version, field_name="reconciliation_policy_version")
        validate_reconciliation_state(self.state)
        validate_confidence(self.confidence)
        _validate_state_confidence(self.state, self.confidence)
        object.__setattr__(
            self,
            "source_observed_at_utc",
            require_utc(self.source_observed_at_utc, field_name="source_observed_at_utc"),
        )
        if self.reason is not None and len(self.reason) > MAX_REASON_LENGTH:
            msg = f"reconciliation reason exceeds maximum length of {MAX_REASON_LENGTH}"
            raise NormalizationError(msg)
        if self.state == ReconciliationState.UNRESOLVED.value:
            if self.canonical_event_id is not None:
                msg = "unresolved reconciliation must not claim a canonical event"
                raise NormalizationError(msg)
            if self.reason is None:
                msg = "unresolved reconciliation requires an explicit reason"
                raise NormalizationError(msg)
        elif self.canonical_event_id is None:
            msg = f"{self.state} reconciliation requires a canonical event id"
            raise NormalizationError(msg)

    @property
    def is_downstream_safe(self) -> bool:
        """Whether downstream readers may treat the event as reconciled."""
        return ReconciliationState(self.state) in DOWNSTREAM_SAFE_RECONCILIATION_STATES


@dataclass(frozen=True, slots=True)
class IngestedParticipant:
    """One source participant reference with its reconciliation and optional canonical."""

    source_reference: SourceParticipantReference
    reconciliation: ParticipantReconciliation
    canonical: CanonicalParticipant | None

    def __post_init__(self) -> None:
        if self.reconciliation.is_downstream_safe:
            if self.canonical is None:
                msg = "downstream-safe participant reconciliation requires a canonical participant"
                raise NormalizationError(msg)
            if self.source_reference.canonical_participant_id != (
                self.canonical.canonical_participant_id
            ):
                msg = "source participant canonical id must match the reconciled canonical"
                raise NormalizationError(msg)
        elif (
            self.canonical is not None or self.source_reference.canonical_participant_id is not None
        ):
            msg = "unresolved participants must not claim a canonical participant"
            raise NormalizationError(msg)


@dataclass(frozen=True, slots=True)
class IngestedSourceEvent:
    """One source event candidate with scheduling, participants, and reconciliation.

    Every source event is retained, including unresolved ones. Odds and
    post-match statistics for unresolved events must not enter downstream-safe
    datasets.
    """

    source_reference: SourceEventReference
    reconciliation: EventReconciliation
    competition_id: str
    season_id: str
    event_occurrence_key: str | None
    event_date: date | None
    scheduled_start_utc: datetime | None
    start_time_precision: str
    status: str
    home_source_participant_id: str
    away_source_participant_id: str
    home_canonical_participant_id: str | None
    away_canonical_participant_id: str | None
    home_score: int | None
    away_score: int | None
    result_code: str | None
    outcome_availability_stage: str
    schema_version: str

    def __post_init__(self) -> None:
        if self.scheduled_start_utc is not None:
            object.__setattr__(
                self,
                "scheduled_start_utc",
                require_utc(self.scheduled_start_utc, field_name="scheduled_start_utc"),
            )
        if self.reconciliation.is_downstream_safe:
            if self.source_reference.canonical_event_id is None:
                msg = "downstream-safe source events require a canonical event id"
                raise NormalizationError(msg)
        elif self.source_reference.canonical_event_id is not None:
            msg = "unresolved source events must not claim a canonical event id"
            raise NormalizationError(msg)


@dataclass(frozen=True, slots=True)
class IngestedEvent:
    """One downstream-safe canonical event produced by successful reconciliation."""

    canonical: CanonicalEvent
    reconciliation: EventReconciliation

    def __post_init__(self) -> None:
        if not self.reconciliation.is_downstream_safe:
            msg = "ingested canonical events require a downstream-safe reconciliation"
            raise NormalizationError(msg)
        if self.reconciliation.canonical_event_id != self.canonical.canonical_event_id:
            msg = "event reconciliation canonical id must match the canonical event"
            raise NormalizationError(msg)


def validate_reconciliation_state(value: str) -> str:
    """Validate a reconciliation state string against the closed contract set."""
    try:
        return ReconciliationState(value).value
    except ValueError as exc:
        allowed = ", ".join(state.value for state in ReconciliationState)
        msg = f"reconciliation state must be one of: {allowed}"
        raise NormalizationError(msg) from exc


def _validate_state_confidence(state: str, confidence: float) -> None:
    if state == ReconciliationState.EXACT.value and confidence != 1.0:
        msg = "exact reconciliation requires confidence 1.0"
        raise NormalizationError(msg)
    if state == ReconciliationState.UNRESOLVED.value and confidence != 0.0:
        msg = "unresolved reconciliation requires confidence 0.0"
        raise NormalizationError(msg)
    if state == ReconciliationState.PROBABLE.value and not 0.0 < confidence < 1.0:
        msg = "probable reconciliation requires confidence strictly between 0.0 and 1.0"
        raise NormalizationError(msg)
