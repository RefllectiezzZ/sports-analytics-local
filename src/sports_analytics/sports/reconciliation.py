"""Conservative, deterministic participant and event reconciliation.

Participant policy ``participant-reconciliation-v1``:

* ``exact`` — competition-scoped normalized name is known and unambiguous.
* ``probable`` / ``manual`` — reserved; never produced automatically here.
* ``unresolved`` — incomplete identity, conflicting duplicate source identities,
  or aliases without an explicit supported mapping.

Event policy ``event-reconciliation-v1``:

* ``exact`` — sport, competition, season, both canonical participants, and
  ``event_occurrence_key`` are known and unambiguous.
* Scheduled date and kickoff are evidence only; they do not form identity and
  differing dates across sources for the same occurrence do not block an exact
  match (postponements / reschedules).
* ``probable`` / ``manual`` — reserved.
* ``unresolved`` — incomplete identity, duplicate conflicting source identities,
  or ambiguous duplicate occurrences from one source.

Deliberately absent: fuzzy matching, scored heuristics, and silent alias merges.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from typing import Final

from sports_analytics.core.exceptions import SourceIntegrityError
from sports_analytics.sports.contracts import (
    EventReconciliation,
    ParticipantReconciliation,
    ReconciliationState,
    require_utc,
)
from sports_analytics.sports.identifiers import (
    build_canonical_event_id_from_key,
    build_canonical_event_key,
    build_canonical_participant_id_from_key,
    build_canonical_participant_key,
)

PARTICIPANT_RECONCILIATION_POLICY_VERSION: Final[str] = "participant-reconciliation-v1"
RECONCILIATION_POLICY_VERSION: Final[str] = "event-reconciliation-v1"

EXACT_CONFIDENCE: Final[float] = 1.0
UNRESOLVED_CONFIDENCE: Final[float] = 0.0


@dataclass(frozen=True, slots=True)
class ParticipantReconciliationCandidate:
    """One source's naming of a participant submitted for reconciliation."""

    source_name: str
    source_participant_id: str
    source_participant_key: str
    sport_code: str
    competition_id: str
    participant_type: str
    normalized_name: str | None
    display_name: str
    source_observed_at_utc: datetime
    schema_version: str
    #: Optional explicit alias target key. Without this, aliases stay unresolved.
    supported_alias_canonical_key: str | None = None


@dataclass(frozen=True, slots=True)
class ReconciliationCandidate:
    """One source's description of a fixture submitted for reconciliation."""

    source_name: str
    source_event_id: str
    source_event_key: str
    sport_code: str
    competition_id: str
    season_id: str
    event_occurrence_key: str | None
    event_date: date | None
    scheduled_start_utc: datetime | None
    home_canonical_participant_id: str | None
    away_canonical_participant_id: str | None
    source_observed_at_utc: datetime
    schema_version: str


def reconcile_participant_candidates(
    candidates: tuple[ParticipantReconciliationCandidate, ...],
    *,
    policy_version: str = PARTICIPANT_RECONCILIATION_POLICY_VERSION,
) -> tuple[ParticipantReconciliation, ...]:
    """Reconcile source participant candidates deterministically."""
    _reject_duplicate_participant_identities(candidates)

    incomplete: dict[tuple[str, str], str] = {}
    keys_by_candidate: dict[tuple[str, str], str] = {}
    grouped: dict[str, list[ParticipantReconciliationCandidate]] = defaultdict(list)

    for candidate in candidates:
        identity = (candidate.source_name, candidate.source_participant_id)
        reason = _incomplete_participant_reason(candidate)
        if reason is not None:
            incomplete[identity] = reason
            continue
        assert candidate.normalized_name is not None
        canonical_key = candidate.supported_alias_canonical_key or candidate.normalized_name
        match_key = build_canonical_participant_key(
            sport_code=candidate.sport_code,
            competition_id=candidate.competition_id,
            participant_type=candidate.participant_type,
            canonical_key=canonical_key,
        )
        keys_by_candidate[identity] = match_key
        grouped[match_key].append(candidate)

    conflicts = _conflicting_participant_match_keys(grouped)

    results: list[ParticipantReconciliation] = []
    for candidate in candidates:
        identity = (candidate.source_name, candidate.source_participant_id)
        observed = require_utc(
            candidate.source_observed_at_utc,
            field_name="source_observed_at_utc",
        )
        reason = incomplete.get(identity)
        if reason is not None:
            results.append(
                _unresolved_participant(
                    candidate,
                    observed=observed,
                    reason=reason,
                    policy=policy_version,
                )
            )
            continue
        match_key = keys_by_candidate[identity]
        conflict_reason = conflicts.get(match_key)
        if conflict_reason is not None:
            results.append(
                _unresolved_participant(
                    candidate,
                    observed=observed,
                    reason=conflict_reason,
                    policy=policy_version,
                )
            )
            continue
        results.append(
            ParticipantReconciliation(
                source_name=candidate.source_name,
                source_participant_id=candidate.source_participant_id,
                source_participant_key=candidate.source_participant_key,
                canonical_participant_id=build_canonical_participant_id_from_key(match_key),
                state=ReconciliationState.EXACT.value,
                confidence=EXACT_CONFIDENCE,
                policy_version=policy_version,
                match_key=match_key,
                reason=None,
                source_observed_at_utc=observed,
                schema_version=candidate.schema_version,
            )
        )

    return tuple(sorted(results, key=lambda item: (item.source_name, item.source_participant_id)))


