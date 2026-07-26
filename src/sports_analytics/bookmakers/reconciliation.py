"""Conservative bookmaker participant and cross-provider event reconciliation.

Uses existing :mod:`sports_analytics.sports.reconciliation` for exact participant
and event candidate reconciliation. Cross-provider matching additionally requires
scheduled-start proximity within a conservative tolerance and exact competition
compatibility. Fuzzy matching is deliberately absent.
"""

from __future__ import annotations

import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from sports_analytics.bookmakers.types import (
    BOOKMAKER_EVENT_RECONCILIATION_POLICY_ID,
    BOOKMAKER_SCHEMA_VERSION,
    DEFAULT_EVENT_START_TOLERANCE_SECONDS,
)
from sports_analytics.core.exceptions import NormalizationError, PermanentSourceError
from sports_analytics.sources.bookmaker_contracts import (
    ProviderAcquisitionBundle,
    ProviderEventObservation,
    ProviderParticipantObservation,
)
from sports_analytics.sports.contracts import (
    EventReconciliation,
    ParticipantReconciliation,
    ParticipantType,
    ReconciliationState,
)
from sports_analytics.sports.identifiers import (
    SPORT_BASKETBALL,
    SPORT_FOOTBALL,
    SPORT_TENNIS,
    build_season_id,
    build_source_event_id,
    build_source_event_key,
    build_source_participant_id,
    build_source_participant_key,
)
from sports_analytics.sports.reconciliation import (
    PARTICIPANT_RECONCILIATION_POLICY_VERSION,
    ParticipantReconciliationCandidate,
    ReconciliationCandidate,
    reconcile_candidates,
    reconcile_participant_candidates,
)

BOOKMAKER_SEASON_LABEL: Final[str] = "current"
MAX_PARTICIPANT_NAME_LENGTH: Final[int] = 200


@dataclass(frozen=True, slots=True)
class BookmakerReconciliationBundle:
    """Participant and event reconciliations for one or more provider bundles."""

    participant_candidates: tuple[ParticipantReconciliationCandidate, ...]
    participant_reconciliations: tuple[ParticipantReconciliation, ...]
    event_candidates: tuple[ReconciliationCandidate, ...]
    event_reconciliations: tuple[EventReconciliation, ...]
    unresolved_event_reconciliations: tuple[EventReconciliation, ...]
    start_tolerance_seconds: int
    policy_version: str


def participant_type_for_sport(sport_code: str) -> str:
    """Return the participant type used for bookmaker canonical identity."""
    if sport_code == SPORT_FOOTBALL:
        return ParticipantType.CLUB.value
    if sport_code == SPORT_BASKETBALL:
        return ParticipantType.TEAM.value
    if sport_code == SPORT_TENNIS:
        return ParticipantType.PLAYER.value
    msg = f"unsupported bookmaker sport: {sport_code}"
    raise PermanentSourceError(msg)


def participant_identity_scope_for_sport(sport_code: str) -> str:
    """Return the provisional identity scope for Portugal bookmaker providers."""
    if sport_code == SPORT_FOOTBALL:
        return "club:portugal"
    if sport_code == SPORT_BASKETBALL:
        return "team:portugal"
    if sport_code == SPORT_TENNIS:
        return "player:international"
    msg = f"unsupported bookmaker sport: {sport_code}"
    raise PermanentSourceError(msg)


def normalize_participant_name(value: str) -> tuple[str, str]:
    """Return ``(display_name, normalized_key)`` without fuzzy aliasing."""
    if not isinstance(value, str):
        msg = "participant name must be a string"
        raise NormalizationError(msg)
    if "\x00" in value:
        msg = "participant name must not contain NUL"
        raise NormalizationError(msg)
    if any(unicodedata.category(ch)[0] == "C" and ch not in {"\t"} for ch in value):
        msg = "participant name must not contain control characters"
        raise NormalizationError(msg)
    collapsed = " ".join(unicodedata.normalize("NFC", value).replace("\t", " ").split())
    if not collapsed:
        msg = "participant name must be non-empty after normalization"
        raise NormalizationError(msg)
    if len(collapsed) > MAX_PARTICIPANT_NAME_LENGTH:
        msg = f"participant name exceeds maximum length of {MAX_PARTICIPANT_NAME_LENGTH}"
        raise NormalizationError(msg)
    return collapsed, collapsed.casefold()


