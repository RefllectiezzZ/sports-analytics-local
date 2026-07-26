"""Sport-agnostic canonical event-result contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final, cast

from sports_analytics.core.exceptions import ResultError
from sports_analytics.data.codec import format_utc_timestamp
from sports_analytics.data.types import JsonValue, validate_sha256_checksum
from sports_analytics.models.identity import content_addressed_id
from sports_analytics.predictions.contracts import CanonicalSelectionIdentity
from sports_analytics.sports.contracts import require_utc
from sports_analytics.sports.football.markets import (
    MARKET_KEY_MATCH_RESULT_1X2,
    MATCH_RESULT_1X2_OUTCOMES,
    OUTCOME_AWAY,
    OUTCOME_DRAW,
    OUTCOME_HOME,
    match_result_1x2_selection,
)
from sports_analytics.sports.identifiers import SPORT_FOOTBALL

RESULT_SCHEMA_VERSION: Final[str] = "canonical-event-result-v1"
RESULT_IDENTITY_VERSION: Final[str] = "canonical-event-result-identity-v1"


class EventResultStatus(StrEnum):
    """Closed result-feed event lifecycle."""

    SCHEDULED = "scheduled"
    IN_PROGRESS = "in-progress"
    COMPLETED = "completed"
    POSTPONED = "postponed"
    CANCELLED = "cancelled"
    ABANDONED = "abandoned"
    INCOMPLETE = "incomplete"


@dataclass(frozen=True, slots=True, order=True)
class ResultInputSnapshot:
    """Immutable upstream evidence reference carried into a canonical result."""

    snapshot_id: str
    checksum_sha256: str
    schema_version: str
    source_name: str

    def __post_init__(self) -> None:
        _non_empty(self.snapshot_id, "snapshot_id")
        _non_empty(self.schema_version, "schema_version")
        _non_empty(self.source_name, "source_name")
        _checksum(self.checksum_sha256, "checksum_sha256")

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "snapshot_id": self.snapshot_id,
            "checksum_sha256": self.checksum_sha256,
            "schema_version": self.schema_version,
            "source_name": self.source_name,
        }


@dataclass(frozen=True, slots=True, order=True)
class ParticipantResult:
    """One participant's verified final result datum."""

    canonical_participant_id: str
    role: str
    score: int

    def __post_init__(self) -> None:
        _non_empty(self.canonical_participant_id, "canonical_participant_id")
        _non_empty(self.role, "participant result role")
        if type(self.score) is not int or self.score < 0:
            raise ResultError("participant result score must be a non-negative integer")

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "canonical_participant_id": self.canonical_participant_id,
            "role": self.role,
            "score": self.score,
        }


@dataclass(frozen=True, slots=True, order=True)
class MarketOutcome:
    """One member of a complete canonical market outcome space."""

    selection: CanonicalSelectionIdentity
    result: str

    def __post_init__(self) -> None:
        if self.result not in {"win", "loss", "push", "void"}:
            raise ResultError("market outcome result must be win, loss, push, or void")

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "selection_id": self.selection.selection_id,
            "selection": self.selection.identity_payload(),
            "result": self.result,
        }


@dataclass(frozen=True, slots=True)
class CanonicalResult:
    """Verified, source-provenanced canonical event outcome."""

    canonical_result_id: str
    schema_version: str
    identity_version: str
    canonical_event_id: str
    sport_code: str
    event_status: EventResultStatus
    scheduled_start_utc: datetime
    result_timestamp_utc: datetime | None
    source_name: str
    source_event_id: str
    source_observed_at_utc: datetime
    result_provenance: str
    participant_results: tuple[ParticipantResult, ...]
    market_outcomes: tuple[MarketOutcome, ...]
    input_snapshots: tuple[ResultInputSnapshot, ...]
    source_checksum_sha256: str
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.schema_version != RESULT_SCHEMA_VERSION:
            raise ResultError("unsupported canonical result schema version")
        if self.identity_version != RESULT_IDENTITY_VERSION:
            raise ResultError("unsupported canonical result identity version")
        for field_name in (
            "canonical_event_id",
            "sport_code",
            "source_name",
            "source_event_id",
            "result_provenance",
        ):
            _non_empty(cast(str, getattr(self, field_name)), field_name)
        try:
            status = EventResultStatus(self.event_status)
            scheduled = require_utc(self.scheduled_start_utc, field_name="scheduled_start_utc")
            observed = require_utc(
                self.source_observed_at_utc,
                field_name="source_observed_at_utc",
            )
            completed = (
                None
                if self.result_timestamp_utc is None
                else require_utc(self.result_timestamp_utc, field_name="result_timestamp_utc")
            )
        except (ValueError, TypeError) as exc:
            raise ResultError(f"invalid canonical result status or timestamp: {exc}") from exc
        object.__setattr__(self, "event_status", status)
        object.__setattr__(self, "scheduled_start_utc", scheduled)
        object.__setattr__(self, "source_observed_at_utc", observed)
        object.__setattr__(self, "result_timestamp_utc", completed)
        _checksum(self.source_checksum_sha256, "source_checksum_sha256")
        if observed < scheduled and status is EventResultStatus.COMPLETED:
            raise ResultError("completed result cannot be observed before scheduled start")
        if completed is not None and completed > observed:
            raise ResultError("result timestamp cannot follow source observation")
        if status is EventResultStatus.COMPLETED:
            if completed is None or not self.participant_results or not self.market_outcomes:
                raise ResultError(
                    "completed result requires result timestamp, participants, and outcomes"
                )
        elif self.participant_results or self.market_outcomes or completed is not None:
            raise ResultError("non-completed event must not carry trusted result data")
        snapshot_ids = [item.snapshot_id for item in self.input_snapshots]
        if len(snapshot_ids) != len(set(snapshot_ids)):
            raise ResultError("canonical result contains duplicate input snapshots")
        selection_ids = [item.selection.selection_id for item in self.market_outcomes]
        if len(selection_ids) != len(set(selection_ids)):
            raise ResultError("canonical result contains duplicate market outcomes")
        expected = derive_canonical_result_id(self)
        if self.canonical_result_id != expected:
            raise ResultError("canonical_result_id does not match material result content")

    @property
    def winning_selection_ids(self) -> frozenset[str]:
        return frozenset(
            item.selection.selection_id for item in self.market_outcomes if item.result == "win"
        )

    def outcome_for(self, selection: CanonicalSelectionIdentity) -> str:
        for item in self.market_outcomes:
            if item.selection == selection:
                return item.result
        raise ResultError("selection is absent from the complete result outcome space")

    def identity_payload(self) -> dict[str, JsonValue]:
        return _identity_payload(self)

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "canonical_result_id": self.canonical_result_id,
            "schema_version": self.schema_version,
            "identity_version": self.identity_version,
            **self.identity_payload(),
        }


