from __future__ import annotations

from datetime import date, timedelta

import numpy as np

from sports_analytics.models.football_evaluation import (
    BlockBootstrapConfiguration,
    EvaluationProvenance,
    assess_production_evidence,
    multiclass_calibration_diagnostics,
    paired_temporal_block_interval,
    rho_stability_diagnostics,
)
from sports_analytics.models.football_scores import (
    ScoreModelConfiguration,
    ScoreTrainingMatch,
    fit_dixon_coles,
)


def test_fixture_evidence_can_never_be_production_eligible() -> None:
    result = assess_production_evidence(
        provenance=EvaluationProvenance.SYNTHETIC_CONTRACT,
        completed_matches=10_000,
        competition_match_counts=(10_000,),
        fold_row_counts=((8_000, 500, 500),) * 3,
        temporal_start=date(2020, 1, 1),
        temporal_end=date(2025, 1, 1),
        missing_target_ratio=0.0,
        prediction_coverage=1.0,
        unseen_team_fallback_ratio=0.0,
        all_candidates_converged=True,
    )
    assert not result.eligible
    assert result.state == "insufficient-real-evaluation-data"
    assert "evaluation-provenance-not-verified-historical" in result.reason_codes


def test_calibration_diagnostics_refuse_fixture_sized_samples() -> None:
    result = multiclass_calibration_diagnostics(
        np.asarray([[0.5, 0.3, 0.2]] * 20),
        np.asarray([0] * 20),
    )
    assert result.state == "insufficient-sample"
    assert result.expected_calibration_error is None


def test_calibration_reliability_bins_cover_real_sample() -> None:
    probabilities = np.tile(np.asarray([[0.5, 0.3, 0.2]]), (100, 1))
    targets = np.asarray([0, 1, 2, 0] * 25)
    result = multiclass_calibration_diagnostics(probabilities, targets)
    assert result.state == "available"
    assert sum(item[2] for item in result.reliability_bins) == 100


def test_temporal_block_bootstrap_is_deterministic_and_paired() -> None:
    candidate = tuple(0.4 + (index % 7) / 100 for index in range(160))
    baseline = tuple(value + 0.05 for value in candidate)
    configuration = BlockBootstrapConfiguration(
        block_size=10,
        replicates=100,
        seed=7,
        minimum_rows=100,
    )
    first = paired_temporal_block_interval(
        candidate,
        baseline,
        configuration=configuration,
    )
    second = paired_temporal_block_interval(
        candidate,
        baseline,
        configuration=configuration,
    )
    assert first == second
    assert first.state == "available"
    assert first.upper is not None and first.upper < 0.0


def test_rho_boundary_and_small_low_score_sample_are_explicit_warnings() -> None:
    teams = ("a", "b", "c", "d")
    matches = tuple(
        ScoreTrainingMatch(
            canonical_event_id=f"event-{index}",
            competition_id="league",
            event_date=date(2024, 1, 1) + timedelta(days=index),
            home_team_id=teams[index % 4],
            away_team_id=teams[(index + 1) % 4],
            home_goals=index % 3,
            away_goals=(index + 1) % 2,
        )
        for index in range(32)
    )
    model = fit_dixon_coles(
        matches,
        configuration=ScoreModelConfiguration(minimum_matches=12),
    )
    diagnostics = rho_stability_diagnostics(model, matches)
    assert diagnostics.correction_factors_positive
    assert diagnostics.low_score_matches <= len(matches)
    if diagnostics.near_boundary:
        assert "rho-near-search-boundary" in diagnostics.warning_codes