def reconcile_candidates(
    candidates: tuple[ReconciliationCandidate, ...],
    *,
    policy_version: str = RECONCILIATION_POLICY_VERSION,
) -> tuple[EventReconciliation, ...]:
    """Reconcile source event candidates deterministically.

    Returns one reconciliation per candidate, ordered by
    ``(source_name, source_event_id)`` so repeated execution is byte-identical.
    """
    _reject_duplicate_event_identities(candidates)

    incomplete: dict[tuple[str, str], str] = {}
    keys_by_candidate: dict[tuple[str, str], str] = {}
    grouped: dict[str, list[ReconciliationCandidate]] = defaultdict(list)

    for candidate in candidates:
        identity = (candidate.source_name, candidate.source_event_id)
        reason = _incomplete_event_reason(candidate)
        if reason is not None:
            incomplete[identity] = reason
            continue
        assert candidate.event_occurrence_key is not None
        assert candidate.home_canonical_participant_id is not None
        assert candidate.away_canonical_participant_id is not None
        match_key = build_canonical_event_key(
            sport_code=candidate.sport_code,
            competition_id=candidate.competition_id,
            season_id=candidate.season_id,
            home_canonical_participant_id=candidate.home_canonical_participant_id,
            away_canonical_participant_id=candidate.away_canonical_participant_id,
            event_occurrence_key=candidate.event_occurrence_key,
        )
        keys_by_candidate[identity] = match_key
        grouped[match_key].append(candidate)

    conflicts = _conflicting_event_match_keys(grouped)

    results: list[EventReconciliation] = []
    for candidate in candidates:
        identity = (candidate.source_name, candidate.source_event_id)
        observed = require_utc(
            candidate.source_observed_at_utc,
            field_name="source_observed_at_utc",
        )
        reason = incomplete.get(identity)
        if reason is not None:
            results.append(
                _unresolved_event(
                    candidate,
                    observed=observed,
                    reason=reason,
                    policy=policy_version,
                )
            )
            continue
        match_key = keys_by_candidate[identity]
        conflict_reason = conflicts.get(match_key)
        if conflict_reason is not None:
            results.append(
                _unresolved_event(
                    candidate,
                    observed=observed,
                    reason=conflict_reason,
                    policy=policy_version,
                )
            )
            continue
        results.append(
            EventReconciliation(
                source_name=candidate.source_name,
                source_event_id=candidate.source_event_id,
                source_event_key=candidate.source_event_key,
                canonical_event_id=build_canonical_event_id_from_key(match_key),
                state=ReconciliationState.EXACT.value,
                confidence=EXACT_CONFIDENCE,
                policy_version=policy_version,
                match_key=match_key,
                reason=None,
                source_observed_at_utc=observed,
                schema_version=candidate.schema_version,
            )
        )

    return tuple(sorted(results, key=lambda item: (item.source_name, item.source_event_id)))


def _reject_duplicate_participant_identities(
    candidates: tuple[ParticipantReconciliationCandidate, ...],
) -> None:
    by_identity: dict[tuple[str, str], list[ParticipantReconciliationCandidate]] = defaultdict(list)
    for candidate in candidates:
        by_identity[(candidate.source_name, candidate.source_participant_id)].append(candidate)
    for identity, group in by_identity.items():
        if len(group) < 2:
            continue
        signatures = {
            (
                item.source_participant_key,
                item.competition_id,
                item.normalized_name,
                item.supported_alias_canonical_key,
            )
            for item in group
        }
        if len(signatures) > 1:
            msg = (
                f"conflicting duplicate source participant identity {identity[0]!r}/{identity[1]!r}"
            )
            raise SourceIntegrityError(msg)