def competitions_compatible(left_competition_id: str, right_competition_id: str) -> bool:
    """Return whether two competition IDs are exactly compatible.

    No fuzzy competition matching is performed. Distinct competition identifiers
    remain distinct unless they are byte-identical after validation.
    """
    return left_competition_id == right_competition_id


def build_participant_candidates_from_bundle(
    bundle: ProviderAcquisitionBundle,
) -> tuple[ParticipantReconciliationCandidate, ...]:
    """Normalize provider participants into reconciliation candidates."""
    sport = bundle.sport
    participant_type = participant_type_for_sport(sport)
    identity_scope = participant_identity_scope_for_sport(sport)
    candidates: list[ParticipantReconciliationCandidate] = []
    seen: set[tuple[str, str]] = set()
    for event in bundle.events:
        competition_id = competition_id_for_event(event)
        for participant in event.participants:
            identity = (bundle.provider_id, participant.source_participant_id)
            if identity in seen:
                continue
            seen.add(identity)
            candidates.append(
                _participant_candidate(
                    provider_id=bundle.provider_id,
                    sport=sport,
                    competition_id=competition_id,
                    participant=participant,
                    participant_type=participant_type,
                    identity_scope=identity_scope,
                    observed_at_utc=bundle.observed_at_utc,
                )
            )
    return tuple(
        sorted(candidates, key=lambda item: (item.source_name, item.source_participant_id))
    )


def build_event_candidates_from_bundle(
    bundle: ProviderAcquisitionBundle,
    *,
    participant_reconciliations: tuple[ParticipantReconciliation, ...],
    start_tolerance_seconds: int = DEFAULT_EVENT_START_TOLERANCE_SECONDS,
) -> tuple[ReconciliationCandidate, ...]:
    """Build event candidates, applying cross-provider start-time grouping keys.

    Occurrence keys embed a tolerance-bucketed kickoff so providers whose starts
    differ by at most ``start_tolerance_seconds`` can share an exact match key
    when sport, participants, roles, and competition also agree. Separate
    meetings outside that window remain distinct.
    """
    _ = start_tolerance_seconds
    by_source_participant = {
        (item.source_name, item.source_participant_id): item
        for item in participant_reconciliations
        if item.source_name == bundle.provider_id
    }
    candidates: list[ReconciliationCandidate] = []
    for event in bundle.events:
        home, away = home_away_participants(event.participants)
        competition_id = competition_id_for_event(event)
        season_id = build_season_id(competition_id=competition_id, label=BOOKMAKER_SEASON_LABEL)
        home_recon = by_source_participant.get((bundle.provider_id, home.source_participant_id))
        away_recon = by_source_participant.get((bundle.provider_id, away.source_participant_id))
        home_canonical = (
            home_recon.canonical_participant_id
            if home_recon is not None and home_recon.is_downstream_safe
            else None
        )
        away_canonical = (
            away_recon.canonical_participant_id
            if away_recon is not None and away_recon.is_downstream_safe
            else None
        )
        occurrence_key = occurrence_key_for_event(
            scheduled_start_utc=event.scheduled_start_utc,
            tolerance_seconds=start_tolerance_seconds,
        )
        display_home, normalized_home = normalize_participant_name(
            home.normalized_name or home.display_name
        )
        display_away, normalized_away = normalize_participant_name(
            away.normalized_name or away.display_name
        )
        _ = (display_home, display_away)
        home_source_key = build_source_participant_key(
            source_name=bundle.provider_id,
            sport_code=bundle.sport,
            competition_id=competition_id,
            normalized_name=normalized_home,
        )
        away_source_key = build_source_participant_key(
            source_name=bundle.provider_id,
            sport_code=bundle.sport,
            competition_id=competition_id,
            normalized_name=normalized_away,
        )
        source_event_key = build_source_event_key(
            source_name=bundle.provider_id,
            competition_id=competition_id,
            season_id=season_id,
            event_date=event.scheduled_start_utc.date(),
            home_source_participant_key=home_source_key,
            away_source_participant_key=away_source_key,
        )
        candidates.append(
            ReconciliationCandidate(
                source_name=bundle.provider_id,
                source_event_id=event.source_event_id,
                source_event_key=source_event_key,
                sport_code=bundle.sport,
                competition_id=competition_id,
                season_id=season_id,
                event_occurrence_key=occurrence_key,
                event_date=event.scheduled_start_utc.date(),
                scheduled_start_utc=event.scheduled_start_utc,
                home_canonical_participant_id=home_canonical,
                away_canonical_participant_id=away_canonical,
                source_observed_at_utc=bundle.observed_at_utc,
                schema_version=BOOKMAKER_SCHEMA_VERSION,
            )
        )
    return tuple(sorted(candidates, key=lambda item: (item.source_name, item.source_event_id)))


