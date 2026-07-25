"""Canonical versus source identity and conservative cross-source reconciliation.

Only one real source adapter exists, so the cross-source proofs below use two
fictional source identifiers. The production catalog still contains exactly one
implemented source.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from sports_analytics.core.exceptions import NormalizationError
from sports_analytics.sports.contracts import (
    EventReconciliation,
    ParticipantType,
    ReconciliationState,
    validate_confidence,
    validate_reconciliation_state,
)
from sports_analytics.sports.identifiers import (
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
    RECONCILIATION_POLICY_VERSION,
    ReconciliationCandidate,
    reconcile_candidates,
)

OBSERVED_AT = datetime(2024, 1, 15, 12, 0, tzinfo=UTC)
EVENT_DATE = date(2023, 8, 12)
KICKOFF = datetime(2023, 8, 12, 14, 0, tzinfo=UTC)
COMPETITION_ID = "eng-premier-league"
SEASON_ID = build_season_id(competition_id=COMPETITION_ID, label="2023-2024")
SCHEMA_VERSION = "football-canonical-v2"

# Two fictional sources used only to prove cross-source behaviour.
SOURCE_ALPHA = "fixture-source-alpha"
SOURCE_BETA = "fixture-source-beta"

HOME_KEY = "northbridge fc"
AWAY_KEY = "southport athletic"
OTHER_KEY = "eastvale rovers"


def _canonical_participant_id(canonical_key: str) -> str:
    return build_canonical_participant_id(
        sport_code=SPORT_FOOTBALL,
        participant_type=ParticipantType.TEAM.value,
        canonical_key=canonical_key,
    )


def _candidate(
    *,
    source_name: str,
    home_key: str = HOME_KEY,
    away_key: str = AWAY_KEY,
    event_date: date | None = EVENT_DATE,
    scheduled_start_utc: datetime | None = None,
    competition_id: str = COMPETITION_ID,
    season_id: str = SEASON_ID,
    home_override: str | None = None,
    away_override: str | None = None,
    home_canonical_missing: bool = False,
    away_canonical_missing: bool = False,
) -> ReconciliationCandidate:
    home_source_key = build_source_participant_key(
        source_name=source_name,
        sport_code=SPORT_FOOTBALL,
        normalized_name=home_key,
    )
    away_source_key = build_source_participant_key(
        source_name=source_name,
        sport_code=SPORT_FOOTBALL,
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
        else (home_override if home_override is not None else _canonical_participant_id(home_key))
    )
    away_canonical = (
        None
        if away_canonical_missing
        else (away_override if away_override is not None else _canonical_participant_id(away_key))
    )
    return ReconciliationCandidate(
        source_name=source_name,
        source_event_id=build_source_event_id(source_event_key=source_event_key),
        source_event_key=source_event_key,
        sport_code=SPORT_FOOTBALL,
        competition_id=competition_id,
        season_id=season_id,
        event_date=event_date,
        scheduled_start_utc=scheduled_start_utc,
        home_canonical_participant_id=home_canonical,
        away_canonical_participant_id=away_canonical,
        source_observed_at_utc=OBSERVED_AT,
        schema_version=SCHEMA_VERSION,
    )


def test_canonical_participant_id_does_not_depend_on_source_name() -> None:
    first = _canonical_participant_id(HOME_KEY)
    second = _canonical_participant_id(HOME_KEY)

    assert first == second
    for source_name in (SOURCE_ALPHA, SOURCE_BETA):
        source_key = build_source_participant_key(
            source_name=source_name,
            sport_code=SPORT_FOOTBALL,
            normalized_name=HOME_KEY,
        )
        assert source_name in source_key
        assert source_name not in first
        assert build_source_participant_id(source_participant_key=source_key) != first


def test_source_participant_ids_differ_per_source() -> None:
    ids = {
        build_source_participant_id(
            source_participant_key=build_source_participant_key(
                source_name=source_name,
                sport_code=SPORT_FOOTBALL,
                normalized_name=HOME_KEY,
            )
        )
        for source_name in (SOURCE_ALPHA, SOURCE_BETA)
    }

    assert len(ids) == 2


def test_canonical_event_id_does_not_depend_on_source_name() -> None:
    canonical = build_canonical_event_id(
        sport_code=SPORT_FOOTBALL,
        competition_id=COMPETITION_ID,
        season_id=SEASON_ID,
        event_date=EVENT_DATE,
        home_canonical_participant_id=_canonical_participant_id(HOME_KEY),
        away_canonical_participant_id=_canonical_participant_id(AWAY_KEY),
    )

    for source_name in (SOURCE_ALPHA, SOURCE_BETA):
        assert source_name not in canonical


def test_two_sources_reconcile_to_one_canonical_event() -> None:
    alpha = _candidate(source_name=SOURCE_ALPHA)
    beta = _candidate(source_name=SOURCE_BETA)

    results = reconcile_candidates((alpha, beta))

    assert alpha.source_event_id != beta.source_event_id
    assert len(results) == 2
    assert {item.state for item in results} == {ReconciliationState.EXACT.value}
    assert {item.confidence for item in results} == {1.0}
    canonical_ids = {item.canonical_event_id for item in results}
    assert len(canonical_ids) == 1
    assert canonical_ids != {None}
    # Source provenance is retained alongside canonical identity.
    assert {item.source_name for item in results} == {SOURCE_ALPHA, SOURCE_BETA}
    assert all(item.policy_version == RECONCILIATION_POLICY_VERSION for item in results)
    assert all(item.is_downstream_safe for item in results)


def test_reconciliation_is_deterministic_across_repeated_runs() -> None:
    candidates = (_candidate(source_name=SOURCE_BETA), _candidate(source_name=SOURCE_ALPHA))

    first = reconcile_candidates(candidates)
    second = reconcile_candidates(candidates)

    assert first == second
    assert [item.source_name for item in first] == sorted(item.source_name for item in first)


def test_conflicting_participants_do_not_reconcile() -> None:
    alpha = _candidate(source_name=SOURCE_ALPHA)
    beta = _candidate(source_name=SOURCE_BETA, away_key=OTHER_KEY)

    results = reconcile_candidates((alpha, beta))

    assert {item.state for item in results} == {ReconciliationState.EXACT.value}
    canonical_ids = {item.canonical_event_id for item in results}
    assert len(canonical_ids) == 2


def test_conflicting_scheduled_start_becomes_unresolved() -> None:
    alpha = _candidate(source_name=SOURCE_ALPHA, scheduled_start_utc=KICKOFF)
    beta = _candidate(
        source_name=SOURCE_BETA,
        scheduled_start_utc=KICKOFF.replace(hour=18),
    )

    results = reconcile_candidates((alpha, beta))

    assert {item.state for item in results} == {ReconciliationState.UNRESOLVED.value}
    assert {item.canonical_event_id for item in results} == {None}
    assert {item.confidence for item in results} == {0.0}
    for item in results:
        assert "conflicting scheduled start" in (item.reason or "")
        assert not item.is_downstream_safe


def test_one_null_scheduled_start_still_reconciles_exactly() -> None:
    alpha = _candidate(source_name=SOURCE_ALPHA, scheduled_start_utc=KICKOFF)
    beta = _candidate(source_name=SOURCE_BETA, scheduled_start_utc=None)

    results = reconcile_candidates((alpha, beta))

    assert {item.state for item in results} == {ReconciliationState.EXACT.value}
    assert len({item.canonical_event_id for item in results}) == 1


def test_duplicate_events_from_one_source_become_unresolved() -> None:
    first = _candidate(source_name=SOURCE_ALPHA)
    second = _candidate(source_name=SOURCE_ALPHA)

    results = reconcile_candidates((first, second))

    assert {item.state for item in results} == {ReconciliationState.UNRESOLVED.value}
    for item in results:
        assert "ambiguous duplicate source events" in (item.reason or "")


@pytest.mark.parametrize(
    ("kwargs", "expected_reason"),
    [
        ({"event_date": None}, "missing event date"),
        ({"home_canonical_missing": True}, "missing canonical home participant"),
        ({"away_canonical_missing": True}, "missing canonical away participant"),
    ],
)
def test_incomplete_identity_becomes_unresolved(
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


def _reconciliation(**overrides: object) -> EventReconciliation:
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


def test_exact_reconciliation_requires_full_confidence() -> None:
    with pytest.raises(NormalizationError, match="exact reconciliation requires confidence 1.0"):
        _reconciliation(confidence=0.9)


def test_probable_reconciliation_requires_intermediate_confidence() -> None:
    probable = _reconciliation(state=ReconciliationState.PROBABLE.value, confidence=0.6)

    assert probable.confidence == 0.6
    with pytest.raises(NormalizationError, match="strictly between"):
        _reconciliation(state=ReconciliationState.PROBABLE.value, confidence=1.0)


def test_unresolved_reconciliation_must_not_claim_a_canonical_event() -> None:
    with pytest.raises(NormalizationError, match="must not claim a canonical event"):
        _reconciliation(
            state=ReconciliationState.UNRESOLVED.value,
            confidence=0.0,
            reason="missing event date",
        )


def test_unresolved_reconciliation_requires_a_reason() -> None:
    with pytest.raises(NormalizationError, match="requires an explicit reason"):
        _reconciliation(
            state=ReconciliationState.UNRESOLVED.value,
            confidence=0.0,
            canonical_event_id=None,
            reason=None,
        )


def test_resolved_reconciliation_requires_a_canonical_event() -> None:
    with pytest.raises(NormalizationError, match="requires a canonical event id"):
        _reconciliation(canonical_event_id=None)


def test_manual_reconciliation_state_is_available_in_the_contract() -> None:
    manual = _reconciliation(state=ReconciliationState.MANUAL.value, confidence=1.0)

    assert manual.state == ReconciliationState.MANUAL.value
    assert manual.is_downstream_safe
