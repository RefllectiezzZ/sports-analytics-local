"""Canonical/source identity and conservative reconciliation contracts."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime

import pytest

from sports_analytics.core.exceptions import NormalizationError, SourceIntegrityError
from sports_analytics.sports.contracts import (
    EventReconciliation,
    ParticipantReconciliation,
    ParticipantType,
    ReconciliationState,
    validate_confidence,
    validate_reconciliation_state,
)
from sports_analytics.sports.identifiers import (
    FOOTBALL_DOMESTIC_HOME_OCCURRENCE_KEY,
    SPORT_FOOTBALL,
    build_canonical_event_id,
    build_canonical_participant_id,
    build_season_id,
    build_source_event_id,
    build_source_event_key,
    build_source_participant_id,
    build_source_participant_key,
)
from sports_analytics.sports.reconciliation import (
    PARTICIPANT_RECONCILIATION_POLICY_VERSION,
    RECONCILIATION_POLICY_VERSION,
    ParticipantReconciliationCandidate,
    ReconciliationCandidate,
    reconcile_candidates,
    reconcile_participant_candidates,
    unsupported_alias_reason,
)

OBSERVED_AT = datetime(2024, 1, 15, 12, 0, tzinfo=UTC)
EVENT_DATE = date(2023, 8, 12)
RESCHEDULED_DATE = date(2023, 8, 19)
KICKOFF = datetime(2023, 8, 12, 14, 0, tzinfo=UTC)
COMPETITION_ID = "eng-premier-league"
OTHER_COMPETITION_ID = "prt-primeira-liga"
SEASON_ID = build_season_id(competition_id=COMPETITION_ID, label="2023-2024")
SCHEMA_VERSION = "football-canonical-v2"

# Fictional source identifiers prove cross-source behaviour without requiring
# extra production source adapters.
SOURCE_ALPHA = "fixture-source-alpha"
SOURCE_BETA = "fixture-source-beta"

HOME_KEY = "northbridge fc"
AWAY_KEY = "southport athletic"
OTHER_KEY = "eastvale rovers"


def _canonical_participant_id(
    canonical_key: str,
    *,
    competition_id: str = COMPETITION_ID,
) -> str:
    return build_canonical_participant_id(
        sport_code=SPORT_FOOTBALL,
        competition_id=competition_id,
        participant_type=ParticipantType.TEAM.value,
        canonical_key=canonical_key,
    )


def _source_participant_key(
    *,
    source_name: str,
    normalized_name: str,
    competition_id: str = COMPETITION_ID,
) -> str:
    return build_source_participant_key(
        source_name=source_name,
        sport_code=SPORT_FOOTBALL,
        competition_id=competition_id,
        normalized_name=normalized_name,
    )


def _participant_candidate(
    *,
    source_name: str,
    normalized_name: str = HOME_KEY,
    display_name: str = "Northbridge FC",
    competition_id: str = COMPETITION_ID,
    supported_alias_canonical_key: str | None = None,
) -> ParticipantReconciliationCandidate:
    source_key = _source_participant_key(
        source_name=source_name,
        competition_id=competition_id,
        normalized_name=normalized_name,
    )
    return ParticipantReconciliationCandidate(
        source_name=source_name,
        source_participant_id=build_source_participant_id(source_participant_key=source_key),
        source_participant_key=source_key,
        sport_code=SPORT_FOOTBALL,
        competition_id=competition_id,
        participant_type=ParticipantType.TEAM.value,
        normalized_name=normalized_name,
        display_name=display_name,
        source_observed_at_utc=OBSERVED_AT,
        schema_version=SCHEMA_VERSION,
        supported_alias_canonical_key=supported_alias_canonical_key,
    )


def _candidate(
    *,
    source_name: str,
    home_key: str = HOME_KEY,
    away_key: str = AWAY_KEY,
    event_occurrence_key: str | None = FOOTBALL_DOMESTIC_HOME_OCCURRENCE_KEY,
    event_date: date | None = EVENT_DATE,
    scheduled_start_utc: datetime | None = KICKOFF,
    competition_id: str = COMPETITION_ID,
    season_id: str = SEASON_ID,
    home_override: str | None = None,
    away_override: str | None = None,
    home_canonical_missing: bool = False,
    away_canonical_missing: bool = False,
) -> ReconciliationCandidate:
    home_source_key = _source_participant_key(
        source_name=source_name,
        competition_id=competition_id,
        normalized_name=home_key,
    )
    away_source_key = _source_participant_key(
        source_name=source_name,
        competition_id=competition_id,
        normalized_name=away_key,
    )
    source_event_key = build_source_event_key(
        source_name=source_name,
        competition_id=competition_id,
        season_id=season_id,
        event_date=event_date if event_date is not None else EVENT_DATE,
        home_source_participant_key=home_source_key,
        away_source_participant_key=away_source_key,
    )
    home_canonical = (
        None
        if home_canonical_missing
        else (
            home_override
            if home_override is not None
            else _canonical_participant_id(home_key, competition_id=competition_id)
        )
    )
    away_canonical = (
        None
        if away_canonical_missing
        else (
            away_override
            if away_override is not None
            else _canonical_participant_id(away_key, competition_id=competition_id)
        )
    )
    return ReconciliationCandidate(
        source_name=source_name,
        source_event_id=build_source_event_id(source_event_key=source_event_key),
        source_event_key=source_event_key,
        sport_code=SPORT_FOOTBALL,
        competition_id=competition_id,
        season_id=season_id,
        event_occurrence_key=event_occurrence_key,
        event_date=event_date,
        scheduled_start_utc=scheduled_start_utc,
        home_canonical_participant_id=home_canonical,
        away_canonical_participant_id=away_canonical,
        source_observed_at_utc=OBSERVED_AT,
        schema_version=SCHEMA_VERSION,
    )


def test_different_sources_resolve_exact_participant_to_one_scoped_id() -> None:
    alpha = _participant_candidate(source_name=SOURCE_ALPHA)
    beta = _participant_candidate(source_name=SOURCE_BETA)

    results = reconcile_participant_candidates((beta, alpha))

    assert [item.source_name for item in results] == [SOURCE_ALPHA, SOURCE_BETA]
    assert {item.state for item in results} == {ReconciliationState.EXACT.value}
    assert {item.confidence for item in results} == {1.0}
    assert len({item.canonical_participant_id for item in results}) == 1
    assert results[0].canonical_participant_id == _canonical_participant_id(HOME_KEY)
    assert all(item.policy_version == PARTICIPANT_RECONCILIATION_POLICY_VERSION for item in results)
    assert all(item.is_downstream_safe for item in results)


def test_same_normalized_name_in_incompatible_competitions_does_not_merge() -> None:
    english = _participant_candidate(source_name=SOURCE_ALPHA, competition_id=COMPETITION_ID)
    portuguese = _participant_candidate(
        source_name=SOURCE_BETA,
        competition_id=OTHER_COMPETITION_ID,
    )

    results = reconcile_participant_candidates((english, portuguese))

    assert {item.state for item in results} == {ReconciliationState.EXACT.value}
    assert len({item.canonical_participant_id for item in results}) == 2
    assert results[0].match_key != results[1].match_key
    assert COMPETITION_ID in (results[0].match_key or "")
    assert OTHER_COMPETITION_ID in (results[1].match_key or "")


def test_alias_names_are_not_automatically_merged_without_mapping() -> None:
    sporting_cp = _participant_candidate(
        source_name=SOURCE_ALPHA,
        normalized_name="sporting cp",
        display_name="Sporting CP",
    )
    sporting_lisbon = _participant_candidate(
        source_name=SOURCE_BETA,
        normalized_name="sporting lisbon",
        display_name="Sporting Lisbon",
    )

    results = reconcile_participant_candidates((sporting_cp, sporting_lisbon))

    assert {item.state for item in results} == {ReconciliationState.EXACT.value}
    assert len({item.canonical_participant_id for item in results}) == 2
    assert len({item.match_key for item in results}) == 2
    # The helper documents the reason a caller should record if it elects to keep
    # an alias pair unresolved instead of supplying an explicit mapping.
    assert unsupported_alias_reason(
        left_name="Sporting CP",
        right_name="Sporting Lisbon",
    ) == (
        "unsupported participant alias without explicit mapping: 'Sporting CP' vs 'Sporting Lisbon'"
    )


def test_explicit_supported_alias_mapping_can_resolve_to_one_participant() -> None:
    canonical_alias_key = "sporting cp"
    sporting_cp = _participant_candidate(
        source_name=SOURCE_ALPHA,
        normalized_name="sporting cp",
        display_name="Sporting CP",
    )
    sporting_lisbon = _participant_candidate(
        source_name=SOURCE_BETA,
        normalized_name="sporting lisbon",
        display_name="Sporting Lisbon",
        supported_alias_canonical_key=canonical_alias_key,
    )

    results = reconcile_participant_candidates((sporting_cp, sporting_lisbon))

    assert {item.state for item in results} == {ReconciliationState.EXACT.value}
    assert len({item.canonical_participant_id for item in results}) == 1
    assert {item.match_key for item in results} == {
        (f"participant|{SPORT_FOOTBALL}|{COMPETITION_ID}|{ParticipantType.TEAM.value}|sporting cp")
    }


def test_canonical_participant_id_is_competition_scoped_and_not_source_scoped() -> None:
    first = _canonical_participant_id(HOME_KEY)
    second = _canonical_participant_id(HOME_KEY)
    other_competition = _canonical_participant_id(
        HOME_KEY,
        competition_id=OTHER_COMPETITION_ID,
    )

    assert first == second
    assert first != other_competition
    for source_name in (SOURCE_ALPHA, SOURCE_BETA):
        source_key = _source_participant_key(source_name=source_name, normalized_name=HOME_KEY)
        assert source_name in source_key
        assert COMPETITION_ID in source_key
        assert source_name not in first
        assert build_source_participant_id(source_participant_key=source_key) != first


def test_changing_event_date_or_kickoff_does_not_change_canonical_event_id() -> None:
    home = _canonical_participant_id(HOME_KEY)
    away = _canonical_participant_id(AWAY_KEY)

    first = build_canonical_event_id(
        sport_code=SPORT_FOOTBALL,
        competition_id=COMPETITION_ID,
        season_id=SEASON_ID,
        home_canonical_participant_id=home,
        away_canonical_participant_id=away,
        event_occurrence_key=FOOTBALL_DOMESTIC_HOME_OCCURRENCE_KEY,
    )
    rescheduled = build_canonical_event_id(
        sport_code=SPORT_FOOTBALL,
        competition_id=COMPETITION_ID,
        season_id=SEASON_ID,
        home_canonical_participant_id=home,
        away_canonical_participant_id=away,
        event_occurrence_key=FOOTBALL_DOMESTIC_HOME_OCCURRENCE_KEY,
    )

    assert first == rescheduled


def test_two_source_refs_for_rescheduled_fixture_resolve_to_same_canonical_event() -> None:
    alpha = _candidate(source_name=SOURCE_ALPHA, event_date=EVENT_DATE, scheduled_start_utc=KICKOFF)
    beta = _candidate(
        source_name=SOURCE_BETA,
        event_date=RESCHEDULED_DATE,
        scheduled_start_utc=datetime(2023, 8, 19, 18, 0, tzinfo=UTC),
    )

    results = reconcile_candidates((alpha, beta))

    assert alpha.source_event_id != beta.source_event_id
    assert {item.state for item in results} == {ReconciliationState.EXACT.value}
    assert len({item.canonical_event_id for item in results}) == 1
    assert all(item.reason is None for item in results)


def test_two_distinct_occurrences_need_different_occurrence_keys() -> None:
    first = _candidate(
        source_name=SOURCE_ALPHA,
        event_occurrence_key=FOOTBALL_DOMESTIC_HOME_OCCURRENCE_KEY,
    )
    second = _candidate(source_name=SOURCE_BETA, event_occurrence_key="season-playoff-leg-1")

    results = reconcile_candidates((first, second))

    assert {item.state for item in results} == {ReconciliationState.EXACT.value}
    assert len({item.canonical_event_id for item in results}) == 2
    assert len({item.match_key for item in results}) == 2


def test_source_event_ids_may_be_schedule_dependent() -> None:
    first = _candidate(source_name=SOURCE_ALPHA, event_date=EVENT_DATE)
    postponed = _candidate(source_name=SOURCE_ALPHA, event_date=RESCHEDULED_DATE)

    assert first.source_event_id != postponed.source_event_id
    assert first.source_event_key != postponed.source_event_key


def test_duplicate_source_event_id_with_conflicting_payload_raises_source_integrity_error() -> None:
    first = _candidate(source_name=SOURCE_ALPHA)
    conflicting = replace(first, event_date=RESCHEDULED_DATE)

    with pytest.raises(SourceIntegrityError, match="conflicting duplicate source event identity"):
        reconcile_candidates((first, conflicting))


def test_duplicate_source_events_from_one_source_become_unresolved() -> None:
    first = _candidate(source_name=SOURCE_ALPHA)
    duplicate_same_payload = _candidate(source_name=SOURCE_ALPHA)

    results = reconcile_candidates((first, duplicate_same_payload))

    assert {item.state for item in results} == {ReconciliationState.UNRESOLVED.value}
    assert {item.canonical_event_id for item in results} == {None}
    for item in results:
        assert "ambiguous duplicate source events" in (item.reason or "")
        assert not item.is_downstream_safe


def test_reconciliation_is_deterministic_across_repeated_runs() -> None:
    candidates = (_candidate(source_name=SOURCE_BETA), _candidate(source_name=SOURCE_ALPHA))

    first = reconcile_candidates(candidates)
    second = reconcile_candidates(candidates)

    assert first == second
    assert [item.source_name for item in first] == sorted(item.source_name for item in first)


def test_unresolved_participants_cannot_produce_downstream_safe_canonical_events() -> None:
    missing_home = _candidate(source_name=SOURCE_ALPHA, home_canonical_missing=True)

    (result,) = reconcile_candidates((missing_home,))

    assert result.state == ReconciliationState.UNRESOLVED.value
    assert result.canonical_event_id is None
    assert result.reason == "missing canonical home participant"
    assert not result.is_downstream_safe


@pytest.mark.parametrize(
    ("kwargs", "expected_reason"),
    [
        ({"event_occurrence_key": None}, "missing event occurrence key"),
        ({"home_canonical_missing": True}, "missing canonical home participant"),
        ({"away_canonical_missing": True}, "missing canonical away participant"),
    ],
)
def test_incomplete_event_identity_becomes_unresolved(
    kwargs: dict[str, object],
    expected_reason: str,
) -> None:
    candidate = _candidate(source_name=SOURCE_ALPHA, **kwargs)  # type: ignore[arg-type]

    results = reconcile_candidates((candidate,))

    assert len(results) == 1
    assert results[0].state == ReconciliationState.UNRESOLVED.value
    assert results[0].canonical_event_id is None
    assert results[0].reason == expected_reason


def test_identical_canonical_participants_become_unresolved() -> None:
    same = _canonical_participant_id(HOME_KEY)
    candidate = _candidate(
        source_name=SOURCE_ALPHA,
        home_override=same,
        away_override=same,
    )

    results = reconcile_candidates((candidate,))

    assert results[0].state == ReconciliationState.UNRESOLVED.value
    assert results[0].reason == "identical canonical participants"


@pytest.mark.parametrize("value", [-0.1, 1.1, float("nan"), float("inf"), True])
def test_reconciliation_confidence_bounds_are_enforced(value: float) -> None:
    with pytest.raises(NormalizationError):
        validate_confidence(value)


@pytest.mark.parametrize("value", [0.0, 0.5, 1.0])
def test_reconciliation_confidence_accepts_bounded_values(value: float) -> None:
    assert validate_confidence(value) == value


def test_reconciliation_state_must_be_a_contract_state() -> None:
    for state in ("exact", "probable", "manual", "unresolved"):
        assert validate_reconciliation_state(state) == state
    with pytest.raises(NormalizationError, match="reconciliation state must be one of"):
        validate_reconciliation_state("maybe")


def _event_reconciliation(**overrides: object) -> EventReconciliation:
    payload: dict[str, object] = {
        "source_name": SOURCE_ALPHA,
        "source_event_id": "1f1c2d3e-4a5b-4c6d-8e7f-90a1b2c3d4e5",
        "source_event_key": "fixture-source-alpha|eng-premier-league",
        "canonical_event_id": "2f1c2d3e-4a5b-4c6d-8e7f-90a1b2c3d4e5",
        "state": ReconciliationState.EXACT.value,
        "confidence": 1.0,
        "policy_version": RECONCILIATION_POLICY_VERSION,
        "match_key": "event|football",
        "reason": None,
        "source_observed_at_utc": OBSERVED_AT,
        "schema_version": SCHEMA_VERSION,
    }
    payload.update(overrides)
    return EventReconciliation(**payload)  # type: ignore[arg-type]


def _participant_reconciliation(**overrides: object) -> ParticipantReconciliation:
    source_key = _source_participant_key(source_name=SOURCE_ALPHA, normalized_name=HOME_KEY)
    payload: dict[str, object] = {
        "source_name": SOURCE_ALPHA,
        "source_participant_id": build_source_participant_id(source_participant_key=source_key),
        "source_participant_key": source_key,
        "canonical_participant_id": _canonical_participant_id(HOME_KEY),
        "state": ReconciliationState.EXACT.value,
        "confidence": 1.0,
        "policy_version": PARTICIPANT_RECONCILIATION_POLICY_VERSION,
        "match_key": (
            f"participant|{SPORT_FOOTBALL}|{COMPETITION_ID}|{ParticipantType.TEAM.value}|{HOME_KEY}"
        ),
        "reason": None,
        "source_observed_at_utc": OBSERVED_AT,
        "schema_version": SCHEMA_VERSION,
    }
    payload.update(overrides)
    return ParticipantReconciliation(**payload)  # type: ignore[arg-type]


def test_exact_reconciliation_requires_full_confidence() -> None:
    with pytest.raises(NormalizationError, match="exact reconciliation requires confidence 1.0"):
        _event_reconciliation(confidence=0.9)


def test_probable_reconciliation_requires_intermediate_confidence() -> None:
    probable = _event_reconciliation(state=ReconciliationState.PROBABLE.value, confidence=0.6)

    assert probable.confidence == 0.6
    with pytest.raises(NormalizationError, match="strictly between"):
        _event_reconciliation(state=ReconciliationState.PROBABLE.value, confidence=1.0)


def test_unresolved_event_reconciliation_must_not_claim_a_canonical_event() -> None:
    with pytest.raises(NormalizationError, match="must not claim a canonical event"):
        _event_reconciliation(
            state=ReconciliationState.UNRESOLVED.value,
            confidence=0.0,
            reason="missing event occurrence key",
        )


def test_unresolved_event_reconciliation_requires_a_reason() -> None:
    with pytest.raises(NormalizationError, match="requires an explicit reason"):
        _event_reconciliation(
            state=ReconciliationState.UNRESOLVED.value,
            confidence=0.0,
            canonical_event_id=None,
            reason=None,
        )


def test_resolved_event_reconciliation_requires_a_canonical_event() -> None:
    with pytest.raises(NormalizationError, match="requires a canonical event id"):
        _event_reconciliation(canonical_event_id=None)


def test_unresolved_participant_reconciliation_must_not_claim_a_canonical_participant() -> None:
    with pytest.raises(NormalizationError, match="must not claim a canonical id"):
        _participant_reconciliation(
            state=ReconciliationState.UNRESOLVED.value,
            confidence=0.0,
            reason="unsupported alias",
        )


def test_resolved_participant_reconciliation_requires_a_canonical_participant() -> None:
    with pytest.raises(NormalizationError, match="requires a canonical participant id"):
        _participant_reconciliation(canonical_participant_id=None)


def test_manual_reconciliation_state_is_available_in_the_contract() -> None:
    manual = _event_reconciliation(state=ReconciliationState.MANUAL.value, confidence=1.0)

    assert manual.state == ReconciliationState.MANUAL.value
    assert manual.is_downstream_safe
