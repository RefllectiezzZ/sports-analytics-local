from __future__ import annotations

from datetime import date, timedelta

from sports_analytics.models.football_scores import (
    ScoreModelConfiguration,
    ScoreTrainingMatch,
)
from sports_analytics.models.football_tournament import TournamentSplitConfiguration
from sports_analytics.models.football_unified_tournament import (
    load_unified_tournament_artifact,
    run_unified_tournament,
    write_unified_tournament_artifact,
)


def test_unified_contract_tournament_uses_common_rows_and_cannot_promote(tmp_path) -> None:
    teams = ("a", "b", "c", "d")
    matches = tuple(
        ScoreTrainingMatch(
            canonical_event_id=f"event-{index:03d}",
            competition_id="league",
            event_date=date(2024, 1, 1) + timedelta(days=index),
            home_team_id=teams[index % 4],
            away_team_id=teams[(index + 1) % 4],
            home_goals=(index * 7) % 4,
            away_goals=(index * 5) % 3,
        )
        for index in range(52)
    )
    tournament = run_unified_tournament(
        matches,
        split_configuration=TournamentSplitConfiguration(
            minimum_training_rows=24,
            calibration_rows=8,
            test_rows=8,
            maximum_folds=2,
        ),
        score_configuration=ScoreModelConfiguration(minimum_matches=12),
    )
    evaluated = {
        candidate for candidate, state in tournament.candidate_states if state == "evaluated"
    }
    assert evaluated == {
        "dynamic-independent-poisson-v1",
        "dynamic-dixon-coles-v1",
        "covariate-dixon-coles-v1",
        "coherent-score-ensemble-v1",
    }
    assert {row.test_rows for row in tournament.metrics} == {8}
    assert tournament.production_eligibility_state == "insufficient-real-evaluation-data"
    assert tournament.promotion_state == "not-promoted-explicit-governance-required"
    artifact = write_unified_tournament_artifact(
        root=tmp_path,
        relative_directory="unified",
        tournament=tournament,
    )
    assert (
        load_unified_tournament_artifact(
            root=tmp_path,
            relative_directory="unified",
            expected_checksum=artifact.checksum_sha256,
        ).artifact_id
        == artifact.artifact_id
    )
