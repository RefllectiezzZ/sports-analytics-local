"""Deterministic resolution of mutable canonical event metadata.

When multiple downstream-safe source events map to one canonical event:

1. immutable identity dimensions must agree;
2. contradictory finished outcomes raise ``SourceIntegrityError``;
3. mutable scheduling/status metadata prefers the most recently observed valid
   state, then higher source authority, then lexicographic source name;
4. every original source fact remains in ``source_events``.
"""

from __future__ import annotations

from typing import Final

from sports_analytics.core.exceptions import SourceIntegrityError
from sports_analytics.data.codec import format_utc_timestamp
from sports_analytics.sports.contracts import (
    CanonicalEvent,
    EventStatus,
    IngestedEvent,
    IngestedSourceEvent,
    require_utc,
)

#: Higher values win when observation timestamps are equal.
DEFAULT_SOURCE_AUTHORITY: Final[dict[str, int]] = {
    "football-data-co-uk": 100,
}


def source_authority(source_name: str) -> int:
    """Return the deterministic authority rank for ``source_name``."""
    return DEFAULT_SOURCE_AUTHORITY.get(source_name, 0)


def resolve_canonical_events_from_sources(
    source_events: tuple[IngestedSourceEvent, ...],
) -> tuple[IngestedEvent, ...]:
    """Build unique canonical events from downstream-safe source events."""
    grouped: dict[str, list[IngestedSourceEvent]] = {}
    for source_event in source_events:
        canonical_event_id = source_event.reconciliation.canonical_event_id
        if not source_event.reconciliation.is_downstream_safe or canonical_event_id is None:
            continue
        grouped.setdefault(canonical_event_id, []).append(source_event)

    events: list[IngestedEvent] = []
    for group in grouped.values():
        _validate_immutable_identity_agreement(group)
        _reject_conflicting_finished_outcomes(group)
        selected = select_current_source_event(tuple(group))
        events.append(
            IngestedEvent(
                canonical=canonical_event_from_source(selected),
                reconciliation=selected.reconciliation,
            )
        )
    return tuple(sorted(events, key=_event_sort_key))


def select_current_source_event(
    source_events: tuple[IngestedSourceEvent, ...],
) -> IngestedSourceEvent:
    """Select the source event whose mutable metadata should populate the canonical."""
    if not source_events:
        msg = "cannot select canonical metadata from an empty source-event group"
        raise SourceIntegrityError(msg)
    selected = source_events[0]
    for candidate in source_events[1:]:
        if _is_preferred_metadata_source(candidate, selected):
            selected = candidate
    return selected


def canonical_event_from_source(source_event: IngestedSourceEvent) -> CanonicalEvent:
    """Project one selected source event into a canonical event record."""
    canonical_event_id = source_event.reconciliation.canonical_event_id
    if canonical_event_id is None:
        msg = "cannot build canonical event from unresolved source event"
        raise SourceIntegrityError(msg)
    if source_event.event_occurrence_key is None:
        msg = "resolved source event missing event occurrence key"
        raise SourceIntegrityError(msg)
    if source_event.event_date is None:
        msg = "resolved source event missing event date"
        raise SourceIntegrityError(msg)
    if (
        source_event.home_canonical_participant_id is None
        or source_event.away_canonical_participant_id is None
    ):
        msg = "resolved source event missing canonical participants"
        raise SourceIntegrityError(msg)
    return CanonicalEvent(
        canonical_event_id=canonical_event_id,
        sport_code=source_event.sport_code,
        competition_id=source_event.competition_id,
        season_id=source_event.season_id,
        event_occurrence_key=source_event.event_occurrence_key,
        event_date=source_event.event_date,
        scheduled_start_utc=source_event.scheduled_start_utc,
        start_time_precision=source_event.start_time_precision,
        status=source_event.status,
        home_canonical_participant_id=source_event.home_canonical_participant_id,
        away_canonical_participant_id=source_event.away_canonical_participant_id,
        home_score=source_event.home_score,
        away_score=source_event.away_score,
        result_code=source_event.result_code,
        outcome_availability_stage=source_event.outcome_availability_stage,
        schema_version=source_event.schema_version,
    )


def _is_preferred_metadata_source(
    candidate: IngestedSourceEvent,
    current: IngestedSourceEvent,
) -> bool:
    candidate_observed = require_utc(
        candidate.source_reference.source_observed_at_utc,
        field_name="source_observed_at_utc",
    )
    current_observed = require_utc(
        current.source_reference.source_observed_at_utc,
        field_name="source_observed_at_utc",
    )
    if candidate_observed != current_observed:
        return candidate_observed > current_observed
    candidate_authority = source_authority(candidate.source_reference.source_name)
    current_authority = source_authority(current.source_reference.source_name)
    if candidate_authority != current_authority:
        return candidate_authority > current_authority
    return candidate.source_reference.source_name < current.source_reference.source_name


def _validate_immutable_identity_agreement(group: list[IngestedSourceEvent]) -> None:
    signatures = {
        (
            item.sport_code,
            item.competition_id,
            item.season_id,
            item.event_occurrence_key,
            item.home_canonical_participant_id,
            item.away_canonical_participant_id,
            item.reconciliation.canonical_event_id,
        )
        for item in group
    }
    if len(signatures) > 1:
        msg = "downstream-safe source events disagree on immutable canonical identity dimensions"
        raise SourceIntegrityError(msg)


def _reject_conflicting_finished_outcomes(group: list[IngestedSourceEvent]) -> None:
    finished_outcomes = {
        (item.home_score, item.away_score, item.result_code)
        for item in group
        if item.status == EventStatus.FINISHED.value
    }
    if len(finished_outcomes) > 1:
        msg = "conflicting finished outcomes for one canonical event"
        raise SourceIntegrityError(msg)


def _event_sort_key(event: IngestedEvent) -> tuple[int, int, str, str, str, str]:
    scheduled = event.canonical.scheduled_start_utc
    return (
        event.canonical.event_date.toordinal(),
        1 if scheduled is None else 0,
        format_utc_timestamp(scheduled) if scheduled is not None else "",
        event.canonical.home_canonical_participant_id,
        event.canonical.away_canonical_participant_id,
        event.canonical.canonical_event_id,
    )
