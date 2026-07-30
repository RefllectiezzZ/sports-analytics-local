from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, timedelta

import pytest

from sports_analytics.artifacts import build_analytical_artifact_document
from sports_analytics.core.exceptions import ArtifactError, ModelError
from sports_analytics.data.codec import dumps_canonical_json
from sports_analytics.models.football_scores import (
    DIXON_COLES,
    INDEPENDENT_POISSON,
    ScoreModelConfiguration,
    ScoreTrainingMatch,
    dixon_coles_correction,
    fit_dixon_coles,
    fit_independent_poisson,
    joint_score_from_intensities,
    load_score_model_artifact,
    predict_joint_score,
    rake_result_regions,
    score_model_from_payload,
    score_model_to_payload,
    temperature_scale_distribution,
    write_score_model_artifact,
)
from sports_analytics.predictions.football_scores import (
    load_production_football_probability_artifact,
    write_production_football_probability_artifact,
)


def _matches(count: int = 36) -> tuple[ScoreTrainingMatch, ...]:
    teams = ("north", "south", "east", "west")
    return tuple(
        ScoreTrainingMatch(
            canonical_event_id=f"event-{index:03d}",
            competition_id="league",
            event_date=date(2024, 1, 1) + timedelta(days=index),
            home_team_id=teams[index % len(teams)],
            away_team_id=teams[(index + 1) % len(teams)],
            home_goals=(index * 7) % 4,
            away_goals=(index * 5 + 1) % 3,
        )
        for index in range(count)
    )


def test_independent_and_dixon_coles_are_deterministic_and_reload_strictly(tmp_path) -> None:
    first = fit_independent_poisson(_matches())
    second = fit_independent_poisson(_matches())
    assert score_model_to_payload(first) == score_model_to_payload(second)
    assert first.model_family == INDEPENDENT_POISSON
    assert first.rho == 0.0
    assert first.diagnostics.converged

    dc = fit_dixon_coles(_matches())
    assert dc.model_family == DIXON_COLES
    assert dc.diagnostics.rho_candidates_evaluated == dc.configuration.rho_grid_points
    assert score_model_from_payload(score_model_to_payload(dc)) == dc

    artifact = write_score_model_artifact(
        root=tmp_path,
        relative_directory="model",
        model=dc,
    )
    loaded_artifact, loaded = load_score_model_artifact(
        root=tmp_path,
        relative_directory="model",
        expected_checksum=artifact.checksum_sha256,
        expected_artifact_id=artifact.artifact_id,
    )
    assert loaded_artifact.artifact_id == artifact.artifact_id
    assert loaded == dc


def test_score_surface_normalization_tail_and_calibration_invariants() -> None:
    distribution = joint_score_from_intensities(
        home_intensity=1.65,
        away_intensity=1.05,
        rho=-0.08,
        model_family=DIXON_COLES,
        prediction_cutoff=date(2025, 1, 1),
    )
    assert sum(map(sum, distribution.probabilities)) == pytest.approx(1.0, abs=1e-12)
    assert distribution.residual_tail_mass <= distribution.tail_tolerance

    calibrated = temperature_scale_distribution(distribution, temperature=1.1)
    assert sum(map(sum, calibrated.probabilities)) == pytest.approx(1.0, abs=1e-12)

    raked = rake_result_regions(
        distribution,
        home_probability=0.5,
        draw_probability=0.25,
        away_probability=0.25,
    )
    home = sum(
        value
        for home_goals, row in enumerate(raked.probabilities)
        for away_goals, value in enumerate(row)
        if home_goals > away_goals
    )
    draw = sum(
        value
        for home_goals, row in enumerate(raked.probabilities)
        for away_goals, value in enumerate(row)
        if home_goals == away_goals
    )
    assert home == pytest.approx(0.5)
    assert draw == pytest.approx(0.25)