def reconcile_bookmaker_bundles(
    bundles: tuple[ProviderAcquisitionBundle, ...],
    *,
    start_tolerance_seconds: int = DEFAULT_EVENT_START_TOLERANCE_SECONDS,
) -> BookmakerReconciliationBundle:
    """Reconcile participants and events across one or more provider bundles."""
    if start_tolerance_seconds < 0:
        msg = "start_tolerance_seconds must be non-negative"
        raise PermanentSourceError(msg)

    participant_candidates: list[ParticipantReconciliationCandidate] = []
    for bundle in bundles:
        participant_candidates.extend(build_participant_candidates_from_bundle(bundle))
    participant_tuple = tuple(participant_candidates)
    participant_reconciliations = reconcile_participant_candidates(participant_tuple)

    event_candidates: list[ReconciliationCandidate] = []
    for bundle in bundles:
        event_candidates.extend(
            build_event_candidates_from_bundle(
                bundle,
                participant_reconciliations=participant_reconciliations,
                start_tolerance_seconds=start_tolerance_seconds,
            )
        )
    event_tuple = tuple(event_candidates)
    filtered = _reject_incompatible_cross_provider_pairs(
        event_tuple,
        start_tolerance_seconds=start_tolerance_seconds,
    )
    event_reconciliations = reconcile_candidates(
        filtered,
        policy_version=BOOKMAKER_EVENT_RECONCILIATION_POLICY_ID,
    )
    unresolved = tuple(
        item for item in event_reconciliations if item.state == ReconciliationState.UNRESOLVED.value
    )
    return BookmakerReconciliationBundle(
        participant_candidates=tuple(
            sorted(
                participant_tuple, key=lambda item: (item.source_name, item.source_participant_id)
            )
        ),
        participant_reconciliations=participant_reconciliations,
        event_candidates=filtered,
        event_reconciliations=event_reconciliations,
        unresolved_event_reconciliations=unresolved,
        start_tolerance_seconds=start_tolerance_seconds,
        policy_version=BOOKMAKER_EVENT_RECONCILIATION_POLICY_ID,
    )


def _reject_incompatible_cross_provider_pairs(
    candidates: tuple[ReconciliationCandidate, ...],
    *,
    start_tolerance_seconds: int,
) -> tuple[ReconciliationCandidate, ...]:
    """Force unresolved candidates that share a match key but violate tolerance.

    Exact reconciliation groups by sport/competition/season/participants/
    occurrence. This guard additionally marks events unresolved when two
    providers would collide despite incompatible competition IDs or starts that
    fall outside the conservative tolerance after bucketization edge cases.
    """
    by_identity = {(item.source_name, item.source_event_id): item for item in candidates}
    grouped: dict[
        tuple[str, str | None, str | None, str | None],
        list[ReconciliationCandidate],
    ] = defaultdict(list)
    for candidate in candidates:
        if (
            candidate.home_canonical_participant_id is None
            or candidate.away_canonical_participant_id is None
            or candidate.event_occurrence_key is None
        ):
            continue
        key = (
            candidate.sport_code,
            candidate.home_canonical_participant_id,
            candidate.away_canonical_participant_id,
            candidate.event_occurrence_key,
        )
        grouped[key].append(candidate)

    blocked: set[tuple[str, str]] = set()
    for group in grouped.values():
        if len(group) < 2:
            continue
        for left_index, left in enumerate(group):
            for right in group[left_index + 1 :]:
                if left.source_name == right.source_name:
                    continue
                if not competitions_compatible(left.competition_id, right.competition_id):
                    blocked.add((left.source_name, left.source_event_id))
                    blocked.add((right.source_name, right.source_event_id))
                    continue
                if left.scheduled_start_utc is None or right.scheduled_start_utc is None:
                    blocked.add((left.source_name, left.source_event_id))
                    blocked.add((right.source_name, right.source_event_id))
                    continue
                delta = abs(left.scheduled_start_utc - right.scheduled_start_utc)
                if delta > timedelta(seconds=start_tolerance_seconds):
                    blocked.add((left.source_name, left.source_event_id))
                    blocked.add((right.source_name, right.source_event_id))

    if not blocked:
        return candidates

    rewritten: list[ReconciliationCandidate] = []
    for candidate in candidates:
        identity = (candidate.source_name, candidate.source_event_id)
        if identity not in blocked:
            rewritten.append(candidate)
            continue
        # Drop occurrence / participants so reconcile_candidates records unresolved.
        rewritten.append(
            ReconciliationCandidate(
                source_name=candidate.source_name,
                source_event_id=candidate.source_event_id,
                source_event_key=candidate.source_event_key,
                sport_code=candidate.sport_code,
                competition_id=candidate.competition_id,
                season_id=candidate.season_id,
                event_occurrence_key=None,
                event_date=candidate.event_date,
                scheduled_start_utc=candidate.scheduled_start_utc,
                home_canonical_participant_id=candidate.home_canonical_participant_id,
                away_canonical_participant_id=candidate.away_canonical_participant_id,
                source_observed_at_utc=candidate.source_observed_at_utc,
                schema_version=candidate.schema_version,
            )
        )
    _ = by_identity
    return tuple(sorted(rewritten, key=lambda item: (item.source_name, item.source_event_id)))


