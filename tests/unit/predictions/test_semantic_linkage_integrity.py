"""Focused regressions for PR #8 semantic-linkage microfix."""

from __future__ import annotations

from copy import deepcopy
from datetime import date, timedelta
from pathlib import Path

import pytest
from tests.unit.predictions.test_second_correction_regressions import (
    START,
    _dependency_metadata_for_opportunity,
    _prediction_from_opportunity,
    _quote_from_opportunity,
    _runtime,
)
from tests.unit.predictions.test_surgical_final_integrity import (
    _analysis_payload,
    _two_market_analysis_datasets,
)
from tests.unit.support.verified_opportunities import (
    basketball_selection,
    build_test_opportunity,
)

from sports_analytics.artifact_schemas import (
    validate_cross_dataset_integrity,
    validate_dataset_row_schema,
)
from sports_analytics.artifact_serializers import build_backtest_datasets
from sports_analytics.artifacts import (
    load_typed_analytical_artifact,
    write_typed_analytical_artifact,
)
from sports_analytics.backtesting.contracts import (
    BacktestFold,
    BacktestMode,
    FoldBacktestInput,
    SettledOpportunity,
    SettlementResult,
    StrategyConfiguration,
)
from sports_analytics.backtesting.engine import run_backtest
from sports_analytics.combinations.contracts import CombinationRules
from sports_analytics.core.exceptions import ArtifactError
from sports_analytics.opportunities.contracts import OpportunityFilter
from sports_analytics.predictions.provenance import PredictionProvenance
from sports_analytics.services.analysis import (
    ANALYSIS_ARTIFACT_SCHEMA,
    AnalysisMarketInput,
    AnalysisPublicationRequest,
    publish_analysis_artifact,
)
from sports_analytics.services.analysis_json import publish_analysis_with_paths
from sports_analytics.value.contracts import QuoteEvaluationMode, evaluate_complete_market


def _mutable_datasets(
    tmp_path: Path,
    *,
    namespace: str = "default",
) -> dict[str, list[dict[str, object]]]:
    isolated = tmp_path / namespace
    isolated.mkdir(parents=True, exist_ok=True)
    return {name: list(rows) for name, rows in _two_market_analysis_datasets(isolated).items()}


def _combination_leg_rows(
    datasets: dict[str, list[dict[str, object]]],
) -> tuple[dict[str, object], dict[str, object]]:
    combination = datasets["combinations"][0]
    opportunities_by_id = {row["opportunity_id"]: row for row in datasets["opportunities"]}
    left_id = combination["dependencies"][0]["left_opportunity_id"]
    right_id = combination["dependencies"][0]["right_opportunity_id"]
    return dict(opportunities_by_id[left_id]), dict(opportunities_by_id[right_id])


def test_opportunity_probability_mismatch_rejected(tmp_path: Path) -> None:
    datasets = _mutable_datasets(tmp_path)
    opportunity = dict(datasets["opportunities"][0])
    opportunity["model_probability"] = float(opportunity["model_probability"]) + 0.05
    datasets["opportunities"][0] = opportunity
    with pytest.raises(ArtifactError, match="model probability does not match prediction"):
        validate_cross_dataset_integrity({name: tuple(rows) for name, rows in datasets.items()})


def test_opportunity_evaluation_values_mismatch_rejected(tmp_path: Path) -> None:
    datasets = _mutable_datasets(tmp_path)
    opportunity = dict(datasets["opportunities"][0])
    opportunity["normalized_implied_probability"] = (
        float(opportunity["normalized_implied_probability"]) + 0.05
    )
    datasets["opportunities"][0] = opportunity
    with pytest.raises(ArtifactError, match="does not match authoritative source"):
        validate_cross_dataset_integrity({name: tuple(rows) for name, rows in datasets.items()})


def test_opportunity_lineage_timing_divergence_rejected(tmp_path: Path) -> None:
    datasets = _mutable_datasets(tmp_path, namespace="lineage-model")
    opportunity = dict(datasets["opportunities"][0])
    opportunity["model_artifact_id"] = "forged-model"
    datasets["opportunities"][0] = opportunity
    with pytest.raises(
        ArtifactError,
        match="model_artifact_id does not match authoritative source",
    ):
        validate_cross_dataset_integrity({name: tuple(rows) for name, rows in datasets.items()})

    datasets = _mutable_datasets(tmp_path, namespace="lineage-timing")
    opportunity = dict(datasets["opportunities"][0])
    opportunity["predicted_at_utc"] = "2024-03-09T12:00:00.000000Z"
    datasets["opportunities"][0] = opportunity
    with pytest.raises(ArtifactError, match="predicted_at_utc does not match authoritative source"):
        validate_cross_dataset_integrity({name: tuple(rows) for name, rows in datasets.items()})


