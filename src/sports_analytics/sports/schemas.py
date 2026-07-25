"""Sport-agnostic canonical entity Arrow schemas and row builders.

Every schema is parameterized by ``schema_version`` and ``sport_code`` so no
sport-specific constant is hard-coded in shared code. Nullability is explicit:
required contract fields are ``nullable=False`` and optional fields document why
absence is permitted.
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
    CompetitionRecord,
    EventReconciliation,
    IngestedEvent,
    IngestedParticipant,
    SeasonRecord,
)

DATASET_COMPETITIONS: Final[str] = "competitions"
DATASET_SEASONS: Final[str] = "seasons"
DATASET_PARTICIPANTS: Final[str] = "participants"
DATASET_SOURCE_PARTICIPANTS: Final[str] = "source_participants"
DATASET_EVENTS: Final[str] = "events"
DATASET_EVENT_RECONCILIATIONS: Final[str] = "event_reconciliations"


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
            pa.field("canonical_participant_id", pa.string(), nullable=False),
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


def events_schema(*, schema_version: str, sport_code: str) -> pa.Schema:
    """Return the canonical events dataset schema.

    Nullable fields and the reason absence is permitted:

    ``scheduled_start_utc``
        the source only published a date, so no kickoff time exists;
    ``home_score`` / ``away_score`` / ``result_code``
        the event has not finished, so no outcome exists yet.
    """
    return pa.schema(
        [
            pa.field("canonical_event_id", pa.string(), nullable=False),
            pa.field("source_event_id", pa.string(), nullable=False),
            pa.field("source_name", dictionary_string(), nullable=False),
            pa.field("source_event_key", pa.string(), nullable=False),
            pa.field("source_row_number", pa.int32(), nullable=False),
            pa.field("sport_code", dictionary_string(), nullable=False),
            pa.field("competition_id", pa.string(), nullable=False),
            pa.field("season_id", pa.string(), nullable=False),
            pa.field("event_date", pa.date32(), nullable=False),
            pa.field("scheduled_start_utc", utc_timestamp(), nullable=True),
            pa.field("start_time_precision", dictionary_string(), nullable=False),
            pa.field("status", dictionary_string(), nullable=False),
            pa.field("home_canonical_participant_id", pa.string(), nullable=False),
            pa.field("away_canonical_participant_id", pa.string(), nullable=False),
            pa.field("home_source_participant_id", pa.string(), nullable=False),
            pa.field("away_source_participant_id", pa.string(), nullable=False),
            pa.field("home_score", pa.int16(), nullable=True),
            pa.field("away_score", pa.int16(), nullable=True),
            pa.field("result_code", dictionary_string(), nullable=True),
            pa.field("outcome_availability_stage", dictionary_string(), nullable=False),
            pa.field("reconciliation_state", dictionary_string(), nullable=False),
            pa.field("reconciliation_confidence", pa.float64(), nullable=False),
            pa.field("reconciliation_policy_version", dictionary_string(), nullable=False),
            pa.field("source_observed_at_utc", utc_timestamp(), nullable=False),
            pa.field("source_file_sha256", pa.string(), nullable=False),
            pa.field("schema_version", dictionary_string(), nullable=False),
        ],
        metadata=dataset_metadata(
            dataset_name=DATASET_EVENTS,
            schema_version=schema_version,
            domain=sport_code,
        ),
    )


def event_reconciliations_schema(*, schema_version: str, sport_code: str) -> pa.Schema:
    """Return the reconciliation audit dataset schema.

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
    """Build canonical participant rows in deterministic order."""
    return [
        {
            "canonical_participant_id": item.canonical.canonical_participant_id,
            "sport_code": item.canonical.sport_code,
            "participant_type": item.canonical.participant_type,
            "canonical_key": item.canonical.canonical_key,
            "display_name": item.canonical.display_name,
            "schema_version": item.canonical.schema_version,
        }
        for item in participants
    ]


def source_participant_rows(
    participants: tuple[IngestedParticipant, ...],
) -> list[dict[str, Any]]:
    """Build source participant reference rows in deterministic order."""
    return [
        {
            "source_participant_id": item.source_reference.source_participant_id,
            "source_name": item.source_reference.source_name,
            "source_participant_key": item.source_reference.source_participant_key,
            "canonical_participant_id": item.source_reference.canonical_participant_id,
            "participant_type": item.source_reference.participant_type,
            "display_name": item.source_reference.display_name,
            "normalized_name": item.source_reference.normalized_name,
            "schema_version": item.source_reference.schema_version,
        }
        for item in participants
    ]


def event_rows(
    events: tuple[IngestedEvent, ...],
    *,
    source_participant_ids: dict[str, str],
) -> list[dict[str, Any]]:
    """Build canonical event rows joined with source identity and reconciliation.

    ``source_participant_ids`` maps canonical participant id to the source
    participant id observed in this ingestion.
    """
    rows: list[dict[str, Any]] = []
    for item in events:
        canonical = item.canonical
        reference = item.source_reference
        reconciliation = item.reconciliation
        rows.append(
            {
                "canonical_event_id": canonical.canonical_event_id,
                "source_event_id": reference.source_event_id,
                "source_name": reference.source_name,
                "source_event_key": reference.source_event_key,
                "source_row_number": reference.source_row_number,
                "sport_code": canonical.sport_code,
                "competition_id": canonical.competition_id,
                "season_id": canonical.season_id,
                "event_date": canonical.event_date,
                "scheduled_start_utc": canonical.scheduled_start_utc,
                "start_time_precision": canonical.start_time_precision,
                "status": canonical.status,
                "home_canonical_participant_id": canonical.home_canonical_participant_id,
                "away_canonical_participant_id": canonical.away_canonical_participant_id,
                "home_source_participant_id": source_participant_ids[
                    canonical.home_canonical_participant_id
                ],
                "away_source_participant_id": source_participant_ids[
                    canonical.away_canonical_participant_id
                ],
                "home_score": canonical.home_score,
                "away_score": canonical.away_score,
                "result_code": canonical.result_code,
                "outcome_availability_stage": canonical.outcome_availability_stage,
                "reconciliation_state": reconciliation.state,
                "reconciliation_confidence": reconciliation.confidence,
                "reconciliation_policy_version": reconciliation.policy_version,
                "source_observed_at_utc": reference.source_observed_at_utc,
                "source_file_sha256": reference.source_file_sha256,
                "schema_version": canonical.schema_version,
            }
        )
    return rows


def event_reconciliation_rows(
    reconciliations: tuple[EventReconciliation, ...],
) -> list[dict[str, Any]]:
    """Build reconciliation audit rows in deterministic order.

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