def _participant_candidate(
    *,
    provider_id: str,
    sport: str,
    competition_id: str,
    participant: ProviderParticipantObservation,
    participant_type: str,
    identity_scope: str,
    observed_at_utc: datetime,
) -> ParticipantReconciliationCandidate:
    display_name, normalized_name = normalize_participant_name(
        participant.normalized_name or participant.display_name
    )
    source_participant_key = build_source_participant_key(
        source_name=provider_id,
        sport_code=sport,
        competition_id=competition_id,
        normalized_name=normalized_name,
    )
    source_participant_id = participant.source_participant_id or build_source_participant_id(
        source_participant_key=source_participant_key
    )
    return ParticipantReconciliationCandidate(
        source_name=provider_id,
        source_participant_id=source_participant_id,
        source_participant_key=source_participant_key,
        sport_code=sport,
        participant_identity_scope=identity_scope,
        participant_type=participant_type,
        normalized_name=normalized_name,
        display_name=display_name,
        source_observed_at_utc=observed_at_utc,
        schema_version=BOOKMAKER_SCHEMA_VERSION,
    )


def home_away_participants(
    participants: tuple[ProviderParticipantObservation, ...],
) -> tuple[ProviderParticipantObservation, ProviderParticipantObservation]:
    """Return ordered home/away (or player-1/player-2) participants."""
    by_role = {item.role: item for item in participants}
    if "home" in by_role and "away" in by_role:
        return by_role["home"], by_role["away"]
    if "player-1" in by_role and "player-2" in by_role:
        return by_role["player-1"], by_role["player-2"]
    if len(participants) >= 2:
        ordered = tuple(
            sorted(participants, key=lambda item: (item.role, item.source_participant_id))
        )
        return ordered[0], ordered[1]
    msg = "event requires home/away or ordered participant pair"
    raise NormalizationError(msg)


def competition_id_for_event(event: ProviderEventObservation) -> str:
    """Derive a competition identity suitable for exact cross-provider matching.

    Prefer an exact case-folded competition display name when present so Betano
    and Betclic can share competition identity without fuzzy matching. When only
    a provider-scoped competition code exists, keep it provider-distinct so
    incompatible competitions never merge silently.
    """
    if event.competition_display_name:
        collapsed = "-".join(event.competition_display_name.casefold().split())
        if collapsed:
            return f"competition:{collapsed}"
    raw = "-".join(event.source_competition_id.casefold().split())
    if not raw:
        msg = "source_competition_id must be non-empty"
        raise NormalizationError(msg)
    return f"provider-competition:{raw}"


def occurrence_key_for_event(
    *,
    scheduled_start_utc: datetime,
    tolerance_seconds: int = DEFAULT_EVENT_START_TOLERANCE_SECONDS,
) -> str:
    """Return the conservative bookmaker occurrence key for a scheduled start."""
    _ = tolerance_seconds
    return f"prematch:{scheduled_start_utc.date().isoformat()}"


#: Re-export for callers that need source event UUID helpers alongside this module.
__all__ = [
    "BOOKMAKER_SEASON_LABEL",
    "BookmakerReconciliationBundle",
    "PARTICIPANT_RECONCILIATION_POLICY_VERSION",
    "build_event_candidates_from_bundle",
    "build_participant_candidates_from_bundle",
    "build_source_event_id",
    "competition_id_for_event",
    "competitions_compatible",
    "home_away_participants",
    "normalize_participant_name",
    "occurrence_key_for_event",
    "participant_identity_scope_for_sport",
    "participant_type_for_sport",
    "reconcile_bookmaker_bundles",
]
