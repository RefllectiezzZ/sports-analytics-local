"""Sport-agnostic canonical and source-scoped Arrow schemas and row builders.

Every schema is parameterized by ``schema_version`` and ``sport_code`` so no
sport-specific constant is hard-coded in shared code. Nullability is explicit:
canonical datasets contain only downstream-safe identities, while source-scoped
datasets retain unresolved rows for provenance and reconciliation audit.
"""

from __future__ import annotations

from typing import Any, Final

import pyarrow as pa

from sports_analytics.snapshots.arrow import (
    dataset_metadata,
    dictionary_string,
    utc_timestamp,
)
from sports_analytics.sports.contracts import (
    CanonicalEvent,
    CanonicalParticipant,
    CompetitionRecord,
    EventReconciliation,
    IngestedEvent,
    IngestedParticipant,
    IngestedSourceEvent,
    ParticipantReconciliation,
    SeasonRecord,
)

DATASET_COMPETITIONS: Final[str] = "competitions"
DATASET_SEASONS: Final[str] = "seasons"
DATASET_PARTICIPANTS: Final[str] = "participants"
DATASET_SOURCE_PARTICIPANTS: Final[str] = "source_participants"
DATASET_PARTICIPANT_RECONCILIATIONS: Final[str] = "participant_reconciliations"
DATASET_EVENTS: Final[str] = "events"
DATASET_SOURCE_EVENTS: Final[str] = "source_events"
DATASET_EVENT_RECONCILIATIONS: Final[str] = "event_reconciliations"

_IngestedEventRowsInput = tuple[IngestedEvent, ...]


def competitions_schema(*, schema_version: str, sport_code: str) -> pa.Schema:
    """Return the competitions dataset schema."""
    return pa.schema(
        [
            pa.field("competition_id", pa.string(), nullable=False),
            pa.field("sport_code", dictionary_string(), nullable=False),
            pa.field("display_name", pa.string(), nullable=False),
            pa.field("country_code", pa.string(), nullable=False),
            pa.field("competition_type", dictionary_string(), nullable=False),
            pa.field("source_name", dictionary_string(), nullable=False),
            pa.field("source_competition_code", pa.string(), nullable=False),
            pa.field("timezone", pa.string(), nullable=False),
            pa.field("schema_version", dictionary_string(), nullable=False),
        ],
        metadata=dataset_metadata(
            dataset_name=DATASET_COMPETITIONS,
            schema_version=schema_version,
            domain=sport_code,
        ),
    )


def seasons_schema(*, schema_version: str, sport_code: str) -> pa.Schema:
    """Return the seasons dataset schema."""
    return pa.schema(
        [
            pa.field("season_id", pa.string(), nullable=False),
            pa.field("competition_id", pa.string(), nullable=False),
            pa.field("label", pa.string(), nullable=False),
            pa.field("start_year", pa.int16(), nullable=False),
            pa.field("end_year", pa.int16(), nullable=False),
            pa.field("source_season_code", pa.string(), nullable=False),
            pa.field("schema_version", dictionary_string(), nullable=False),
        ],
        metadata=dataset_metadata(
            dataset_name=DATASET_SEASONS,
            schema_version=schema_version,
            domain=sport_code,
        ),
    )


def participants_schema(*, schema_version: str, sport_code: str) -> pa.Schema:
    """Return the canonical participants dataset schema."""
    return pa.schema(
        [
            pa.field("canonical_participant_id", pa.string(), nullable=False),
            pa.field("sport_code", dictionary_string(), nullable=False),
            pa.field("participant_identity_scope", dictionary_string(), nullable=False),
            pa.field("participant_type", dictionary_string(), nullable=False),
            pa.field("canonical_key", pa.string(), nullable=False),
            pa.field("display_name", pa.string(), nullable=False),
            pa.field("schema_version", dictionary_string(), nullable=False),
        ],
        metadata=dataset_metadata(
            dataset_name=DATASET_PARTICIPANTS,
            schema_version=schema_version,
            domain=sport_code,
        ),
    )


def source_participants_schema(*, schema_version: str, sport_code: str) -> pa.Schema:
    """Return the source participant reference dataset schema."""
    return pa.schema(
        [
            pa.field("source_participant_id", pa.string(), nullable=False),
            pa.field("source_name", dictionary_string(), nullable=False),
            pa.field("source_participant_key", pa.string(), nullable=False),
            pa.field("competition_id", pa.string(), nullable=False),
            pa.field("canonical_participant_id", pa.string(), nullable=True),
            pa.field("participant_type", dictionary_string(), nullable=False),
            pa.field("display_name", pa.string(), nullable=False),
            pa.field("normalized_name", pa.string(), nullable=False),
            pa.field("schema_version", dictionary_string(), nullable=False),
        ],
        metadata=dataset_metadata(
            dataset_name=DATASET_SOURCE_PARTICIPANTS,
            schema_version=schema_version,
            domain=sport_code,
        ),
    )


