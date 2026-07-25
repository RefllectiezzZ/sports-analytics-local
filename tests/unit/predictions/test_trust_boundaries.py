"""Trust boundary, identity, schema, and integrity regression tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from tests.unit.predictions.test_prediction_value_layer import _prediction, _quote
from tests.unit.support.verified_opportunities import build_test_opportunity

from sports_analytics.artifact_schemas import (
    validate_cross_dataset_integrity,
    validate_dataset_row_schema,
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
from sports_analytics.combinations.contracts import CombinationRules, validate_combination_manual
from sports_analytics.core.exceptions import ArtifactError, BacktestError, OpportunityError
from sports_analytics.opportunities.contracts import Opportunity, OpportunityFilter
from sports_analytics.opportunities.identity import (
    derive_opportunity_id,
    opportunity_identity_payload,
)
from sports_analytics.predictions.contracts import (
    PredictionQualityFlags,
)
from sports_analytics.predictions.synthetic import build_synthetic_market_prediction
from sports_analytics.services.engine_cli import main as engine_main
from sports_analytics.value.timing import compute_decision_as_of

START = datetime(2024, 3, 10, 15, tzinfo=UTC)


def test_caller_supplied_verification_flags_cannot_create_production_eligible_prediction() -> None:
    base = _prediction()
    with pytest.raises(ValueError, match="production eligibility"):
        build_synthetic_market_prediction(
            canonical_event_id=base.canonical_event_id,
            event_start_utc=base.event_start_utc,
            predicted_at_utc=base.predicted_at_utc,
            feature_available_at_utc=base.feature_available_at_utc,
            lineage=base.lineage,
            probabilities=base.probabilities,
            quality=PredictionQualityFlags(
                calibrated=True,
                model_artifact_verified=True,
                feature_artifact_verified=True,
                sufficient_history=True,
                data_quality_passed=True,
            ),
        )


def test_forged_opportunity_calculations_are_rejected() -> None:
    opportunity = build_test_opportunity("1", event_id="event-1", start=START)
    with pytest.raises(OpportunityError):
        replace(opportunity, expected_value=opportunity.expected_value + 0.5)


def test_opportunity_id_changes_when_material_input_changes() -> None:
    first = build_test_opportunity("1", event_id="event-1", start=START)
    second = build_test_opportunity("2", event_id="event-2", start=START + timedelta(days=1))
    assert first.opportunity_id != second.opportunity_id
    payload = opportunity_identity_payload(second)
    payload = {**payload, "provider_id": "book-b"}
    expected = derive_opportunity_id(payload=payload)
    assert expected != second.opportunity_id


def test_quote_validity_uses_common_decision_time() -> None:
    prediction = _prediction()
    quote = _quote(prediction)
    from sports_analytics.value.contracts import QuoteEvaluationMode

    decision = compute_decision_as_of(
        prediction=prediction,
        quote=quote,
        mode=QuoteEvaluationMode.LIVE_SAFE,
    )
    assert decision == max(
        prediction.predicted_at_utc,
        quote.quoted_at_utc,
        quote.source_observed_at_utc,
    )


def test_manual_unknown_dependencies_never_receive_product_probability() -> None:
    first = build_test_opportunity("1", event_id="event-1", start=START)
    missing = build_test_opportunity(
        "2",
        event_id="event-2",
        start=START + timedelta(days=1),
        dependency_keys=frozenset(),
        participant_ids=frozenset(),
        dependency_metadata_complete=False,
        quoted=START - timedelta(hours=2),
        predicted_at_utc=START - timedelta(hours=3),
        source_observed_at_utc=START - timedelta(hours=1),
    )
    result = validate_combination_manual((first, missing), rules=CombinationRules())
    assert not result.eligible
    assert result.combination is None
    assert any("unknown dependency" in reason for reason in result.rejection_reasons)


def test_allow_unknown_dependencies_flag_cannot_create_eligible_combination() -> None:
    first = build_test_opportunity("1", event_id="event-1", start=START)
    missing = build_test_opportunity(
        "2",
        event_id="event-2",
        start=START + timedelta(days=1),
        dependency_keys=frozenset(),
        participant_ids=frozenset(),
        dependency_metadata_complete=False,
        quoted=START - timedelta(hours=2),
        predicted_at_utc=START - timedelta(hours=3),
        source_observed_at_utc=START - timedelta(hours=1),
    )
    result = validate_combination_manual(
        (first, missing),
        rules=CombinationRules(allow_unknown_dependencies=True),
    )
    assert not result.eligible


def test_strategy_id_includes_combination_policy_fields() -> None:
    base = StrategyConfiguration(
        strategy_version="v1",
        mode=BacktestMode.TIMESTAMPED_SYNTHETIC,
        opportunity_filter=OpportunityFilter(),
        include_combinations=True,
        combination_rules=CombinationRules(minimum_joint_probability=0.1),
    )
    changed = StrategyConfiguration(
        strategy_version="v1",
        mode=BacktestMode.TIMESTAMPED_SYNTHETIC,
        opportunity_filter=OpportunityFilter(),
        include_combinations=True,
        combination_rules=CombinationRules(minimum_joint_probability=0.2),
    )
    assert base.strategy_id != changed.strategy_id


def test_changing_settlement_changes_backtest_result_id() -> None:
    first = build_test_opportunity("1", event_id="event-1", start=START)
    fold = BacktestFold(
        fold_id="fold-1",
        train_start_date=date(2024, 1, 1),
        train_end_date=date(2024, 2, 1),
        calibration_start_date=date(2024, 2, 2),
        calibration_end_date=date(2024, 3, 2),
        test_start_date=START.date(),
        test_end_date=START.date(),
    )
    strategy = StrategyConfiguration(
        strategy_version="v1",
        mode=BacktestMode.TIMESTAMPED_SYNTHETIC,
        opportunity_filter=OpportunityFilter(minimum_edge=-1, minimum_expected_value=-1),
    )
    win_input = FoldBacktestInput(
        fold=fold,
        candidates=(SettledOpportunity(first, SettlementResult.WIN),),
    )
    loss_input = FoldBacktestInput(
        fold=fold,
        candidates=(SettledOpportunity(first, SettlementResult.LOSS),),
    )
    win = run_backtest((win_input,), strategy=strategy)
    loss = run_backtest((loss_input,), strategy=strategy)
    assert win.backtest_result_id != loss.backtest_result_id


def test_prediction_probability_outside_unit_interval_rejected() -> None:
    row = {
        "prediction_id": "prediction-1",
        "schema_version": "predictions-v2",
        "canonical_event_id": "event-1",
        "event_start_utc": "2024-02-10T15:00:00Z",
        "predicted_at_utc": "2024-02-10T12:00:00Z",
        "feature_available_at_utc": "2024-02-10T11:00:00Z",
        "provenance": "synthetic-contract",
        "ordered_selection_ids": ["a", "b"],
        "probabilities": [
            {"selection_id": "a", "probability": 1.2},
            {"selection_id": "b", "probability": -0.2},
        ],
        "quality": {
            "calibrated": False,
            "model_artifact_verified": False,
            "feature_artifact_verified": False,
            "sufficient_history": False,
            "data_quality_passed": False,
        },
        "lineage": {
            "model_artifact_id": "model-1",
            "model_checksum_sha256": "a" * 64,
            "model_specification_version": "model-v1",
            "feature_artifact_id": "feature-1",
            "feature_manifest_checksum_sha256": "b" * 64,
            "feature_specification_version": "feature-v1",
            "feature_row_id": "event-1",
            "trained_through_date": "2024-02-01",
            "calibrated_through_date": "2024-02-02",
            "input_snapshots": [],
        },
    }
    with pytest.raises(ArtifactError, match="\\[0, 1\\]"):
        validate_dataset_row_schema("predictions", row, version="predictions-v2")


def test_orphan_decisions_rejected_by_cross_dataset_integrity() -> None:
    with pytest.raises(ArtifactError, match="opportunity decisions must cover every opportunity"):
        validate_cross_dataset_integrity(
            {
                "predictions": ({"prediction_id": "prediction-1"},),
                "opportunities": (),
                "opportunity_decisions": ({"opportunity_id": "missing"},),
            }
        )


def test_artifact_verification_cli_requires_schema(tmp_path, capsys) -> None:
    config = tmp_path / "settings.toml"
    config.write_text(
        "\n".join(
            (
                "[storage]",
                f'root_directory = "{(tmp_path / "storage").as_posix()}"',
                f'exports_directory = "{(tmp_path / "exports").as_posix()}"',
                f'sqlite_path = "{(tmp_path / "operational.sqlite3").as_posix()}"',
                "[logging]",
                "file_enabled = false",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(SystemExit) as exc:
        engine_main(
            [
                "--config",
                str(config),
                "--verify-analysis-artifact",
                "analysis/example",
            ]
        )
    assert exc.value.code == 2
    assert "artifact-schema" in capsys.readouterr().err


def test_missing_eligible_opportunity_lineage_is_rejected() -> None:
    opportunity = build_test_opportunity("1", event_id="event-1", start=START)
    with pytest.raises(OpportunityError, match="lineage|does not match canonical identity"):
        Opportunity(
            opportunity_id=opportunity.opportunity_id,
            canonical_event_id=opportunity.canonical_event_id,
            event_start_utc=opportunity.event_start_utc,
            selection=opportunity.selection,
            prediction_id=opportunity.prediction_id,
            predicted_at_utc=opportunity.predicted_at_utc,
            model_trained_through_date=opportunity.model_trained_through_date,
            model_calibrated_through_date=opportunity.model_calibrated_through_date,
            quote_observation_id=opportunity.quote_observation_id,
            quote_series_id=opportunity.quote_series_id,
            quoted_at_utc=opportunity.quoted_at_utc,
            source_observed_at_utc=opportunity.source_observed_at_utc,
            source_name=opportunity.source_name,
            provider_type=opportunity.provider_type,
            provider_id=opportunity.provider_id,
            evaluation_mode=opportunity.evaluation_mode,
            decimal_odds=opportunity.decimal_odds,
            model_probability=opportunity.model_probability,
            raw_implied_probability=opportunity.raw_implied_probability,
            normalized_implied_probability=opportunity.normalized_implied_probability,
            overround=opportunity.overround,
            edge=opportunity.edge,
            expected_value=opportunity.expected_value,
            decision_as_of_utc=opportunity.decision_as_of_utc,
            model_artifact_id="model-partial",
            model_checksum_sha256="",
            model_specification_version=opportunity.model_specification_version,
            feature_artifact_id=opportunity.feature_artifact_id,
            feature_manifest_checksum_sha256=opportunity.feature_manifest_checksum_sha256,
            feature_specification_version=opportunity.feature_specification_version,
            feature_row_id=opportunity.feature_row_id,
            dependency_keys=opportunity.dependency_keys,
            participant_ids=opportunity.participant_ids,
            dependency_metadata_complete=opportunity.dependency_metadata_complete,
            prediction_quality_passed=opportunity.prediction_quality_passed,
        )


def test_exact_dataset_schema_rejects_unknown_fields() -> None:
    row = {
        "prediction_id": "prediction-1",
        "schema_version": "predictions-v2",
        "canonical_event_id": "event-1",
        "event_start_utc": "2024-02-10T15:00:00Z",
        "predicted_at_utc": "2024-02-10T12:00:00Z",
        "ordered_selection_ids": ["a", "b"],
        "probabilities": [
            {"selection_id": "a", "probability": 0.6},
            {"selection_id": "b", "probability": 0.4},
        ],
        "unexpected": True,
    }
    with pytest.raises(ArtifactError, match="unknown fields"):
        validate_dataset_row_schema("predictions", row, version="predictions-v2")


def test_formula_inconsistent_settlement_rejected() -> None:
    row = {
        "bet_id": "bet-1",
        "schema_version": "settlements-v2",
        "fold_id": "fold-1",
        "kind": "single",
        "opportunity_ids": ["opportunity-1"],
        "decimal_odds": "2.0",
        "result": "win",
        "stake_units": "1",
        "returned_units": "0.5",
        "profit_units": "0.5",
    }
    with pytest.raises(ArtifactError, match="inconsistent"):
        validate_dataset_row_schema("settlements", row, version="settlements-v2")


def test_content_inconsistent_opportunity_id_rejected() -> None:
    opportunity = build_test_opportunity("1", event_id="event-1", start=START)
    with pytest.raises(OpportunityError, match="does not match canonical identity"):
        replace(opportunity, opportunity_id="forged-opportunity-id")


def test_changing_only_rejected_candidate_changes_backtest_result_id() -> None:
    accepted = build_test_opportunity("1", event_id="event-1", start=START)
    rejected_a = build_test_opportunity(
        "2",
        event_id="event-2",
        start=START + timedelta(days=1),
        model_probability=0.01,
        odds="50.0",
    )
    rejected_b = build_test_opportunity(
        "3",
        event_id="event-3",
        start=START + timedelta(days=2),
        model_probability=0.01,
        odds="50.0",
    )
    fold = BacktestFold(
        fold_id="fold-1",
        train_start_date=date(2024, 1, 1),
        train_end_date=date(2024, 2, 1),
        calibration_start_date=date(2024, 2, 2),
        calibration_end_date=date(2024, 3, 2),
        test_start_date=START.date(),
        test_end_date=(START + timedelta(days=2)).date(),
    )
    strategy = StrategyConfiguration(
        strategy_version="v1",
        mode=BacktestMode.TIMESTAMPED_SYNTHETIC,
        opportunity_filter=OpportunityFilter(minimum_edge=0.05),
    )
    first = run_backtest(
        (
            FoldBacktestInput(
                fold=fold,
                candidates=(
                    SettledOpportunity(accepted, SettlementResult.WIN),
                    SettledOpportunity(rejected_a, SettlementResult.LOSS),
                ),
            ),
        ),
        strategy=strategy,
    )
    second = run_backtest(
        (
            FoldBacktestInput(
                fold=fold,
                candidates=(
                    SettledOpportunity(accepted, SettlementResult.WIN),
                    SettledOpportunity(rejected_b, SettlementResult.LOSS),
                ),
            ),
        ),
        strategy=strategy,
    )
    assert first.backtest_result_id != second.backtest_result_id


def test_overlapping_test_events_rejected_across_folds() -> None:
    opportunity = build_test_opportunity("1", event_id="shared-event", start=START)
    fold_a = BacktestFold(
        fold_id="fold-a",
        train_start_date=date(2024, 1, 1),
        train_end_date=date(2024, 2, 1),
        calibration_start_date=date(2024, 2, 2),
        calibration_end_date=date(2024, 3, 2),
        test_start_date=START.date(),
        test_end_date=START.date(),
    )
    fold_b = BacktestFold(
        fold_id="fold-b",
        train_start_date=date(2024, 1, 1),
        train_end_date=date(2024, 2, 1),
        calibration_start_date=date(2024, 2, 2),
        calibration_end_date=date(2024, 3, 2),
        test_start_date=date(2024, 3, 11),
        test_end_date=date(2024, 3, 11),
    )
    strategy = StrategyConfiguration(
        strategy_version="v1",
        mode=BacktestMode.TIMESTAMPED_SYNTHETIC,
        opportunity_filter=OpportunityFilter(minimum_edge=-1, minimum_expected_value=-1),
    )
    with pytest.raises(BacktestError, match="overlapping test event"):
        run_backtest(
            (
                FoldBacktestInput(
                    fold=fold_a,
                    candidates=(SettledOpportunity(opportunity, SettlementResult.WIN),),
                ),
                FoldBacktestInput(
                    fold=fold_b,
                    candidates=(SettledOpportunity(opportunity, SettlementResult.LOSS),),
                ),
            ),
            strategy=strategy,
        )


def test_backtest_reports_rejection_counts_by_reason() -> None:
    weak = build_test_opportunity(
        "1",
        event_id="event-1",
        start=START,
        model_probability=0.01,
        odds="50.0",
    )
    fold = BacktestFold(
        fold_id="fold-1",
        train_start_date=date(2024, 1, 1),
        train_end_date=date(2024, 2, 1),
        calibration_start_date=date(2024, 2, 2),
        calibration_end_date=date(2024, 3, 2),
        test_start_date=START.date(),
        test_end_date=START.date(),
    )
    result = run_backtest(
        (
            FoldBacktestInput(
                fold=fold,
                candidates=(SettledOpportunity(weak, SettlementResult.LOSS),),
            ),
        ),
        strategy=StrategyConfiguration(
            strategy_version="v1",
            mode=BacktestMode.TIMESTAMPED_SYNTHETIC,
            opportunity_filter=OpportunityFilter(minimum_edge=0.5),
        ),
    )
    assert result.metrics.rejection_count >= 1
    assert result.metrics.rejection_counts_by_reason
    assert all(sample_size > 0 for _, _, sample_size in result.metrics.rejection_counts_by_reason)


def test_combination_builder_rejections_persisted_in_backtest() -> None:
    first = build_test_opportunity("1", event_id="event-1", start=START)
    missing = build_test_opportunity(
        "2",
        event_id="event-2",
        start=START + timedelta(days=1),
        dependency_keys=frozenset(),
        participant_ids=frozenset(),
        dependency_metadata_complete=False,
    )
    fold = BacktestFold(
        fold_id="fold-1",
        train_start_date=date(2024, 1, 1),
        train_end_date=date(2024, 2, 1),
        calibration_start_date=date(2024, 2, 2),
        calibration_end_date=date(2024, 3, 2),
        test_start_date=START.date(),
        test_end_date=(START + timedelta(days=1)).date(),
    )
    result = run_backtest(
        (
            FoldBacktestInput(
                fold=fold,
                candidates=(
                    SettledOpportunity(first, SettlementResult.WIN),
                    SettledOpportunity(missing, SettlementResult.LOSS),
                ),
            ),
        ),
        strategy=StrategyConfiguration(
            strategy_version="v1",
            mode=BacktestMode.TIMESTAMPED_SYNTHETIC,
            opportunity_filter=OpportunityFilter(minimum_edge=-1, minimum_expected_value=-1),
            include_combinations=True,
            combination_rules=CombinationRules(minimum_legs=2, maximum_legs=2),
        ),
    )
    assert result.combination_rejections
    assert any("unknown dependency" in item.reason for item in result.combination_rejections)


@pytest.mark.parametrize(
    ("field_name", "value"),
    (
        ("minimum_legs", 3),
        ("maximum_legs", 3),
        ("minimum_joint_probability", 0.05),
        ("minimum_expected_value", 0.1),
        ("maximum_candidates", 25),
        ("maximum_evaluated_combinations", 5000),
        ("maximum_outputs", 50),
        ("allow_multiple_sports", False),
        ("allow_multiple_dates", False),
    ),
)
def test_every_combination_rules_field_affects_strategy_id(
    field_name: str,
    value: object,
) -> None:
    base = StrategyConfiguration(
        strategy_version="v1",
        mode=BacktestMode.TIMESTAMPED_SYNTHETIC,
        opportunity_filter=OpportunityFilter(),
        include_combinations=True,
        combination_rules=CombinationRules(),
    )
    changed_rules = replace(CombinationRules(), **{field_name: value})
    changed = StrategyConfiguration(
        strategy_version="v1",
        mode=BacktestMode.TIMESTAMPED_SYNTHETIC,
        opportunity_filter=OpportunityFilter(),
        include_combinations=True,
        combination_rules=changed_rules,
    )
    assert base.strategy_id != changed.strategy_id


def test_analysis_publication_is_deterministic_and_reloadable(tmp_path: Path) -> None:
    from tests.unit.predictions.test_prediction_value_layer import _prediction, _quote

    from sports_analytics.artifacts import load_typed_analytical_artifact
    from sports_analytics.core.paths import resolve_paths
    from sports_analytics.core.settings import load_settings
    from sports_analytics.services.analysis_json import publish_analysis_with_paths

    def _paths(root: Path):
        settings = load_settings(
            config_path=None,
            env_file=None,
            environ={
                "SPORTS_ANALYTICS_STORAGE__ROOT_DIRECTORY": str(root / "storage"),
                "SPORTS_ANALYTICS_STORAGE__EXPORTS_DIRECTORY": str(root / "exports"),
                "SPORTS_ANALYTICS_STORAGE__SQLITE_PATH": str(root / "operational.sqlite3"),
            },
        )
        resolved = resolve_paths(settings, root)
        resolved.exports_directory.mkdir(parents=True, exist_ok=True)
        return resolved

    prediction = _prediction()
    quote = _quote(prediction)
    payload = {
        "prediction": {
            "canonical_event_id": prediction.canonical_event_id,
            "event_start_utc": prediction.event_start_utc.isoformat().replace("+00:00", "Z"),
            "predicted_at_utc": prediction.predicted_at_utc.isoformat().replace("+00:00", "Z"),
            "feature_available_at_utc": prediction.feature_available_at_utc.isoformat().replace(
                "+00:00",
                "Z",
            ),
            "lineage": {
                "model_artifact_id": prediction.lineage.model_artifact_id,
                "model_checksum_sha256": prediction.lineage.model_checksum_sha256,
                "model_specification_version": prediction.lineage.model_specification_version,
                "feature_artifact_id": prediction.lineage.feature_artifact_id,
                "feature_manifest_checksum_sha256": (
                    prediction.lineage.feature_manifest_checksum_sha256
                ),
                "feature_specification_version": prediction.lineage.feature_specification_version,
                "feature_row_id": prediction.lineage.feature_row_id,
                "trained_through_date": prediction.lineage.trained_through_date.isoformat(),
                "calibrated_through_date": prediction.lineage.calibrated_through_date.isoformat(),
            },
            "probabilities": [
                {
                    "sport_code": item.selection.sport_code,
                    "market_family": item.selection.market_family,
                    "market_key": item.selection.market_key,
                    "market_period": item.selection.market_period,
                    "participant_scope": item.selection.participant_scope,
                    "canonical_participant_id": item.selection.canonical_participant_id,
                    "line_type": item.selection.line_type,
                    "line_value": (
                        None
                        if item.selection.line_value is None
                        else str(item.selection.line_value)
                    ),
                    "outcome_key": item.selection.outcome_key,
                    "probability": item.probability,
                }
                for item in prediction.probabilities
            ],
        },
        "quote": {
            "canonical_event_id": quote.canonical_event_id,
            "source_name": quote.source_name,
            "provider_type": quote.provider_type,
            "provider_id": quote.provider_id,
            "quote_phase": quote.quote_phase,
            "source_observed_at_utc": quote.source_observed_at_utc.isoformat().replace(
                "+00:00",
                "Z",
            ),
            "quoted_at_utc": quote.quoted_at_utc.isoformat().replace("+00:00", "Z"),
            "quote_timestamp_precision": quote.quote_timestamp_precision,
            "selections": [
                {
                    "sport_code": priced.selection.sport_code,
                    "market_family": priced.selection.market_family,
                    "market_key": priced.selection.market_key,
                    "market_period": priced.selection.market_period,
                    "participant_scope": priced.selection.participant_scope,
                    "canonical_participant_id": priced.selection.canonical_participant_id,
                    "line_type": priced.selection.line_type,
                    "line_value": (
                        None
                        if priced.selection.line_value is None
                        else str(priced.selection.line_value)
                    ),
                    "outcome_key": priced.selection.outcome_key,
                    "decimal_odds": str(priced.decimal_odds),
                    "quote_series_id": priced.quote_series_id,
                    "quote_observation_id": priced.quote_observation_id,
                }
                for priced in quote.selections
            ],
        },
        "mode": "live-safe",
        "provenance": "synthetic-contract",
        "filters": {},
        "relative_directory": "analysis/test-run",
    }
    paths_one = _paths(tmp_path / "one")
    paths_two = _paths(tmp_path / "two")
    first = publish_analysis_with_paths(payload, paths=paths_one)
    second = publish_analysis_with_paths(payload, paths=paths_two)
    assert first["artifact_id"] == second["artifact_id"]
    assert first["checksum_sha256"] == second["checksum_sha256"]
    loaded = load_typed_analytical_artifact(
        root=paths_one.exports_directory,
        relative_directory=str(first["relative_directory"]),
        expected_kind="analysis",
        expected_schema_version="analysis-v2",
        expected_checksum=str(first["checksum_sha256"]),
    )
    assert loaded.artifact_id == first["artifact_id"]


def test_verified_model_inference_produces_complete_prediction_record(tmp_path: Path) -> None:
    from tests.helpers_snapshots import database_path, prepare, publication_service
    from tests.helpers_training import synthetic_season_csv

    from sports_analytics.core.paths import resolve_paths
    from sports_analytics.core.settings import load_settings
    from sports_analytics.evaluation.temporal import TemporalSplitConfig
    from sports_analytics.features.football.datasets import load_feature_artifact
    from sports_analytics.predictions.provenance import PredictionProvenance
    from sports_analytics.predictions.service import (
        VerifiedPredictionRequest,
        generate_verified_football_1x2_prediction,
    )
    from sports_analytics.services.training import (
        FeatureBuildRequest,
        TrainRequest,
        build_football_1x2_features,
        train_football_1x2_model,
    )

    manifest = prepare(
        tmp_path,
        snapshot_id="11111111-1111-4111-8111-111111111111",
        snapshots_directory=tmp_path / "snapshots",
        season_label="2023-2024",
        source_season_code="2324",
        content=synthetic_season_csv(
            season_start_year=2023,
            match_count=30,
            include_closing_avg=True,
        ),
    )
    database = database_path(tmp_path / "db")
    service = publication_service(database, tmp_path / "snapshots")
    published = service.publish_or_reuse(prepared=manifest, actor="test", correlation_id="job-1")
    settings = load_settings(
        config_path=None,
        env_file=None,
        environ={
            "SPORTS_ANALYTICS_STORAGE__ROOT_DIRECTORY": str(tmp_path / "storage"),
            "SPORTS_ANALYTICS_STORAGE__SNAPSHOTS_DIRECTORY": str(tmp_path / "snapshots"),
            "SPORTS_ANALYTICS_STORAGE__FEATURES_DIRECTORY": str(tmp_path / "features"),
            "SPORTS_ANALYTICS_STORAGE__MODELS_DIRECTORY": str(tmp_path / "models"),
            "SPORTS_ANALYTICS_STORAGE__SQLITE_PATH": str(tmp_path / "operational.sqlite3"),
        },
    )
    paths = resolve_paths(settings, tmp_path)
    paths.features_directory.mkdir(parents=True, exist_ok=True)
    paths.models_directory.mkdir(parents=True, exist_ok=True)
    artifact = build_football_1x2_features(
        paths=paths,
        request=FeatureBuildRequest(
            relative_manifest_paths=(published.snapshot_relative_path,),
            minimum_events=20,
            split_config=TemporalSplitConfig(
                min_train_rows=15,
                min_calibration_rows=5,
                min_test_rows=5,
                step_rows=4,
                maximum_folds=2,
            ),
        ),
    )
    trained = train_football_1x2_model(
        paths=paths,
        request=TrainRequest(
            feature_relative_directory=artifact.relative_directory,
            feature_manifest_checksum=artifact.manifest_checksum_sha256,
            random_seed=42,
        ),
    )
    manifest_doc, vectors, _, _ = load_feature_artifact(
        features_root=paths.features_directory,
        relative_directory=artifact.relative_directory,
        expected_manifest_checksum=artifact.manifest_checksum_sha256,
    )

    from sports_analytics.predictions.replay import derive_historical_replay_cutoff_utc

    vectors_with_start = [item for item in vectors if item.metadata.scheduled_start_utc is not None]
    if not vectors_with_start:
        pytest.skip("synthetic fixture lacks scheduled_start_utc required for historical replay")
    vector = max(vectors_with_start, key=lambda item: item.metadata.event_date)
    event_start = vector.metadata.scheduled_start_utc
    assert event_start is not None
    predicted_at = derive_historical_replay_cutoff_utc(event_start)
    prediction = generate_verified_football_1x2_prediction(
        paths=paths,
        request=VerifiedPredictionRequest(
            model_relative_path=trained.final_artifact_relative_directory,
            model_checksum_sha256=trained.final_artifact_checksum,
            feature_relative_directory=artifact.relative_directory,
            feature_manifest_checksum_sha256=artifact.manifest_checksum_sha256,
            canonical_event_id=vector.metadata.canonical_event_id,
            event_start_utc=event_start,
            predicted_at_utc=predicted_at,
            provenance=PredictionProvenance.HISTORICAL_REPLAY,
        ),
    )
    assert prediction.prediction_id
    assert prediction.quality.model_artifact_verified
    assert prediction.quality.feature_artifact_verified
    assert not prediction.production_eligible
    assert manifest_doc.get("input_snapshots") is not None
    assert prediction.lineage.input_snapshots or manifest_doc.get("input_snapshots") == []
