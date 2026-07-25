"""Focused regressions for PR #8 artifact-semantics microfix."""

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
from sports_analytics.artifact_serializers import (
    build_backtest_datasets,
    serialize_opportunity_row,
)
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
from sports_analytics.opportunities.identity import derive_opportunity_id
from sports_analytics.predictions.provenance import PredictionProvenance
from sports_analytics.services.analysis import (
    ANALYSIS_ARTIFACT_SCHEMA,
    AnalysisMarketInput,
    AnalysisPublicationRequest,
    publish_analysis_artifact,
)
from sports_analytics.services.analysis_json import publish_analysis_with_paths
from sports_analytics.value.contracts import QuoteEvaluationMode, evaluate_complete_market

_NON_IDENTITY_FIELDS = frozenset(
    {
        "opportunity_id",
        "schema_version",
        "model_trained_through_date",
        "model_calibrated_through_date",
    }
)


def _mutable_datasets(
    tmp_path: Path,
    *,
    namespace: str = "default",
) -> dict[str, list[dict[str, object]]]:
    isolated = tmp_path / namespace
    isolated.mkdir(parents=True, exist_ok=True)
    return {name: list(rows) for name, rows in _two_market_analysis_datasets(isolated).items()}


def _recompute_opportunity_row(row: dict[str, object]) -> dict[str, object]:
    forged = dict(row)
    payload = {key: forged[key] for key in forged if key not in _NON_IDENTITY_FIELDS}
    forged["opportunity_id"] = derive_opportunity_id(payload=payload)
    return forged


def _replace_opportunity_row(
    datasets: dict[str, list[dict[str, object]]],
    *,
    index: int,
    row: dict[str, object],
) -> None:
    previous_id = datasets["opportunities"][index]["opportunity_id"]
    updated = _recompute_opportunity_row(row)
    datasets["opportunities"][index] = updated
    new_id = updated["opportunity_id"]
    if new_id == previous_id:
        return
    datasets["opportunity_decisions"] = [
        {**decision, "opportunity_id": new_id}
        if decision["opportunity_id"] == previous_id
        else dict(decision)
        for decision in datasets["opportunity_decisions"]
    ]
    for combination in datasets.get("combinations", []):
        combination["opportunity_ids"] = [
            new_id if opportunity_id == previous_id else opportunity_id
            for opportunity_id in combination["opportunity_ids"]
        ]
        for dependency in combination.get("dependencies", []):
            if dependency.get("left_opportunity_id") == previous_id:
                dependency["left_opportunity_id"] = new_id
            if dependency.get("right_opportunity_id") == previous_id:
                dependency["right_opportunity_id"] = new_id
    datasets["rejections"] = [
        {
            **rejection,
            "opportunity_id": new_id
            if rejection.get("opportunity_id") == previous_id
            else rejection.get("opportunity_id"),
            "opportunity_ids": [
                new_id if opportunity_id == previous_id else opportunity_id
                for opportunity_id in rejection.get("opportunity_ids", [])
            ],
        }
        for rejection in datasets.get("rejections", [])
    ]


def test_forged_early_decision_as_of_rejected(tmp_path: Path) -> None:
    datasets = _mutable_datasets(tmp_path, namespace="early-decision")
    _replace_opportunity_row(
        datasets,
        index=0,
        row={
            **dict(datasets["opportunities"][0]),
            "decision_as_of_utc": "2024-03-09T12:00:00.000000Z",
        },
    )
    with pytest.raises(ArtifactError, match="derived live-safe timing"):
        validate_cross_dataset_integrity({name: tuple(rows) for name, rows in datasets.items()})


def test_live_safe_opportunity_without_quoted_at_rejected(tmp_path: Path) -> None:
    datasets = _mutable_datasets(tmp_path, namespace="missing-quote-time")
    opportunity = _recompute_opportunity_row(
        {
            **dict(datasets["opportunities"][0]),
            "quoted_at_utc": None,
        }
    )
    with pytest.raises(ArtifactError, match="requires quoted_at_utc"):
        validate_dataset_row_schema("opportunities", opportunity, version="opportunities-v2")


def test_live_safe_decision_at_or_after_event_start_rejected(tmp_path: Path) -> None:
    datasets = _mutable_datasets(tmp_path, namespace="late-decision")
    opportunity_row = dict(datasets["opportunities"][0])
    _replace_opportunity_row(
        datasets,
        index=0,
        row={
            **opportunity_row,
            "decision_as_of_utc": opportunity_row["event_start_utc"],
        },
    )
    with pytest.raises(
        ArtifactError,
        match="derived live-safe timing|strictly before event_start_utc",
    ):
        validate_cross_dataset_integrity({name: tuple(rows) for name, rows in datasets.items()})


def test_closing_benchmark_decision_different_from_event_start_rejected() -> None:
    opportunity = build_test_opportunity(
        "closing",
        event_id="event-1",
        start=START,
        mode=QuoteEvaluationMode.CLOSING_LINE_HISTORICAL_BENCHMARK,
    )
    row = serialize_opportunity_row(opportunity)
    forged = _recompute_opportunity_row(
        {
            **row,
            "decision_as_of_utc": "2024-03-09T12:00:00.000000Z",
        }
    )
    with pytest.raises(ArtifactError, match="must equal event_start_utc"):
        validate_dataset_row_schema("opportunities", forged, version="opportunities-v2")


def test_forged_prediction_quality_passed_rejected(tmp_path: Path) -> None:
    datasets = _mutable_datasets(tmp_path, namespace="forged-quality")
    opportunity_row = dict(datasets["opportunities"][0])
    prediction_id = opportunity_row["prediction_id"]
    predictions = [dict(row) for row in datasets["predictions"]]
    prediction = next(row for row in predictions if row["prediction_id"] == prediction_id)
    quality = dict(prediction["quality"])
    quality["data_quality_passed"] = False
    prediction["quality"] = quality
    datasets["predictions"] = predictions
    _replace_opportunity_row(
        datasets,
        index=0,
        row={
            **opportunity_row,
            "prediction_quality_passed": True,
        },
    )
    with pytest.raises(ArtifactError, match="prediction_quality_passed does not match"):
        validate_cross_dataset_integrity({name: tuple(rows) for name, rows in datasets.items()})


def test_evaluation_version_mismatch_rejected(tmp_path: Path) -> None:
    datasets = _mutable_datasets(tmp_path, namespace="evaluation-version")
    _replace_opportunity_row(
        datasets,
        index=0,
        row={
            **dict(datasets["opportunities"][0]),
            "evaluation_version": "forged-evaluation-version",
        },
    )
    with pytest.raises(
        ArtifactError, match="evaluation_version does not match authoritative source"
    ):
        validate_cross_dataset_integrity({name: tuple(rows) for name, rows in datasets.items()})


def test_duplicate_decision_under_another_filter_config_id_rejected(tmp_path: Path) -> None:
    datasets = _mutable_datasets(tmp_path, namespace="duplicate-filter")
    duplicate = deepcopy(datasets["opportunity_decisions"][0])
    duplicate["filter_config_id"] = "alternate-filter-config"
    datasets["opportunity_decisions"].append(duplicate)
    with pytest.raises(ArtifactError, match="match opportunity count exactly|exactly once"):
        validate_cross_dataset_integrity({name: tuple(rows) for name, rows in datasets.items()})


def test_valid_analysis_and_backtest_artifacts_still_publish_and_reload(tmp_path: Path) -> None:
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
        relative_directory="backtests/artifact-semantics",
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