def participant_reconciliations_schema(*, schema_version: str, sport_code: str) -> pa.Schema:
    """Return the participant reconciliation audit dataset schema.

    ``canonical_participant_id`` and ``match_key`` are null exactly for unresolved
    reconciliations; ``reason`` is null exactly when no problem was recorded.
    """
    return pa.schema(
        [
            pa.field("source_name", dictionary_string(), nullable=False),
            pa.field("source_participant_id", pa.string(), nullable=False),
            pa.field("source_participant_key", pa.string(), nullable=False),
            pa.field("canonical_participant_id", pa.string(), nullable=True),
            pa.field("reconciliation_state", dictionary_string(), nullable=False),
            pa.field("reconciliation_confidence", pa.float64(), nullable=False),
            pa.field("reconciliation_policy_version", dictionary_string(), nullable=False),
            pa.field("match_key", pa.string(), nullable=True),
            pa.field("reason", pa.string(), nullable=True),
            pa.field("source_observed_at_utc", utc_timestamp(), nullable=False),
            pa.field("schema_version", dictionary_string(), nullable=False),
        ],
        metadata=dataset_metadata(
            dataset_name=DATASET_PARTICIPANT_RECONCILIATIONS,
            schema_version=schema_version,
            domain=sport_code,
        ),
    )


def events_schema(*, schema_version: str, sport_code: str) -> pa.Schema:
    """Return the canonical events dataset schema.

    ``scheduled_start_utc`` is nullable when only a date is available; scores and
    ``result_code`` are nullable when the event is not finished.
    ``event_occurrence_key`` is the source-independent occurrence discriminator.
    """
    return pa.schema(
        [
            pa.field("canonical_event_id", pa.string(), nullable=False),
            pa.field("sport_code", dictionary_string(), nullable=False),
            pa.field("competition_id", pa.string(), nullable=False),
            pa.field("season_id", pa.string(), nullable=False),
            pa.field("event_occurrence_key", pa.string(), nullable=False),
            pa.field("event_date", pa.date32(), nullable=False),
            pa.field("scheduled_start_utc", utc_timestamp(), nullable=True),
            pa.field("start_time_precision", dictionary_string(), nullable=False),
            pa.field("status", dictionary_string(), nullable=False),
            pa.field("home_canonical_participant_id", pa.string(), nullable=False),
            pa.field("away_canonical_participant_id", pa.string(), nullable=False),
            pa.field("home_score", pa.int16(), nullable=True),
            pa.field("away_score", pa.int16(), nullable=True),
            pa.field("result_code", dictionary_string(), nullable=True),
            pa.field("outcome_availability_stage", dictionary_string(), nullable=False),
            pa.field("schema_version", dictionary_string(), nullable=False),
        ],
        metadata=dataset_metadata(
            dataset_name=DATASET_EVENTS,
            schema_version=schema_version,
            domain=sport_code,
        ),
    )


def source_events_schema(*, schema_version: str, sport_code: str) -> pa.Schema:
    """Return the source event dataset schema, including unresolved events."""
    return pa.schema(
        [
            pa.field("source_name", dictionary_string(), nullable=False),
            pa.field("source_event_id", pa.string(), nullable=False),
            pa.field("source_event_key", pa.string(), nullable=False),
            pa.field("canonical_event_id", pa.string(), nullable=True),
            pa.field("sport_code", dictionary_string(), nullable=False),
            pa.field("competition_id", pa.string(), nullable=False),
            pa.field("season_id", pa.string(), nullable=False),
            pa.field("event_occurrence_key", pa.string(), nullable=True),
            pa.field("event_date", pa.date32(), nullable=True),
            pa.field("scheduled_start_utc", utc_timestamp(), nullable=True),
            pa.field("start_time_precision", dictionary_string(), nullable=False),
            pa.field("status", dictionary_string(), nullable=False),
            pa.field("home_source_participant_id", pa.string(), nullable=False),
            pa.field("away_source_participant_id", pa.string(), nullable=False),
            pa.field("home_canonical_participant_id", pa.string(), nullable=True),
            pa.field("away_canonical_participant_id", pa.string(), nullable=True),
            pa.field("home_score", pa.int16(), nullable=True),
            pa.field("away_score", pa.int16(), nullable=True),
            pa.field("result_code", dictionary_string(), nullable=True),
            pa.field("outcome_availability_stage", dictionary_string(), nullable=False),
            pa.field("source_row_number", pa.int32(), nullable=False),
            pa.field("source_file_sha256", pa.string(), nullable=False),
            pa.field("source_observed_at_utc", utc_timestamp(), nullable=False),
            pa.field("reconciliation_state", dictionary_string(), nullable=False),
            pa.field("reconciliation_confidence", pa.float64(), nullable=False),
            pa.field("reconciliation_policy_version", dictionary_string(), nullable=False),
            pa.field("reconciliation_reason", pa.string(), nullable=True),
            pa.field("schema_version", dictionary_string(), nullable=False),
        ],
        metadata=dataset_metadata(
            dataset_name=DATASET_SOURCE_EVENTS,
            schema_version=schema_version,
            domain=sport_code,
        ),
    )