def build_canonical_result(
    *,
    canonical_event_id: str,
    sport_code: str,
    event_status: EventResultStatus | str,
    scheduled_start_utc: datetime,
    result_timestamp_utc: datetime | None,
    source_name: str,
    source_event_id: str,
    source_observed_at_utc: datetime,
    result_provenance: str,
    participant_results: tuple[ParticipantResult, ...] = (),
    market_outcomes: tuple[MarketOutcome, ...] = (),
    input_snapshots: tuple[ResultInputSnapshot, ...] = (),
    source_checksum_sha256: str,
    warnings: tuple[str, ...] = (),
) -> CanonicalResult:
    """Build a canonical result and derive its content identity."""
    try:
        normalized_status = EventResultStatus(event_status)
    except ValueError as exc:
        raise ResultError("unsupported event result status") from exc
    ordered_participants = tuple(
        sorted(participant_results, key=lambda item: (item.role, item.canonical_participant_id))
    )
    ordered_outcomes = tuple(sorted(market_outcomes, key=lambda item: item.selection.selection_id))
    ordered_inputs = tuple(sorted(input_snapshots, key=lambda item: item.snapshot_id))
    provisional = CanonicalResult.__new__(CanonicalResult)
    for key, value in {
        "canonical_event_id": canonical_event_id,
        "sport_code": sport_code,
        "event_status": normalized_status,
        "scheduled_start_utc": scheduled_start_utc,
        "result_timestamp_utc": result_timestamp_utc,
        "source_name": source_name,
        "source_event_id": source_event_id,
        "source_observed_at_utc": source_observed_at_utc,
        "result_provenance": result_provenance,
        "participant_results": ordered_participants,
        "market_outcomes": ordered_outcomes,
        "input_snapshots": ordered_inputs,
        "source_checksum_sha256": source_checksum_sha256,
        "warnings": tuple(sorted(warnings)),
    }.items():
        object.__setattr__(provisional, key, value)
    identity = content_addressed_id(
        identity_type=RESULT_IDENTITY_VERSION,
        payload=_identity_payload(provisional),
    )
    return CanonicalResult(
        canonical_result_id=identity,
        schema_version=RESULT_SCHEMA_VERSION,
        identity_version=RESULT_IDENTITY_VERSION,
        canonical_event_id=canonical_event_id,
        sport_code=sport_code,
        event_status=normalized_status,
        scheduled_start_utc=scheduled_start_utc,
        result_timestamp_utc=result_timestamp_utc,
        source_name=source_name,
        source_event_id=source_event_id,
        source_observed_at_utc=source_observed_at_utc,
        result_provenance=result_provenance,
        participant_results=ordered_participants,
        market_outcomes=ordered_outcomes,
        input_snapshots=ordered_inputs,
        source_checksum_sha256=source_checksum_sha256,
        warnings=tuple(sorted(warnings)),
    )


