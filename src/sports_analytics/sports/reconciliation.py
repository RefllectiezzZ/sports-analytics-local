"""Conservative, deterministic cross-source event reconciliation.

Policy ``event-reconciliation-v1``:

* ``exact`` — every canonical identity component (sport, competition, season,
  event date, both canonical participants) is known and unambiguous, and no
  candidate contradicts another candidate for the same canonical event.
* ``probable`` — reserved by the contract for future scored matching. No
  automatic rule in this release produces it.
* ``manual`` — reserved by the contract for an operator-confirmed link. No
  workflow in this release produces it.
* ``unresolved`` — identity is incomplete, or two candidates conflict. Unresolved
  events are never merged and are excluded from downstream-safe consumption.

Deliberately absent: fuzzy name matching, scored heuristics, and any rule that
could silently merge unrelated events.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from typing import Final

from sports_analytics.sports.contracts import (
    EventReconciliation,
    ReconciliationState,
    require_utc,
)
from sports_analytics.sports.identifiers import (
    build_canonical_event_id_from_key,
    build_canonical_event_key,
)

RECONCILIATION_POLICY_VERSION: Final[str] = "event-reconciliation-v1"

EXACT_CONFIDENCE: Final[float] = 1.0
UNRESOLVED_CONFIDENCE: Final[float] = 0.0


@dataclass(frozen=True, slots=True)
class ReconciliationCandidate:
    """One source's description of a fixture submitted for reconciliation."""

    source_name: str
    source_event_id: str
    source_event_key: str
    sport_code: str
    competition_id: str
    season_id: str
    event_date: date | None
    scheduled_start_utc: datetime | None
    home_canonical_participant_id: str | None
    away_canonical_participant_id: str | None
    source_observed_at_utc: datetime
    schema_version: str


def reconcile_candidates(
    candidates: tuple[ReconciliationCandidate, ...],
    *,
    policy_version: str = RECONCILIATION_POLICY_VERSION,
) -> tuple[EventReconciliation, ...]:
    """Reconcile source event candidates deterministically.

    Returns one reconciliation per candidate, ordered by
    ``(source_name, source_event_id)`` so repeated execution is byte-identical.
    """
    incomplete: dict[tuple[str, str], str] = {}
    keys_by_candidate: dict[tuple[str, str], str] = {}
    grouped: dict[str, list[ReconciliationCandidate]] = defaultdict(list)

    for candidate in candidates:
        identity = (candidate.source_name, candidate.source_event_id)
        reason = _incomplete_reason(candidate)
        if reason is not None:
            incomplete[identity] = reason
            continue
        assert candidate.event_date is not None
        assert candidate.home_canonical_participant_id is not None
        assert candidate.away_canonical_participant_id is not None
        match_key = build_canonical_event_key(
            sport_code=candidate.sport_code,
            competition_id=candidate.competition_id,
            season_id=candidate.season_id,
            event_date=candidate.event_date,
            home_canonical_participant_id=candidate.home_canonical_participant_id,
            away_canonical_participant_id=candidate.away_canonical_participant_id,
        )
        keys_by_candidate[identity] = match_key
        grouped[match_key].append(candidate)

    conflicts = _conflicting_match_keys(grouped)

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
                _unresolved(candidate, observed=observed, reason=reason, policy=policy_version)
            )
            continue
        match_key = keys_by_candidate[identity]
        conflict_reason = conflicts.get(match_key)
        if conflict_reason is not None:
            results.append(
                _unresolved(
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


def _unresolved(
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


def _incomplete_reason(candidate: ReconciliationCandidate) -> str | None:
    if candidate.event_date is None:
        return "missing event date"
    if candidate.home_canonical_participant_id is None:
        return "missing canonical home participant"
    if candidate.away_canonical_participant_id is None:
        return "missing canonical away participant"
    if candidate.home_canonical_participant_id == candidate.away_canonical_participant_id:
        return "identical canonical participants"
    return None


def _conflicting_match_keys(
    grouped: dict[str, list[ReconciliationCandidate]],
) -> dict[str, str]:
    """Return match keys that cannot be reconciled, with an explicit reason."""
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
            continue
        starts = {
            require_utc(candidate.scheduled_start_utc, field_name="scheduled_start_utc")
            for candidate in group
            if candidate.scheduled_start_utc is not None
        }
        if len(starts) > 1:
            conflicts[match_key] = "conflicting scheduled start times across sources"
    return conflicts
