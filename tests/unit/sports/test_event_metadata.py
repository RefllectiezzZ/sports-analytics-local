"""Canonical event metadata resolution from downstream-safe source events."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from sports_analytics.core.exceptions import SourceIntegrityError
from sports_analytics.sports.contracts import (
    EventReconciliation,
    EventStatus,
    IngestedSourceEvent,
    OutcomeAvailability,
    ReconciliationState,
    SourceEventReference,
    StartTimePrecision,
)
from sports_analytics.sports.event_metadata import resolve_canonical_events_from_sources
from sports_analytics.sports.football.identifiers import football_club_identity_scope
from sports_analytics.sports.identifiers import (
    FOOTBALL_DOMESTIC_HOME_OCCURRENCE_KEY,
    SPORT_FOOTBALL,
    build_canonical_event_id,
    build_canonical_event_key,
    build_canonical_participant_id,
    build_season_id,
    build_source_event_id,
    build_source_event_key,
    build_source_participant_id,
    build_source_participant_key,
)
from sports_analytics.sports.reconciliation import RECONCILIATION_POLICY_VERSION

COMPETITION_ID = "eng-premier-league"
SEASON_ID = build_season_id(competition_id=COMPETITION_ID, label="2023-2024")
SCHEMA_VERSION = "football-canonical-v2"
SOURCE_FILE_SHA256 = "e" * 64
HOME_KEY = "northbridge fc"
AWAY_KEY = "southport athletic"
HOME_CANONICAL_ID = build_canonical_participant_id(
    sport_code=SPORT_FOOTBALL,
    participant_identity_scope=football_club_identity_scope("ENG"),
    participant_type="club",
    canonical_key=HOME_KEY,
)
AWAY_CANONICAL_ID = build_canonical_participant_id(
    sport_code=SPORT_FOOTBALL,
    participant_identity_scope=football_club_identity_scope("ENG"),
    participant_type="club",
    canonical_key=AWAY_KEY,
)
CANONICAL_EVENT_ID = build_canonical_event_id(
    sport_code=SPORT_FOOTBALL,
    competition_id=COMPETITION_ID,
    season_id=SEASON_ID,
    home_canonical_participant_id=HOME_CANONICAL_ID,
    away_canonical_participant_id=AWAY_CANONICAL_ID,
    event_occurrence_key=FOOTBALL_DOMESTIC_HOME_OCCURRENCE_KEY,
)
CANONICAL_EVENT_KEY = build_canonical_event_key(
    sport_code=SPORT_FOOTBALL,
    competition_id=COMPETITION_ID,
    season_id=SEASON_ID,
    home_canonical_participant_id=HOME_CANONICAL_ID,
    away_canonical_participant_id=AWAY_CANONICAL_ID,
    event_occurrence_key=FOOTBALL_DOMESTIC_HOME_OCCURRENCE_KEY,
)


def _source_event(
    *,
    source_name: str,
    observed: datetime,
    event_date: date,
    scheduled_start_utc: datetime,
    status: str = EventStatus.SCHEDULED.value,
    home_score: int | None = None,
    away_score: int | None = None,
    result_code: str | None = None,
    outcome_availability_stage: str = OutcomeAvailability.PRE_EVENT_UNAVAILABLE.value,
    source_row_number: int = 2,
) -> IngestedSourceEvent:
    home_source_key = build_source_participant_key(
        source_name=source_name,
        sport_code=SPORT_FOOTBALL,
        competition_id=COMPETITION_ID,
        normalized_name=HOME_KEY,
    )
    away_source_key = build_source_participant_key(
        source_name=source_name,
        sport_code=SPORT_FOOTBALL,
        competition_id=COMPETITION_ID,
        normalized_name=AWAY_KEY,
    )
    source_event_key = build_source_event_key(
        source_name=source_name,
        competition_id=COMPETITION_ID,
        season_id=SEASON_ID,
        event_date=event_date,
        home_source_participant_key=home_source_key,
        away_source_participant_key=away_source_key,
    )
    source_event_id = build_source_event_id(source_event_key=source_event_key)
    reconciliation = EventReconciliation(
        source_name=source_name,
        source_event_id=source_event_id,
        source_event_key=source_event_key,
        canonical_event_id=CANONICAL_EVENT_ID,
        state=ReconciliationState.EXACT.value,
        confidence=1.0,
        policy_version=RECONCILIATION_POLICY_VERSION,
        match_key=CANONICAL_EVENT_KEY,
        reason=None,
        source_observed_at_utc=observed,
        schema_version=SCHEMA_VERSION,
    )
    source_reference = SourceEventReference(
        source_event_id=source_event_id,
        source_name=source_name,
        source_event_key=source_event_key,
        canonical_event_id=CANONICAL_EVENT_ID,
        source_row_number=source_row_number,
        source_observed_at_utc=observed,
        source_file_sha256=SOURCE_FILE_SHA256,
        schema_version=SCHEMA_VERSION,
    )
    return IngestedSourceEvent(
        source_reference=source_reference,
        reconciliation=reconciliation,
        sport_code=SPORT_FOOTBALL,
        competition_id=COMPETITION_ID,
        season_id=SEASON_ID,
        event_occurrence_key=FOOTBALL_DOMESTIC_HOME_OCCURRENCE_KEY,
        event_date=event_date,
        scheduled_start_utc=scheduled_start_utc,
        start_time_precision=StartTimePrecision.MINUTE.value,
        status=status,
        home_source_participant_id=build_source_participant_id(
            source_participant_key=home_source_key
        ),
        away_source_participant_id=build_source_participant_id(
            source_participant_key=away_source_key
        ),
        home_canonical_participant_id=HOME_CANONICAL_ID,
        away_canonical_participant_id=AWAY_CANONICAL_ID,
        home_score=home_score,
        away_score=away_score,
        result_code=result_code,
        outcome_availability_stage=outcome_availability_stage,
        schema_version=SCHEMA_VERSION,
    )


def test_newer_reschedule_selects_tuesday_metadata_without_changing_identity() -> None:
    saturday = _source_event(
        source_name="fixture-source-alpha",
        observed=datetime(2024, 1, 15, 12, 0, tzinfo=UTC),
        event_date=date(2023, 8, 12),
        scheduled_start_utc=datetime(2023, 8, 12, 14, 0, tzinfo=UTC),
        source_row_number=2,
    )
    tuesday = _source_event(
        source_name="fixture-source-beta",
        observed=datetime(2024, 1, 16, 12, 0, tzinfo=UTC),
        event_date=date(2023, 8, 15),
        scheduled_start_utc=datetime(2023, 8, 15, 19, 0, tzinfo=UTC),
        source_row_number=3,
    )

    (event,) = resolve_canonical_events_from_sources((saturday, tuesday))

    assert {
        saturday.reconciliation.canonical_event_id,
        tuesday.reconciliation.canonical_event_id,
    } == {CANONICAL_EVENT_ID}
    assert event.canonical.canonical_event_id == CANONICAL_EVENT_ID
    assert event.canonical.event_date == date(2023, 8, 15)
    assert event.canonical.scheduled_start_utc == datetime(2023, 8, 15, 19, 0, tzinfo=UTC)
    assert [item.event_date for item in (saturday, tuesday)] == [
        date(2023, 8, 12),
        date(2023, 8, 15),
    ]
    assert [item.scheduled_start_utc for item in (saturday, tuesday)] == [
        datetime(2023, 8, 12, 14, 0, tzinfo=UTC),
        datetime(2023, 8, 15, 19, 0, tzinfo=UTC),
    ]


def test_conflicting_finished_outcomes_raise_source_integrity_error() -> None:
    alpha = _source_event(
        source_name="fixture-source-alpha",
        observed=datetime(2024, 1, 15, 12, 0, tzinfo=UTC),
        event_date=date(2023, 8, 12),
        scheduled_start_utc=datetime(2023, 8, 12, 14, 0, tzinfo=UTC),
        status=EventStatus.FINISHED.value,
        home_score=2,
        away_score=1,
        result_code="home",
        outcome_availability_stage=OutcomeAvailability.POST_EVENT.value,
    )
    beta = _source_event(
        source_name="fixture-source-beta",
        observed=datetime(2024, 1, 16, 12, 0, tzinfo=UTC),
        event_date=date(2023, 8, 12),
        scheduled_start_utc=datetime(2023, 8, 12, 14, 0, tzinfo=UTC),
        status=EventStatus.FINISHED.value,
        home_score=1,
        away_score=1,
        result_code="draw",
        outcome_availability_stage=OutcomeAvailability.POST_EVENT.value,
    )

    with pytest.raises(SourceIntegrityError, match="conflicting finished outcomes"):
        resolve_canonical_events_from_sources((alpha, beta))


def test_equal_recency_prefers_source_authority_then_lexicographic_source_name() -> None:
    observed = datetime(2024, 1, 15, 12, 0, tzinfo=UTC)
    low_authority = _source_event(
        source_name="zz-low-priority",
        observed=observed,
        event_date=date(2023, 8, 12),
        scheduled_start_utc=datetime(2023, 8, 12, 14, 0, tzinfo=UTC),
    )
    football_data = _source_event(
        source_name="football-data-co-uk",
        observed=observed,
        event_date=date(2023, 8, 13),
        scheduled_start_utc=datetime(2023, 8, 13, 15, 0, tzinfo=UTC),
    )
    beta = _source_event(
        source_name="beta-source",
        observed=observed,
        event_date=date(2023, 8, 14),
        scheduled_start_utc=datetime(2023, 8, 14, 16, 0, tzinfo=UTC),
    )
    alpha = _source_event(
        source_name="alpha-source",
        observed=observed,
        event_date=date(2023, 8, 15),
        scheduled_start_utc=datetime(2023, 8, 15, 17, 0, tzinfo=UTC),
    )

    (authority_winner,) = resolve_canonical_events_from_sources((low_authority, football_data))
    (name_winner,) = resolve_canonical_events_from_sources((beta, alpha))

    assert authority_winner.reconciliation.source_name == "football-data-co-uk"
    assert authority_winner.canonical.event_date == date(2023, 8, 13)
    assert name_winner.reconciliation.source_name == "alpha-source"
    assert name_winner.canonical.event_date == date(2023, 8, 15)


def test_newer_scheduled_observation_cannot_downgrade_established_finished_result() -> None:
    finished = _source_event(
        source_name="fixture-source-alpha",
        observed=datetime(2024, 1, 15, 12, 0, tzinfo=UTC),
        event_date=date(2023, 8, 12),
        scheduled_start_utc=datetime(2023, 8, 12, 14, 0, tzinfo=UTC),
        status=EventStatus.FINISHED.value,
        home_score=2,
        away_score=1,
        result_code="home",
        outcome_availability_stage=OutcomeAvailability.POST_EVENT.value,
    )
    scheduled = _source_event(
        source_name="fixture-source-beta",
        observed=datetime(2024, 1, 16, 12, 0, tzinfo=UTC),
        event_date=date(2023, 8, 12),
        scheduled_start_utc=datetime(2023, 8, 12, 14, 0, tzinfo=UTC),
        status=EventStatus.SCHEDULED.value,
        source_row_number=3,
    )

    (event,) = resolve_canonical_events_from_sources((finished, scheduled))

    assert event.canonical.canonical_event_id == CANONICAL_EVENT_ID
    assert event.canonical.status == EventStatus.FINISHED.value
    assert event.canonical.home_score == 2
    assert event.canonical.away_score == 1
    assert event.canonical.result_code == "home"
    assert event.canonical.outcome_availability_stage == OutcomeAvailability.POST_EVENT.value
    assert event.reconciliation.source_name == "fixture-source-alpha"


def test_agreeing_finished_observations_select_preferred_finished_source() -> None:
    older_finished = _source_event(
        source_name="zz-low-priority",
        observed=datetime(2024, 1, 15, 12, 0, tzinfo=UTC),
        event_date=date(2023, 8, 12),
        scheduled_start_utc=datetime(2023, 8, 12, 14, 0, tzinfo=UTC),
        status=EventStatus.FINISHED.value,
        home_score=2,
        away_score=1,
        result_code="home",
        outcome_availability_stage=OutcomeAvailability.POST_EVENT.value,
    )
    newer_finished = _source_event(
        source_name="fixture-source-beta",
        observed=datetime(2024, 1, 16, 12, 0, tzinfo=UTC),
        event_date=date(2023, 8, 12),
        scheduled_start_utc=datetime(2023, 8, 12, 14, 0, tzinfo=UTC),
        status=EventStatus.FINISHED.value,
        home_score=2,
        away_score=1,
        result_code="home",
        outcome_availability_stage=OutcomeAvailability.POST_EVENT.value,
        source_row_number=3,
    )

    (event,) = resolve_canonical_events_from_sources((older_finished, newer_finished))

    assert event.reconciliation.source_name == "fixture-source-beta"
    assert event.canonical.status == EventStatus.FINISHED.value
    assert event.canonical.home_score == 2
    assert event.canonical.away_score == 1
    assert event.canonical.result_code == "home"


def test_agreeing_finished_observations_tie_break_by_authority_then_source_name() -> None:
    observed = datetime(2024, 1, 15, 12, 0, tzinfo=UTC)
    low_authority = _source_event(
        source_name="zz-low-priority",
        observed=observed,
        event_date=date(2023, 8, 12),
        scheduled_start_utc=datetime(2023, 8, 12, 14, 0, tzinfo=UTC),
        status=EventStatus.FINISHED.value,
        home_score=2,
        away_score=1,
        result_code="home",
        outcome_availability_stage=OutcomeAvailability.POST_EVENT.value,
    )
    football_data = _source_event(
        source_name="football-data-co-uk",
        observed=observed,
        event_date=date(2023, 8, 12),
        scheduled_start_utc=datetime(2023, 8, 12, 14, 0, tzinfo=UTC),
        status=EventStatus.FINISHED.value,
        home_score=2,
        away_score=1,
        result_code="home",
        outcome_availability_stage=OutcomeAvailability.POST_EVENT.value,
        source_row_number=3,
    )
    beta = _source_event(
        source_name="beta-source",
        observed=observed,
        event_date=date(2023, 8, 12),
        scheduled_start_utc=datetime(2023, 8, 12, 14, 0, tzinfo=UTC),
        status=EventStatus.FINISHED.value,
        home_score=2,
        away_score=1,
        result_code="home",
        outcome_availability_stage=OutcomeAvailability.POST_EVENT.value,
        source_row_number=4,
    )
    alpha = _source_event(
        source_name="alpha-source",
        observed=observed,
        event_date=date(2023, 8, 12),
        scheduled_start_utc=datetime(2023, 8, 12, 14, 0, tzinfo=UTC),
        status=EventStatus.FINISHED.value,
        home_score=2,
        away_score=1,
        result_code="home",
        outcome_availability_stage=OutcomeAvailability.POST_EVENT.value,
        source_row_number=5,
    )

    (authority_winner,) = resolve_canonical_events_from_sources((low_authority, football_data))
    (name_winner,) = resolve_canonical_events_from_sources((beta, alpha))

    assert authority_winner.reconciliation.source_name == "football-data-co-uk"
    assert name_winner.reconciliation.source_name == "alpha-source"
    assert authority_winner.canonical.home_score == 2
    assert authority_winner.canonical.away_score == 1
    assert authority_winner.canonical.result_code == "home"


def test_input_ordering_does_not_change_canonical_event() -> None:
    finished = _source_event(
        source_name="fixture-source-alpha",
        observed=datetime(2024, 1, 15, 12, 0, tzinfo=UTC),
        event_date=date(2023, 8, 12),
        scheduled_start_utc=datetime(2023, 8, 12, 14, 0, tzinfo=UTC),
        status=EventStatus.FINISHED.value,
        home_score=2,
        away_score=1,
        result_code="home",
        outcome_availability_stage=OutcomeAvailability.POST_EVENT.value,
    )
    scheduled = _source_event(
        source_name="fixture-source-beta",
        observed=datetime(2024, 1, 16, 12, 0, tzinfo=UTC),
        event_date=date(2023, 8, 12),
        scheduled_start_utc=datetime(2023, 8, 12, 14, 0, tzinfo=UTC),
        status=EventStatus.SCHEDULED.value,
        source_row_number=3,
    )

    forward = resolve_canonical_events_from_sources((finished, scheduled))
    reverse = resolve_canonical_events_from_sources((scheduled, finished))

    assert forward == reverse