def build_football_full_match_1x2_result(
    *,
    canonical_event_id: str,
    scheduled_start_utc: datetime,
    event_status: EventResultStatus | str,
    source_name: str,
    source_event_id: str,
    source_observed_at_utc: datetime,
    source_checksum_sha256: str,
    result_provenance: str,
    home_canonical_participant_id: str,
    away_canonical_participant_id: str,
    full_time_home_score: int | None = None,
    full_time_away_score: int | None = None,
    result_timestamp_utc: datetime | None = None,
    claimed_outcome_key: str | None = None,
    input_snapshots: tuple[ResultInputSnapshot, ...] = (),
) -> CanonicalResult:
    """Project verified football full-time scores into the canonical 1X2 market."""
    try:
        status = EventResultStatus(event_status)
    except ValueError as exc:
        raise ResultError("unsupported event result status") from exc
    if home_canonical_participant_id == away_canonical_participant_id:
        raise ResultError("football result participants must differ")
    if status is not EventResultStatus.COMPLETED:
        if (
            full_time_home_score is not None
            or full_time_away_score is not None
            or result_timestamp_utc is not None
            or claimed_outcome_key is not None
        ):
            raise ResultError("non-completed football event must not carry final result data")
        return build_canonical_result(
            canonical_event_id=canonical_event_id,
            sport_code=SPORT_FOOTBALL,
            event_status=status,
            scheduled_start_utc=scheduled_start_utc,
            result_timestamp_utc=None,
            source_name=source_name,
            source_event_id=source_event_id,
            source_observed_at_utc=source_observed_at_utc,
            result_provenance=result_provenance,
            input_snapshots=input_snapshots,
            source_checksum_sha256=source_checksum_sha256,
        )
    for score, name in (
        (full_time_home_score, "full_time_home_score"),
        (full_time_away_score, "full_time_away_score"),
    ):
        if type(score) is not int or score < 0:
            raise ResultError(f"{name} must be a non-negative integer")
    if result_timestamp_utc is None:
        raise ResultError("completed football result requires result_timestamp_utc")
    home_score = cast(int, full_time_home_score)
    away_score = cast(int, full_time_away_score)
    winner = (
        OUTCOME_HOME
        if home_score > away_score
        else OUTCOME_AWAY
        if away_score > home_score
        else OUTCOME_DRAW
    )
    if claimed_outcome_key is not None and claimed_outcome_key != winner:
        raise ResultError("claimed football outcome contradicts verified full-time scores")
    outcomes = tuple(
        MarketOutcome(
            selection=CanonicalSelectionIdentity.from_selection(
                match_result_1x2_selection(outcome_key)
            ),
            result="win" if outcome_key == winner else "loss",
        )
        for outcome_key in MATCH_RESULT_1X2_OUTCOMES
    )
    if any(item.selection.market_key != MARKET_KEY_MATCH_RESULT_1X2 for item in outcomes):
        raise ResultError("football result adapter produced a contradictory market identity")
    return build_canonical_result(
        canonical_event_id=canonical_event_id,
        sport_code=SPORT_FOOTBALL,
        event_status=status,
        scheduled_start_utc=scheduled_start_utc,
        result_timestamp_utc=result_timestamp_utc,
        source_name=source_name,
        source_event_id=source_event_id,
        source_observed_at_utc=source_observed_at_utc,
        result_provenance=result_provenance,
        participant_results=(
            ParticipantResult(home_canonical_participant_id, "home", home_score),
            ParticipantResult(away_canonical_participant_id, "away", away_score),
        ),
        market_outcomes=outcomes,
        input_snapshots=input_snapshots,
        source_checksum_sha256=source_checksum_sha256,
    )


def derive_canonical_result_id(result: CanonicalResult) -> str:
    return content_addressed_id(
        identity_type=RESULT_IDENTITY_VERSION,
        payload=_identity_payload(result),
    )


def _identity_payload(result: CanonicalResult) -> dict[str, JsonValue]:
    return {
        "canonical_event_id": result.canonical_event_id,
        "sport_code": result.sport_code,
        "event_status": result.event_status.value,
        "scheduled_start_utc": format_utc_timestamp(
            require_utc(result.scheduled_start_utc, field_name="scheduled_start_utc")
        ),
        "result_timestamp_utc": (
            None
            if result.result_timestamp_utc is None
            else format_utc_timestamp(
                require_utc(result.result_timestamp_utc, field_name="result_timestamp_utc")
            )
        ),
        "source_name": result.source_name,
        "source_event_id": result.source_event_id,
        "source_observed_at_utc": format_utc_timestamp(
            require_utc(result.source_observed_at_utc, field_name="source_observed_at_utc")
        ),
        "result_provenance": result.result_provenance,
        "participant_results": [item.to_json() for item in result.participant_results],
        "market_outcomes": [item.to_json() for item in result.market_outcomes],
        "input_snapshots": [item.to_json() for item in result.input_snapshots],
        "source_checksum_sha256": result.source_checksum_sha256,
        "warnings": list(result.warnings),
    }


def _non_empty(value: str, field_name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ResultError(f"{field_name} must be a non-empty string without surrounding whitespace")
    return value


def _checksum(value: str, field_name: str) -> str:
    try:
        return validate_sha256_checksum(value)
    except Exception as exc:
        raise ResultError(f"{field_name} must be a lowercase SHA-256 checksum") from exc
