"""Unit tests for leakage-safe football pre-match features."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from tests.helpers_training import make_club_id, synthetic_finished_events

from sports_analytics.core.exceptions import FeatureError
from sports_analytics.features.contracts import FORBIDDEN_MODEL_FEATURE_FIELDS
from sports_analytics.features.football.prematch import (
    ELO_INITIAL_RATING,
    FinishedTrainingEvent,
    generate_prematch_features,
)
from sports_analytics.features.football.specification import (
    FOOTBALL_1X2_FEATURE_NAMES_V1,
    football_1x2_prematch_specification,
)


def _event(
    *,
    event_id: str,
    event_date: date,
    home: str,
    away: str,
    home_score: int,
    away_score: int,
    result_code: str,
    season_id: str = "eng-premier-league:2023-2024",
) -> FinishedTrainingEvent:
    return FinishedTrainingEvent(
        canonical_event_id=event_id,
        sport_code="football",
        competition_id="eng-premier-league",
        season_id=season_id,
        event_date=event_date,
        scheduled_start_utc=None,
        home_canonical_participant_id=home,
        away_canonical_participant_id=away,
        home_score=home_score,
        away_score=away_score,
        result_code=result_code,
    )


def test_result_does_not_affect_same_event_features() -> None:
    home = make_club_id("Northbridge FC")
    away = make_club_id("Southport Athletic")
    event = _event(
        event_id="e1",
        event_date=date(2023, 8, 12),
        home=home,
        away=away,
        home_score=5,
        away_score=0,
        result_code="home",
    )
    vectors = generate_prematch_features((event,))
    assert vectors[0].features["home_elo"] == ELO_INITIAL_RATING
    assert vectors[0].features["away_elo"] == ELO_INITIAL_RATING
    assert vectors[0].features["home_matches_played"] == 0.0
    assert vectors[0].features["home_window5_count"] == 0.0


def test_same_date_matches_do_not_affect_each_other() -> None:
    a = make_club_id("Northbridge FC")
    b = make_club_id("Southport Athletic")
    c = make_club_id("Eastmere United")
    d = make_club_id("Westfield Town")
    day = date(2023, 8, 12)
    events = (
        _event(
            event_id="e1",
            event_date=day,
            home=a,
            away=b,
            home_score=3,
            away_score=0,
            result_code="home",
        ),
        _event(
            event_id="e2",
            event_date=day,
            home=c,
            away=d,
            home_score=0,
            away_score=2,
            result_code="away",
        ),
    )
    vectors = generate_prematch_features(events)
    assert all(item.features["home_matches_played"] == 0.0 for item in vectors)
    assert all(item.features["home_elo"] == ELO_INITIAL_RATING for item in vectors)


def test_future_result_changes_do_not_alter_earlier_features() -> None:
    home = make_club_id("Northbridge FC")
    away = make_club_id("Southport Athletic")
    other = make_club_id("Eastmere United")
    base = (
        _event(
            event_id="e1",
            event_date=date(2023, 8, 12),
            home=home,
            away=away,
            home_score=1,
            away_score=0,
            result_code="home",
        ),
        _event(
            event_id="e2",
            event_date=date(2023, 8, 19),
            home=home,
            away=other,
            home_score=2,
            away_score=2,
            result_code="draw",
        ),
    )
    altered = (
        base[0],
        _event(
            event_id="e2",
            event_date=date(2023, 8, 19),
            home=home,
            away=other,
            home_score=0,
            away_score=5,
            result_code="away",
        ),
    )
    first = generate_prematch_features(base)[0].ordered_values()
    second = generate_prematch_features(altered)[0].ordered_values()
    assert first == second


def test_feature_whitelist_excludes_odds_and_targets() -> None:
    specification = football_1x2_prematch_specification()
    names = set(specification.ordered_feature_names)
    assert "result_code" not in names
    assert names.isdisjoint(FORBIDDEN_MODEL_FEATURE_FIELDS)
    assert specification.ordered_feature_names == FOOTBALL_1X2_FEATURE_NAMES_V1


def test_deterministic_elo_and_rolling_features() -> None:
    events = synthetic_finished_events(
        matches_per_season=24, season_ids=("eng-premier-league:2023-2024",)
    )
    first = generate_prematch_features(events)
    second = generate_prematch_features(tuple(reversed(events)))
    assert [item.ordered_values() for item in first] == [item.ordered_values() for item in second]
    later = first[10]
    assert (
        later.features["home_matches_played"] > 0.0 or later.features["away_matches_played"] > 0.0
    )
    assert (
        later.features["home_elo"] != ELO_INITIAL_RATING
        or later.features["away_elo"] != ELO_INITIAL_RATING
    )


def test_cold_start_defaults() -> None:
    home = make_club_id("Northbridge FC")
    away = make_club_id("Southport Athletic")
    vectors = generate_prematch_features(
        (
            _event(
                event_id="e1",
                event_date=date(2023, 8, 12),
                home=home,
                away=away,
                home_score=1,
                away_score=0,
                result_code="home",
            ),
        )
    )
    features = vectors[0].features
    assert features["home_ppg_5"] == 0.0
    assert features["home_rest_available"] == 0.0
    assert features["home_days_since_prev"] == 7.0
    assert features["home_window5_count"] == 0.0
    assert features["home_window10_count"] == 0.0


@pytest.mark.parametrize(
    ("prior_matches", "expected_count"),
    [(0, 0), (1, 1), (5, 5), (10, 5)],
)
def test_history_counts_are_capped(prior_matches: int, expected_count: int) -> None:
    home = make_club_id("Northbridge FC")
    away = make_club_id("Southport Athletic")
    opponent = make_club_id("Eastmere United")
    events: list[FinishedTrainingEvent] = []
    current = date(2023, 8, 1)
    for index in range(prior_matches):
        events.append(
            _event(
                event_id=f"hist-{index}",
                event_date=current,
                home=home,
                away=opponent,
                home_score=1,
                away_score=0,
                result_code="home",
            )
        )
        current += timedelta(days=7)
    events.append(
        _event(
            event_id="target",
            event_date=current,
            home=home,
            away=away,
            home_score=1,
            away_score=1,
            result_code="draw",
        )
    )
    vector = generate_prematch_features(tuple(events))[-1]
    assert vector.features["home_window5_count"] == float(expected_count)
    assert vector.features["home_window10_count"] == float(min(prior_matches, 10))


def test_rejects_mixed_competitions() -> None:
    home = make_club_id("Northbridge FC")
    away = make_club_id("Southport Athletic")
    events = (
        _event(
            event_id="e1",
            event_date=date(2023, 8, 12),
            home=home,
            away=away,
            home_score=1,
            away_score=0,
            result_code="home",
        ),
        FinishedTrainingEvent(
            canonical_event_id="e2",
            sport_code="football",
            competition_id="prt-primeira-liga",
            season_id="prt-primeira-liga:2023-2024",
            event_date=date(2023, 8, 13),
            scheduled_start_utc=None,
            home_canonical_participant_id=home,
            away_canonical_participant_id=away,
            home_score=1,
            away_score=0,
            result_code="home",
        ),
    )
    with pytest.raises(FeatureError, match="mixed competitions"):
        generate_prematch_features(events)


def test_target_separate_from_feature_names() -> None:
    events = synthetic_finished_events(
        matches_per_season=8, season_ids=("eng-premier-league:2023-2024",)
    )
    vectors = generate_prematch_features(events)
    assert "result_code" not in vectors[0].features
    assert vectors[0].result_code in {"home", "draw", "away"}