def test_shared_participant_ids_cannot_be_relabelled_structurally_separate(
    tmp_path: Path,
) -> None:
    datasets = _mutable_datasets(tmp_path, namespace="shared-participants")
    left, right = _combination_leg_rows(datasets)
    shared_participant = "participant-shared"
    left["participant_ids"] = [shared_participant]
    right["participant_ids"] = [shared_participant]
    opportunities_by_id = {row["opportunity_id"]: row for row in datasets["opportunities"]}
    opportunities_by_id[left["opportunity_id"]] = left
    opportunities_by_id[right["opportunity_id"]] = right
    datasets["opportunities"] = list(opportunities_by_id.values())
    combination = dict(datasets["combinations"][0])
    dependencies = deepcopy(combination["dependencies"])
    assert isinstance(dependencies, list)
    for dependency in dependencies:
        assert isinstance(dependency, dict)
        dependency["classification"] = "structurally_separate"
        dependency["reason"] = "complete metadata proves distinct dependency keys and participants"
    combination["dependencies"] = dependencies
    datasets["combinations"][0] = combination
    with pytest.raises(ArtifactError, match="not structurally separate"):
        validate_cross_dataset_integrity({name: tuple(rows) for name, rows in datasets.items()})


def test_shared_dependency_keys_cannot_be_relabelled_structurally_separate(
    tmp_path: Path,
) -> None:
    datasets = _mutable_datasets(tmp_path, namespace="shared-dependency-keys")
    left, right = _combination_leg_rows(datasets)
    shared_key = "event:shared-dependency"
    left["dependency_keys"] = [shared_key]
    right["dependency_keys"] = [shared_key]
    opportunities_by_id = {row["opportunity_id"]: row for row in datasets["opportunities"]}
    opportunities_by_id[left["opportunity_id"]] = left
    opportunities_by_id[right["opportunity_id"]] = right
    datasets["opportunities"] = list(opportunities_by_id.values())
    combination = dict(datasets["combinations"][0])
    dependencies = deepcopy(combination["dependencies"])
    assert isinstance(dependencies, list)
    for dependency in dependencies:
        assert isinstance(dependency, dict)
        dependency["classification"] = "structurally_separate"
        dependency["reason"] = "complete metadata proves distinct dependency keys and participants"
    combination["dependencies"] = dependencies
    datasets["combinations"][0] = combination
    with pytest.raises(ArtifactError, match="not structurally separate"):
        validate_cross_dataset_integrity({name: tuple(rows) for name, rows in datasets.items()})


def test_persisted_dependency_reason_tampering_rejected(tmp_path: Path) -> None:
    datasets = _mutable_datasets(tmp_path)
    combination = dict(datasets["combinations"][0])
    dependencies = deepcopy(combination["dependencies"])
    assert isinstance(dependencies, list) and dependencies
    dependency = dict(dependencies[0])
    dependency["reason"] = "forged structural independence claim"
    dependencies[0] = dependency
    combination["dependencies"] = dependencies
    datasets["combinations"][0] = combination
    with pytest.raises(ArtifactError, match="reason does not match policy"):
        validate_cross_dataset_integrity({name: tuple(rows) for name, rows in datasets.items()})


def test_accepted_rank_true_rejected(tmp_path: Path) -> None:
    datasets = _mutable_datasets(tmp_path)
    decision = dict(datasets["opportunity_decisions"][0])
    decision["accepted_rank"] = True
    with pytest.raises(ArtifactError, match="accepted_rank must be a JSON integer"):
        validate_dataset_row_schema(
            "opportunity_decisions",
            decision,
            version="opportunity-decisions-v2",
        )
    with pytest.raises(ArtifactError, match="accepted_rank must be a JSON integer"):
        datasets["opportunity_decisions"][0] = decision
        validate_cross_dataset_integrity({name: tuple(rows) for name, rows in datasets.items()})