def event_reconciliations_schema(*, schema_version: str, sport_code: str) -> pa.Schema:
    """Return the event reconciliation audit dataset schema.

    ``canonical_event_id`` and ``match_key`` are null exactly for unresolved
    reconciliations; ``reason`` is null exactly when no problem was recorded.
    """
    return pa.schema(
        [
            pa.field("source_name", dictionary_string(), nullable=False),
            pa.field("source_event_id", pa.string(), nullable=False),
            pa.field("source_event_key", pa.string(), nullable=False),
            pa.field("canonical_event_id", pa.string(), nullable=True),
            pa.field("reconciliation_state", dictionary_string(), nullable=False),
            pa.field("reconciliation_confidence", pa.float64(), nullable=False),
            pa.field("reconciliation_policy_version", dictionary_string(), nullable=False),
            pa.field("match_key", pa.string(), nullable=True),
            pa.field("reason", pa.string(), nullable=True),
            pa.field("source_observed_at_utc", utc_timestamp(), nullable=False),
            pa.field("schema_version", dictionary_string(), nullable=False),
        ],
        metadata=dataset_metadata(
            dataset_name=DATASET_EVENT_RECONCILIATIONS,
            schema_version=schema_version,
            domain=sport_code,
        ),
    )


def competition_rows(records: tuple[CompetitionRecord, ...]) -> list[dict[str, Any]]:
    """Build competition rows in deterministic order."""
    return [
        {
            "competition_id": item.competition_id,
            "sport_code": item.sport_code,
            "display_name": item.display_name,
            "country_code": item.country_code,
            "competition_type": item.competition_type,
            "source_name": item.source_name,
            "source_competition_code": item.source_competition_code,
            "timezone": item.timezone,
            "schema_version": item.schema_version,
        }
        for item in records
    ]


def season_rows(records: tuple[SeasonRecord, ...]) -> list[dict[str, Any]]:
    """Build season rows in deterministic order."""
    return [
        {
            "season_id": item.season_id,
            "competition_id": item.competition_id,
            "label": item.label,
            "start_year": item.start_year,
            "end_year": item.end_year,
            "source_season_code": item.source_season_code,
            "schema_version": item.schema_version,
        }
        for item in records
    ]


def participant_rows(participants: tuple[IngestedParticipant, ...]) -> list[dict[str, Any]]:
    """Build canonical participant rows from resolved participants."""
    canonical_by_id = {
        item.canonical.canonical_participant_id: item.canonical
        for item in participants
        if item.canonical is not None
    }
    return [
        _canonical_participant_row(item)
        for item in sorted(
            canonical_by_id.values(),
            key=lambda participant: participant.canonical_participant_id,
        )
    ]


def source_participant_rows(
    participants: tuple[IngestedParticipant, ...],
) -> list[dict[str, Any]]:
    """Build source participant reference rows, including unresolved participants."""
    return [
        {
            "source_participant_id": item.source_reference.source_participant_id,
            "source_name": item.source_reference.source_name,
            "source_participant_key": item.source_reference.source_participant_key,
            "competition_id": item.source_reference.competition_id,
            "canonical_participant_id": item.source_reference.canonical_participant_id,
            "participant_type": item.source_reference.participant_type,
            "display_name": item.source_reference.display_name,
            "normalized_name": item.source_reference.normalized_name,
            "schema_version": item.source_reference.schema_version,
        }
        for item in participants
    ]