def _reject_duplicate_event_identities(
    candidates: tuple[ReconciliationCandidate, ...],
) -> None:
    by_identity: dict[tuple[str, str], list[ReconciliationCandidate]] = defaultdict(list)
    for candidate in candidates:
        by_identity[(candidate.source_name, candidate.source_event_id)].append(candidate)
    for identity, group in by_identity.items():
        if len(group) < 2:
            continue
        signatures = {
            (
                item.source_event_key,
                item.event_occurrence_key,
                item.event_date,
                item.home_canonical_participant_id,
                item.away_canonical_participant_id,
            )
            for item in group
        }
        if len(signatures) > 1:
            msg = f"conflicting duplicate source event identity {identity[0]!r}/{identity[1]!r}"
            raise SourceIntegrityError(msg)


def _unresolved_participant(
    candidate: ParticipantReconciliationCandidate,
    *,
    observed: datetime,
    reason: str,
    policy: str,
) -> ParticipantReconciliation:
    return ParticipantReconciliation(
        source_name=candidate.source_name,
        source_participant_id=candidate.source_participant_id,
        source_participant_key=candidate.source_participant_key,
        canonical_participant_id=None,
        state=ReconciliationState.UNRESOLVED.value,
        confidence=UNRESOLVED_CONFIDENCE,
        policy_version=policy,
        match_key=None,
        reason=reason,
        source_observed_at_utc=observed,
        schema_version=candidate.schema_version,
    )


def _unresolved_event(
    candidate: ReconciliationCandidate,
    *,
    observed: datetime,
    reason: str,
    policy: str,
) -> EventReconciliation:
    return EventReconciliation(
        source_name=candidate.source_name,
        source_event_id=candidate.source_event_id,
        source_event_key=candidate.source_event_key,
        canonical_event_id=None,
        state=ReconciliationState.UNRESOLVED.value,
        confidence=UNRESOLVED_CONFIDENCE,
        policy_version=policy,
        match_key=None,
        reason=reason,
        source_observed_at_utc=observed,
        schema_version=candidate.schema_version,
    )


def _incomplete_participant_reason(
    candidate: ParticipantReconciliationCandidate,
) -> str | None:
    if not candidate.normalized_name:
        return "missing normalized participant name"
    return None


def unsupported_alias_reason(*, left_name: str, right_name: str) -> str:
    """Return the explicit unresolved reason for an unsupported alias pair."""
    return (
        f"unsupported participant alias without explicit mapping: {left_name!r} vs {right_name!r}"
    )


def _incomplete_event_reason(candidate: ReconciliationCandidate) -> str | None:
    if candidate.event_occurrence_key is None:
        return "missing event occurrence key"
    if candidate.home_canonical_participant_id is None:
        return "missing canonical home participant"
    if candidate.away_canonical_participant_id is None:
        return "missing canonical away participant"
    if candidate.home_canonical_participant_id == candidate.away_canonical_participant_id:
        return "identical canonical participants"
    return None


def _conflicting_participant_match_keys(
    grouped: dict[str, list[ParticipantReconciliationCandidate]],
) -> dict[str, str]:
    conflicts: dict[str, str] = {}
    for match_key, group in grouped.items():
        by_source: dict[str, int] = defaultdict(int)
        for candidate in group:
            by_source[candidate.source_name] += 1
        duplicated = sorted(name for name, count in by_source.items() if count > 1)
        if duplicated:
            conflicts[match_key] = (
                "ambiguous duplicate source participants for one canonical participant from "
                f"{', '.join(duplicated)}"
            )
    return conflicts


def _conflicting_event_match_keys(
    grouped: dict[str, list[ReconciliationCandidate]],
) -> dict[str, str]:
    """Return match keys that cannot be reconciled, with an explicit reason.

    Differing ``event_date`` / ``scheduled_start_utc`` values across sources for
    the same occurrence are treated as postponement/reschedule evidence and do
    not create a conflict.
    """
    conflicts: dict[str, str] = {}
    for match_key, group in grouped.items():
        by_source: dict[str, int] = defaultdict(int)
        for candidate in group:
            by_source[candidate.source_name] += 1
        duplicated = sorted(name for name, count in by_source.items() if count > 1)
        if duplicated:
            conflicts[match_key] = (
                "ambiguous duplicate source events for one canonical event from "
                f"{', '.join(duplicated)}"
            )
    return conflicts
