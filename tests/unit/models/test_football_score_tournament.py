from __future__ import annotations

from datetime import date, timedelta

from sports_analytics.models.football_scores import (
    ScoreModelConfiguration,
    ScoreTrainingMatch,
)
from sports_analytics.models.football_tournament import (
    TournamentSplitConfiguration,
    default_score_candidates,
    run_score_tournament,
    tournament_payload,
)


def test_rolling_origin_tournament_is_deterministic_and_does_not_promote() -> None:
    teams = ("a", "b", "c", "d")
    matches = tuple(
        ScoreTrainingMatch(
            canonical_event_id=f"event-{index:03d}",
            competition_id="league",
            event_date=date(2023, 1, 1) + timedelta(days=index),
            home_team_id=teams[index % 4],
            away_team_id=teams[(index + 1) % 4],
            home_goals=(index * 7) % 4,
            away_goals=(index * 5) % 3,
        )
        for index in range(52)
    )
    model_config = ScoreModelConfiguration(minimum_matches=12)
    candidates = default_score_candidates(configuration=model_config)
    split = TournamentSplitConfiguration(
        minimum_training_rows=24,
        calibration_rows=8,
        test_rows=8,
        maximum_folds=2,
    )
    first = run_score_tournament(
        matches,
        candidates=candidates,
        split_configuration=split,
    )
    second = run_score_tournament(
        matches,
        candidates=candidates,
        split_configuration=split,
    )
    assert tournament_payload(first) == tournament_payload(second)
    assert first.provisional_winner_candidate_id is not None
    assert first.evaluation_provenance.value == "synthetic-contract"
    assert first.production_eligibility_state == "insufficient-real-evaluation-data"
    assert first.promotion_state == "not-promoted-explicit-governance-required"
    assert all(
        metric.training_end < metric.calibration_start
        and metric.calibration_end < metric.test_start
        for metric in first.fold_metrics
    )