def participant_reconciliation_rows(
    reconciliations: tuple[ParticipantReconciliation, ...],
) -> list[dict[str, Any]]:
    """Build participant reconciliation audit rows in deterministic order."""
    return [
        {
            "source_name": item.source_name,
            "source_participant_id": item.source_participant_id,
            "source_participant_key": item.source_participant_key,
            "canonical_participant_id": item.canonical_participant_id,
            "reconciliation_state": item.state,
            "reconciliation_confidence": item.confidence,
            "reconciliation_policy_version": item.policy_version,
            "match_key": item.match_key,
            "reason": item.reason,
            "source_observed_at_utc": item.source_observed_at_utc,
            "schema_version": item.schema_version,
        }
        for item in reconciliations
    ]


def event_rows(events: tuple[CanonicalEvent, ...]) -> list[dict[str, Any]]:
    """Build canonical event rows in source-independent deterministic order."""
    canonical_by_id = {item.canonical_event_id: item for item in events}
    return [
        {
            "canonical_event_id": item.canonical_event_id,
            "sport_code": item.sport_code,
            "competition_id": item.competition_id,
            "season_id": item.season_id,
            "event_occurrence_key": item.event_occurrence_key,
            "event_date": item.event_date,
            "scheduled_start_utc": item.scheduled_start_utc,
            "start_time_precision": item.start_time_precision,
            "status": item.status,
            "home_canonical_participant_id": item.home_canonical_participant_id,
            "away_canonical_participant_id": item.away_canonical_participant_id,
            "home_score": item.home_score,
            "away_score": item.away_score,
            "result_code": item.result_code,
            "outcome_availability_stage": item.outcome_availability_stage,
            "schema_version": item.schema_version,
        }
        for item in sorted(
            canonical_by_id.values(),
            key=lambda event: (
                event.event_date,
                event.event_occurrence_key,
                event.home_canonical_participant_id,
                event.away_canonical_participant_id,
                event.canonical_event_id,
            ),
        )
    ]


def source_event_rows(source_events: tuple[IngestedSourceEvent, ...]) -> list[dict[str, Any]]:
    """Build source event rows, including unresolved source events."""
    return [
        {
            "source_name": item.source_reference.source_name,
            "source_event_id": item.source_reference.source_event_id,
            "source_event_key": item.source_reference.source_event_key,
            "canonical_event_id": item.source_reference.canonical_event_id,
            "sport_code": item.sport_code,
            "competition_id": item.competition_id,
            "season_id": item.season_id,
            "event_occurrence_key": item.event_occurrence_key,
            "event_date": item.event_date,
            "scheduled_start_utc": item.scheduled_start_utc,
            "start_time_precision": item.start_time_precision,
            "status": item.status,
            "home_source_participant_id": item.home_source_participant_id,
            "away_source_participant_id": item.away_source_participant_id,
            "home_canonical_participant_id": item.home_canonical_participant_id,
            "away_canonical_participant_id": item.away_canonical_participant_id,
            "home_score": item.home_score,
            "away_score": item.away_score,
            "result_code": item.result_code,
            "outcome_availability_stage": item.outcome_availability_stage,
            "source_row_number": item.source_reference.source_row_number,
            "source_file_sha256": item.source_reference.source_file_sha256,
            "source_observed_at_utc": item.source_reference.source_observed_at_utc,
            "reconciliation_state": item.reconciliation.state,
            "reconciliation_confidence": item.reconciliation.confidence,
            "reconciliation_policy_version": item.reconciliation.policy_version,
            "reconciliation_reason": item.reconciliation.reason,
            "schema_version": item.schema_version,
        }
        for item in source_events
    ]


def event_reconciliation_rows(
    reconciliations: tuple[EventReconciliation, ...],
) -> list[dict[str, Any]]:
    """Build event reconciliation audit rows in deterministic order.

    Unresolved reconciliations are recorded here with a null ``canonical_event_id``
    and an explicit reason. They are deliberately absent from the events dataset so
    downstream readers cannot treat them as safe candidates.
    """
    return [
        {
            "source_name": item.source_name,
            "source_event_id": item.source_event_id,
            "source_event_key": item.source_event_key,
            "canonical_event_id": item.canonical_event_id,
            "reconciliation_state": item.state,
            "reconciliation_confidence": item.confidence,
            "reconciliation_policy_version": item.policy_version,
            "match_key": item.match_key,
            "reason": item.reason,
            "source_observed_at_utc": item.source_observed_at_utc,
            "schema_version": item.schema_version,
        }
        for item in reconciliations
    ]


def _canonical_participant_row(item: CanonicalParticipant) -> dict[str, Any]:
    return {
        "canonical_participant_id": item.canonical_participant_id,
        "sport_code": item.sport_code,
        "participant_identity_scope": item.participant_identity_scope,
        "participant_type": item.participant_type,
        "canonical_key": item.canonical_key,
        "display_name": item.display_name,
        "schema_version": item.schema_version,
    }