def test_valid_analysis_and_backtest_artifacts_still_reload(tmp_path: Path) -> None:
    paths = _runtime(tmp_path)
    first = build_test_opportunity(
        "1",
        event_id="event-1",
        start=START,
        quoted=START - timedelta(hours=2),
        predicted_at_utc=START - timedelta(hours=3),
        source_observed_at_utc=START - timedelta(hours=1),
    )
    second = build_test_opportunity(
        "2",
        event_id="event-2",
        start=START + timedelta(days=1),
        quoted=START - timedelta(hours=2),
        predicted_at_utc=START - timedelta(hours=3),
        source_observed_at_utc=START - timedelta(hours=1),
        selection=basketball_selection(
            sport_code="tennis",
            market_key="tennis.match-winner.full-match",
            outcome="player-z",
        ),
    )
    published = publish_analysis_artifact(
        paths=paths,
        request=AnalysisPublicationRequest(
            markets=(
                AnalysisMarketInput(
                    prediction=_prediction_from_opportunity(first),
                    quote=_quote_from_opportunity(first),
                    dependency_metadata=_dependency_metadata_for_opportunity(
                        first,
                        event_key="event-1",
                        participant="participant-event-1",
                    ),
                ),
                AnalysisMarketInput(
                    prediction=_prediction_from_opportunity(second),
                    quote=_quote_from_opportunity(second),
                    dependency_metadata=_dependency_metadata_for_opportunity(
                        second,
                        event_key="event-2",
                        participant="participant-event-2",
                    ),
                ),
            ),
            mode=QuoteEvaluationMode.LIVE_SAFE,
            filters=OpportunityFilter(minimum_edge=-1, minimum_expected_value=-1),
            combination_rules=CombinationRules(
                minimum_legs=2,
                maximum_legs=2,
                allow_multiple_sports=True,
                allow_multiple_dates=True,
            ),
            provenance=PredictionProvenance.SYNTHETIC_CONTRACT,
        ),
    )
    loaded_analysis = load_typed_analytical_artifact(
        root=paths.exports_directory,
        relative_directory=published.relative_directory,
        expected_kind="analysis",
        expected_schema_version=ANALYSIS_ARTIFACT_SCHEMA,
        expected_checksum=published.checksum_sha256,
    )
    assert loaded_analysis.dataset("combinations").row_count >= 1
    fold = BacktestFold(
        fold_id="fold-1",
        train_start_date=date(2024, 1, 1),
        train_end_date=date(2024, 2, 1),
        calibration_start_date=date(2024, 2, 2),
        calibration_end_date=date(2024, 3, 2),
        test_start_date=START.date(),
        test_end_date=(START + timedelta(days=1)).date(),
    )
    strategy = StrategyConfiguration(
        strategy_version="v1",
        mode=BacktestMode.TIMESTAMPED_SYNTHETIC,
        opportunity_filter=OpportunityFilter(minimum_edge=-1),
        include_combinations=True,
        combination_rules=CombinationRules(
            minimum_legs=2,
            maximum_legs=2,
            allow_multiple_sports=True,
            allow_multiple_dates=True,
        ),
    )
    result = run_backtest(
        (
            FoldBacktestInput(
                fold=fold,
                candidates=(
                    SettledOpportunity(first, SettlementResult.WIN),
                    SettledOpportunity(second, SettlementResult.WIN),
                ),
            ),
        ),
        strategy=strategy,
    )
    predictions = (
        _prediction_from_opportunity(first),
        _prediction_from_opportunity(second),
    )
    evaluations = (
        evaluate_complete_market(
            prediction=_prediction_from_opportunity(first),
            quote=_quote_from_opportunity(first),
            mode=QuoteEvaluationMode.LIVE_SAFE,
        ),
        evaluate_complete_market(
            prediction=_prediction_from_opportunity(second),
            quote=_quote_from_opportunity(second),
            mode=QuoteEvaluationMode.LIVE_SAFE,
        ),
    )
    datasets = build_backtest_datasets(
        result=result,
        predictions=predictions,
        evaluations=evaluations,
        feature_artifact_id="feature-1",
        feature_manifest_checksum_sha256="b" * 64,
        input_snapshots=(),
        random_seed=1,
        test_event_count=2,
        complete_quote_event_count=2,
        quote_coverage=1.0,
        provenance="synthetic-contract",
    )
    validate_cross_dataset_integrity(datasets)
    backtest = write_typed_analytical_artifact(
        root=paths.exports_directory,
        relative_directory="backtests/semantic-linkage",
        artifact_kind="backtest",
        schema_version="football-1x2-closing-backtest-v2",
        datasets=datasets,
    )
    loaded_backtest = load_typed_analytical_artifact(
        root=paths.exports_directory,
        relative_directory=backtest.relative_directory,
        expected_kind="backtest",
        expected_schema_version="football-1x2-closing-backtest-v2",
        expected_checksum=backtest.checksum_sha256,
    )
    assert loaded_backtest.dataset("settlements").row_count >= 1
    assert datasets["combinations"]


def test_deterministic_publication_remains_unchanged(tmp_path: Path) -> None:
    paths = _runtime(tmp_path)
    payload = {**_analysis_payload(), "provenance": "synthetic-contract"}
    first = publish_analysis_with_paths(payload, paths=paths)
    second = publish_analysis_with_paths(
        {**payload, "relative_directory": f"{first['relative_directory']}-repeat"},
        paths=paths,
    )
    assert first["analysis_run_id"] == second["analysis_run_id"]
    assert first["artifact_id"] == second["artifact_id"]
    assert first["checksum_sha256"] == second["checksum_sha256"]