def test_rho_zero_matches_independent_and_invalid_states_fail() -> None:
    independent = joint_score_from_intensities(
        home_intensity=1.4,
        away_intensity=1.2,
        prediction_cutoff=date(2025, 1, 1),
    )
    dc_zero = joint_score_from_intensities(
        home_intensity=1.4,
        away_intensity=1.2,
        rho=0.0,
        model_family=DIXON_COLES,
        prediction_cutoff=date(2025, 1, 1),
    )
    assert dc_zero.probabilities == independent.probabilities
    assert (
        dixon_coles_correction(
            home_goals=0,
            away_goals=0,
            home_intensity=1.4,
            away_intensity=1.2,
            rho=0.0,
        )
        == 1.0
    )
    with pytest.raises(ModelError, match="hard cap"):
        joint_score_from_intensities(
            home_intensity=12.0,
            away_intensity=11.0,
            prediction_cutoff=date(2025, 1, 1),
            configuration=ScoreModelConfiguration(
                minimum_grid_goals=2,
                maximum_grid_goals=3,
            ),
        )


def test_unseen_team_fallback_and_prediction_chronology() -> None:
    model = fit_independent_poisson(_matches())
    distribution = predict_joint_score(
        model,
        home_team_id="promoted",
        away_team_id="north",
        prediction_cutoff=model.training_end + timedelta(days=1),
    )
    assert distribution.fallback_used
    with pytest.raises(ModelError, match="after"):
        predict_joint_score(
            model,
            home_team_id="north",
            away_team_id="south",
            prediction_cutoff=model.training_end,
        )


def test_score_model_loader_rejects_parameter_tampering() -> None:
    payload = score_model_to_payload(fit_independent_poisson(_matches()))
    parameters = payload["parameters"]
    assert isinstance(parameters, dict)
    parameters["base_log_rate"] = float("nan")
    with pytest.raises(ArtifactError, match="finite"):
        score_model_from_payload(payload)


def _write_production_surface(tmp_path, *, predicted_at: datetime, event_start: datetime):
    distribution = joint_score_from_intensities(
        home_intensity=1.4,
        away_intensity=1.2,
        prediction_cutoff=predicted_at.date(),
    )
    return write_production_football_probability_artifact(
        root=tmp_path,
        relative_directory="production-probability",
        canonical_event_id="event-1",
        model_artifact_id="model-1",
        model_checksum_sha256="1" * 64,
        active_champion_role_revision=3,
        active_champion_transition_id="transition-1",
        predicted_at_utc=predicted_at,
        decision_as_of_utc=predicted_at,
        event_start_utc=event_start,
        upcoming_event_artifact_id="upcoming-1",
        upcoming_event_checksum_sha256="2" * 64,
        participant_registry_artifact_id="participants-1",
        participant_registry_checksum_sha256="3" * 64,
        distribution=distribution,
        participant_identity={
            "home_participant_identity_state": "registered-model-seen",
            "away_participant_identity_state": "registered-model-seen",
            "home_model_team_state": "model-seen",
            "away_model_team_state": "model-seen",
            "unseen_team_fallback_used": False,
            "unseen_participant_ids": [],
            "fallback_policy": None,
        },
    )


@pytest.mark.parametrize(
    "predicted_at",
    (
        datetime(2026, 8, 9, 15, tzinfo=UTC),
        datetime(2026, 8, 9, 15, 0, 1, tzinfo=UTC),
    ),
)
def test_production_probability_rejects_prediction_at_or_after_event_start(
    tmp_path, predicted_at
) -> None:
    with pytest.raises(ArtifactError, match="precede event start"):
        _write_production_surface(
            tmp_path,
            predicted_at=predicted_at,
            event_start=datetime(2026, 8, 9, 15, tzinfo=UTC),
        )


def test_production_probability_rejects_resigned_noncanonical_timestamp(tmp_path) -> None:
    artifact = _write_production_surface(
        tmp_path,
        predicted_at=datetime(2026, 8, 8, 12, tzinfo=UTC),
        event_start=datetime(2026, 8, 9, 15, tzinfo=UTC),
    )
    payload = dict(artifact.payload)
    payload["predicted_at_utc"] = "2026-08-08T12:00:00Z"
    document = build_analytical_artifact_document(
        artifact_type=artifact.artifact_type,
        schema_version=artifact.schema_version,
        payload=payload,
    )
    text = dumps_canonical_json(document) + "\n"
    directory = tmp_path / "production-probability"
    (directory / "manifest.json").write_text(text, encoding="utf-8", newline="\n")
    checksum = hashlib.sha256(text.encode()).hexdigest()
    (directory / "manifest_checksum.sha256").write_text(checksum + "\n")
    with pytest.raises(ArtifactError, match="lineage is invalid"):
        load_production_football_probability_artifact(
            root=tmp_path,
            relative_directory="production-probability",
        )
