"""Deterministic canonical and source-scoped entity identifiers.

Canonical identifiers are derived only from source-independent facts so the same
real-world participant or fixture receives one identity across every source.
Source-scoped identifiers deliberately include ``source_name`` and exist for
provenance and adapter tracing.

Canonical participant identity uses ``participant_identity_scope`` (association
or namespace), never ``competition_id``. Scheduled date and kickoff are excluded
from canonical event identity: they are mutable metadata. Distinct occurrences
between the same participants are separated by ``event_occurrence_key``.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Final

from sports_analytics.core.exceptions import NormalizationError
from sports_analytics.sports.contracts import (
    validate_canonical_key,
    validate_domain_identifier,
)

SPORT_FOOTBALL: Final[str] = "football"
SPORT_BASKETBALL: Final[str] = "basketball"
SPORT_TENNIS: Final[str] = "tennis"

# Project-owned UUIDv5 namespaces. Canonical and source namespaces are distinct so
# a source-scoped key can never collide with a canonical key.
CANONICAL_ENTITY_NAMESPACE: Final[uuid.UUID] = uuid.UUID("2b1f0f26-0f2a-5a7c-9a3e-6b9d1c4f7e21")
SOURCE_ENTITY_NAMESPACE: Final[uuid.UUID] = uuid.UUID("6f8a2c91-4d3e-5b7a-9c1d-2e4f6a8b0c1d")

#: Domestic-league occurrence discriminator for the single expected home meeting
#: of an ordered participant pair within one season. Cups, legs, replays, and
#: other sports must supply their own occurrence keys; this value is football-
#: league specific and is not a universal sport assumption.
FOOTBALL_DOMESTIC_HOME_OCCURRENCE_KEY: Final[str] = "season-ordered-pair-home-1"


def build_canonical_participant_key(
    *,
    sport_code: str,
    participant_identity_scope: str,
    participant_type: str,
    canonical_key: str,
) -> str:
    """Build a source-independent canonical participant key.

    ``participant_identity_scope`` is a stable association/namespace such as
    ``club:england``. It must not embed ``source_name``, ``competition_id``,
    season, or current membership.
    """
    validate_domain_identifier(sport_code, field_name="sport_code")
    validate_domain_identifier(
        participant_identity_scope,
        field_name="participant_identity_scope",
    )
    validate_domain_identifier(participant_type, field_name="participant_type")
    validate_canonical_key(canonical_key, field_name="canonical_key")
    return (
        f"participant|{sport_code}|{participant_identity_scope}|{participant_type}|{canonical_key}"
    )


def build_canonical_participant_id(
    *,
    sport_code: str,
    participant_identity_scope: str,
    participant_type: str,
    canonical_key: str,
) -> str:
    """Return a deterministic canonical participant UUIDv5 (never source-scoped)."""
    key = build_canonical_participant_key(
        sport_code=sport_code,
        participant_identity_scope=participant_identity_scope,
        participant_type=participant_type,
        canonical_key=canonical_key,
    )
    return str(uuid.uuid5(CANONICAL_ENTITY_NAMESPACE, key))


def build_canonical_participant_id_from_key(canonical_participant_key: str) -> str:
    """Return the canonical participant UUIDv5 for an already-built key."""
    if not canonical_participant_key.startswith("participant|"):
        msg = "canonical_participant_key must be built by build_canonical_participant_key"
        raise NormalizationError(msg)
    return str(uuid.uuid5(CANONICAL_ENTITY_NAMESPACE, canonical_participant_key))


def build_source_participant_key(
    *,
    source_name: str,
    sport_code: str,
    competition_id: str,
    normalized_name: str,
) -> str:
    """Build the source-scoped participant key retained for provenance."""
    validate_domain_identifier(source_name, field_name="source_name")
    validate_domain_identifier(sport_code, field_name="sport_code")
    validate_domain_identifier(competition_id, field_name="competition_id")
    if not normalized_name:
        msg = "normalized_name must be non-empty"
        raise NormalizationError(msg)
    return f"{source_name}|{sport_code}|{competition_id}|{normalized_name}"


def build_source_participant_id(*, source_participant_key: str) -> str:
    """Return a deterministic source-scoped participant UUIDv5."""
    if not source_participant_key:
        msg = "source_participant_key must be non-empty"
        raise NormalizationError(msg)
    return str(uuid.uuid5(SOURCE_ENTITY_NAMESPACE, f"participant|{source_participant_key}"))


def build_canonical_event_key(
    *,
    sport_code: str,
    competition_id: str,
    season_id: str,
    home_canonical_participant_id: str,
    away_canonical_participant_id: str,
    event_occurrence_key: str,
) -> str:
    """Build the source-independent canonical event key.

    The key deliberately excludes ``source_name``, ``event_date``, and kickoff
    time so postponed or rescheduled fixtures retain one canonical identity.
    Distinct meetings between the same participants are separated by
    ``event_occurrence_key`` (round, leg, or sport-specific occurrence).
    """
    validate_domain_identifier(sport_code, field_name="sport_code")
    validate_domain_identifier(competition_id, field_name="competition_id")
    validate_domain_identifier(season_id, field_name="season_id")
    validate_domain_identifier(event_occurrence_key, field_name="event_occurrence_key")
    if home_canonical_participant_id == away_canonical_participant_id:
        msg = "canonical event participants must differ"
        raise NormalizationError(msg)
    return (
        f"event|{sport_code}|{competition_id}|{season_id}|"
        f"{home_canonical_participant_id}|{away_canonical_participant_id}|"
        f"{event_occurrence_key}"
    )


def build_canonical_event_id(
    *,
    sport_code: str,
    competition_id: str,
    season_id: str,
    home_canonical_participant_id: str,
    away_canonical_participant_id: str,
    event_occurrence_key: str,
) -> str:
    """Return a deterministic canonical event UUIDv5 (never source-scoped)."""
    key = build_canonical_event_key(
        sport_code=sport_code,
        competition_id=competition_id,
        season_id=season_id,
        home_canonical_participant_id=home_canonical_participant_id,
        away_canonical_participant_id=away_canonical_participant_id,
        event_occurrence_key=event_occurrence_key,
    )
    return str(uuid.uuid5(CANONICAL_ENTITY_NAMESPACE, key))


def build_canonical_event_id_from_key(canonical_event_key: str) -> str:
    """Return the canonical event UUIDv5 for an already-built canonical key."""
    if not canonical_event_key.startswith("event|"):
        msg = "canonical_event_key must be built by build_canonical_event_key"
        raise NormalizationError(msg)
    return str(uuid.uuid5(CANONICAL_ENTITY_NAMESPACE, canonical_event_key))


def build_source_event_key(
    *,
    source_name: str,
    competition_id: str,
    season_id: str,
    event_date: date,
    home_source_participant_key: str,
    away_source_participant_key: str,
) -> str:
    """Build the source-scoped event key retained for provenance and tracing.

    Source event keys may remain schedule-dependent. That must not force the
    canonical event identity to change when a fixture is postponed.
    """
    validate_domain_identifier(source_name, field_name="source_name")
    validate_domain_identifier(competition_id, field_name="competition_id")
    validate_domain_identifier(season_id, field_name="season_id")
    if not isinstance(event_date, date):
        msg = "event_date must be a date"
        raise NormalizationError(msg)
    return (
        f"{source_name}|{competition_id}|{season_id}|{event_date.isoformat()}|"
        f"{home_source_participant_key}|{away_source_participant_key}"
    )


def build_source_event_id(*, source_event_key: str) -> str:
    """Return a deterministic source-scoped event UUIDv5."""
    if not source_event_key:
        msg = "source_event_key must be non-empty"
        raise NormalizationError(msg)
    return str(uuid.uuid5(SOURCE_ENTITY_NAMESPACE, f"event|{source_event_key}"))


def build_season_id(*, competition_id: str, label: str) -> str:
    """Return a stable season identifier from competition and canonical label."""
    return validate_domain_identifier(f"{competition_id}:{label}", field_name="season_id")
