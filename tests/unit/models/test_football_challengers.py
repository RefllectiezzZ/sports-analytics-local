from __future__ import annotations

from datetime import date, timedelta

import pytest

from sports_analytics.models.football_challengers import (
    blend_score_surfaces,
    fit_covariate_dixon_coles,
    load_challenger_artifact,
    predict_covariate_score,
    select_ensemble_weights,
    write_challenger_artifact,
)
from sports_analytics.models.football_scores import (
    ScoreModelConfiguration,
    ScoreTrainingMatch,
    joint_score_from_intensities,
)


def _matches() -> tuple[ScoreTrainingMatch, ...]:
    teams = ("a", "b", "c", "d")
    return tuple(
        ScoreTrainingMatch(
            canonical_event_id=f"event-{index:03d}",
            competition_id="league",
            event_date=date(2024, 1, 1) + timedelta(days=index),
            home_team_id=teams[index % 4],
            away_team_id=teams[(index + 1) % 4],
            home_goals=(index * 3) % 4,
            away_goals=(index * 5) % 3,
        )
        for index in range(52)
    )


def test_covariate_challenger_is_temporal_deterministic_and_reloadable(tmp_path) -> None:
    matches = _matches()
    model = fit_covariate_dixon_coles(
        matches,
        score_configuration=ScoreModelConfiguration(minimum_matches=12),
    )
    first = predict_covariate_score(
        model,
        home_team_id="a",
        away_team_id="b",
        prediction_cutoff=date(2025, 1, 1),
    )
    second = predict_covariate_score(
        model,
        home_team_id="a",
        away_team_id="b",
        prediction_cutoff=date(2025, 1, 1),
    )
    assert first == second
    artifact = write_challenger_artifact(
        root=tmp_path,
        relative_directory="challenger",
        model=model,
        ensemble_weights=(0.3, 0.3, 0.4),
        training_evidence_artifact_id="a" * 64,
        training_evidence_checksum_sha256="b" * 64,
        source_snapshot_refs=(("snapshot-a", "c" * 64),),
    )
    loaded = load_challenger_artifact(
        root=tmp_path,
        relative_directory="challenger",
        expected_checksum=artifact.checksum_sha256,
    )
    assert loaded.artifact_id == artifact.artifact_id
    assert loaded.payload["training_evidence"] == {
        "artifact_id": "a" * 64,
        "checksum_sha256": "b" * 64,
    }
    assert loaded.payload["source_snapshot_refs"] == [
        {"snapshot_id": "snapshot-a", "checksum_sha256": "c" * 64}
    ]


def test_complete_surface_ensemble_preserves_coherence_and_selects_weights() -> None:
    calibration = tuple(
        (
            joint_score_from_intensities(
                home_intensity=1.5 + index / 100,
                away_intensity=0.9,
                prediction_cutoff=date(2025, 1, 1),
            ),
            joint_score_from_intensities(
                home_intensity=1.1,
                away_intensity=1.2 + index / 100,
                prediction_cutoff=date(2025, 1, 1),
            ),
        )
        for index in range(12)
    )
    components = (
        tuple(item[0] for item in calibration),
        tuple(item[1] for item in calibration),
    )
    weights = select_ensemble_weights(
        components,
        tuple((1, 0) for _ in calibration),
    )
    assert sum(weights) == pytest.approx(1.0)
    blended = blend_score_surfaces(
        (components[0][0], components[1][0]),
        weights=weights,
    )
    assert sum(sum(row) for row in blended.probabilities) == pytest.approx(1.0)
    assert min(value for row in blended.probabilities for value in row) >= 0.0
