"""Focused regressions for PR #8 third correction integrity requirements."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from tests.unit.artifacts.test_analytical_artifacts import _typed_datasets
from tests.unit.predictions.test_prediction_value_layer import _prediction, _quote
from tests.unit.predictions.test_second_correction_regressions import (
    START,
    _dependency_metadata_for_opportunity,
    _prediction_from_opportunity,
    _quote_from_opportunity,
    _runtime,
    _trained_fixture,
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
    derive_analysis_run_id,
    quote_fingerprint_from_quote,
    serialize_opportunity_row,
)
from sports_analytics.artifacts import (
    load_typed_analytical_artifact,
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
from sports_analytics.combinations.builder import build_combinations
from sports_analytics.combinations.contracts import CombinationRules
from sports_analytics.combinations.evidence import (
    SYNTHETIC_COMBINATION_EVIDENCE_LABEL,
    CombinationEvidenceMode,
)
from sports_analytics.core.exceptions import (
    ArtifactError,
    ConfigurationError,
    PredictionError,
)
from sports_analytics.opportunities.contracts import OpportunityFilter
from sports_analytics.predictions.contracts import (
    derive_prediction_id,
)
from sports_analytics.predictions.provenance import PredictionProvenance
from sports_analytics.predictions.replay import derive_historical_replay_cutoff_utc
from sports_analytics.predictions.service import (
    VerifiedPredictionRequest,
    generate_verified_football_1x2_prediction,
)
from sports_analytics.services.analysis import (
    ANALYSIS_ARTIFACT_SCHEMA,
    AnalysisMarketInput,
    AnalysisPublicationRequest,
    publish_analysis_artifact,
)
from sports_analytics.services.analysis_json import (
    build_combinations_from_json,
    validate_combination_from_json,
)
from sports_analytics.services.combinations_trusted import (
    build_combinations_from_analysis_artifact,
)
from sports_analytics.value.contracts import QuoteEvaluationMode


def test_historical_replay_feature_availability_never_follows_prediction_time(
    tmp_path: Path,
) -> None:
    paths, artifact, trained, vector = _trained_fixture(tmp_path)
    event_start = vector.metadata.scheduled_start_utc
    assert event_start is not None
    cutoff = derive_historical_replay_cutoff_utc(event_start)
    prediction = generate_verified_football_1x2_prediction(
        paths=paths,
        request=VerifiedPredictionRequest(
            model_relative_path=trained.final_artifact_relative_directory,
            model_checksum_sha256=trained.final_artifact_checksum,
            feature_relative_directory=artifact.relative_directory,
            feature_manifest_checksum_sha256=artifact.manifest_checksum_sha256,
            canonical_event_id=vector.metadata.canonical_event_id,
            event_start_utc=event_start,
            predicted_at_utc=cutoff,
            provenance=PredictionProvenance.HISTORICAL_REPLAY,
        ),
    )
    assert prediction.feature_available_at_utc <= prediction.predicted_at_utc
    assert prediction.predicted_at_utc < prediction.event_start_utc
    assert prediction.quality.data_quality_passed is False


def test_arbitrary_replay_prediction_time_rejected(tmp_path: Path) -> None:
    paths, artifact, trained, vector = _trained_fixture(tmp_path)
    event_start = vector.metadata.scheduled_start_utc
    assert event_start is not None
    with pytest.raises(PredictionError, match="predicted_at_utc must equal"):
        generate_verified_football_1x2_prediction(
            paths=paths,
            request=VerifiedPredictionRequest(
                model_relative_path=trained.final_artifact_relative_directory,
                model_checksum_sha256=trained.final_artifact_checksum,
                feature_relative_directory=artifact.relative_directory,
                feature_manifest_checksum_sha256=artifact.manifest_checksum_sha256,
                canonical_event_id=vector.metadata.canonical_event_id,
                event_start_utc=event_start,
                predicted_at_utc=event_start - timedelta(hours=1),
                provenance=PredictionProvenance.HISTORICAL_REPLAY,
            ),
        )


def test_historical_replay_cutoff_is_deterministic() -> None:
    event_start = datetime(2024, 2, 10, 15, 0, tzinfo=UTC)
    first = derive_historical_replay_cutoff_utc(event_start)
    second = derive_historical_replay_cutoff_utc(event_start)
    assert first == second
    assert first == event_start - timedelta(microseconds=1)


def test_cross_sport_multi_date_combination_persisted(tmp_path: Path) -> None:
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
    loaded = load_typed_analytical_artifact(
        root=paths.exports_directory,
        relative_directory=published.relative_directory,
        expected_kind="analysis",
        expected_schema_version=ANALYSIS_ARTIFACT_SCHEMA,
        expected_checksum=published.checksum_sha256,
    )
    assert len(loaded.dataset("combinations").rows) >= 1


def test_missing_dependency_metadata_remains_unknown(tmp_path: Path) -> None:
    paths = _runtime(tmp_path)
    first = build_test_opportunity("1", event_id="event-1", start=START)
    second = build_test_opportunity(
        "2",
        event_id="event-2",
        start=START + timedelta(days=1),
        dependency_keys=frozenset(),
        participant_ids=frozenset(),
        dependency_metadata_complete=False,
    )
    published = publish_analysis_artifact(
        paths=paths,
        request=AnalysisPublicationRequest(
            markets=(
                AnalysisMarketInput(
                    prediction=_prediction_from_opportunity(first),
                    quote=_quote_from_opportunity(first),
                ),
                AnalysisMarketInput(
                    prediction=_prediction_from_opportunity(second),
                    quote=_quote_from_opportunity(second),
                ),
            ),
            mode=QuoteEvaluationMode.LIVE_SAFE,
            filters=OpportunityFilter(minimum_edge=-1, minimum_expected_value=-1),
            combination_rules=CombinationRules(minimum_legs=2, maximum_legs=2),
            provenance=PredictionProvenance.SYNTHETIC_CONTRACT,
        ),
    )
    loaded = load_typed_analytical_artifact(
        root=paths.exports_directory,
        relative_directory=published.relative_directory,
        expected_kind="analysis",
        expected_schema_version=ANALYSIS_ARTIFACT_SCHEMA,
        expected_checksum=published.checksum_sha256,
    )
    rejections = loaded.dataset("rejections").rows
    assert any(row.get("rejection_code") == "unknown-dependency" for row in rejections)


def test_raw_combination_json_is_synthetic_only() -> None:
    first = build_test_opportunity("1", event_id="event-1", start=START)
    second = build_test_opportunity(
        "2",
        event_id="event-2",
        start=START + timedelta(days=1),
    )
    payload = {
        "provenance": "synthetic-contract",
        "opportunities": [
            {"opportunity": serialize_opportunity_row(first)},
            {"opportunity": serialize_opportunity_row(second)},
        ],
        "rules": {},
    }
    result = build_combinations_from_json(payload)
    assert result["evidence_label"] == SYNTHETIC_COMBINATION_EVIDENCE_LABEL
    with pytest.raises(ConfigurationError, match="synthetic-contract"):
        build_combinations_from_json({**payload, "provenance": "historical-replay"})
    with pytest.raises(
        (ConfigurationError, PredictionError),
        match="synthetic-contract|provenance",
    ):
        validate_combination_from_json({**payload, "provenance": "live"})


def test_trusted_combination_path_rejects_failed_prediction_quality() -> None:
    accepted = build_test_opportunity("1", event_id="event-1", start=START)
    failed = build_test_opportunity(
        "2",
        event_id="event-2",
        start=START + timedelta(days=1),
        model_probability=0.01,
        odds="50.0",
    )
    object.__setattr__(failed, "prediction_quality_passed", False)
    result = build_combinations(
        (accepted, failed),
        rules=CombinationRules(minimum_legs=2, maximum_legs=2),
        evidence_mode=CombinationEvidenceMode.TRUSTED_VERIFIED,
    )
    assert not result.combinations
    assert any("failed prediction quality" in item.reason for item in result.rejections)


def test_provenance_relabelling_invalidates_prediction_id() -> None:
    prediction = _prediction()
    synthetic_id = derive_prediction_id(
        canonical_event_id=prediction.canonical_event_id,
        event_start_utc=prediction.event_start_utc,
        predicted_at_utc=prediction.predicted_at_utc,
        feature_available_at_utc=prediction.feature_available_at_utc,
        lineage=prediction.lineage,
        probabilities=prediction.probabilities,
        quality=prediction.quality,
        provenance=PredictionProvenance.SYNTHETIC_CONTRACT,
    )
    replay_id = derive_prediction_id(
        canonical_event_id=prediction.canonical_event_id,
        event_start_utc=prediction.event_start_utc,
        predicted_at_utc=prediction.predicted_at_utc,
        feature_available_at_utc=prediction.feature_available_at_utc,
        lineage=prediction.lineage,
        probabilities=prediction.probabilities,
        quality=prediction.quality,
        provenance=PredictionProvenance.HISTORICAL_REPLAY,
    )
    assert synthetic_id != replay_id


def test_reversing_market_input_order_preserves_analysis_identity(tmp_path: Path) -> None:
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
    request_a = AnalysisPublicationRequest(
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
        combination_rules=CombinationRules(minimum_legs=2, maximum_legs=2),
        provenance=PredictionProvenance.SYNTHETIC_CONTRACT,
    )
    request_b = replace(request_a, markets=tuple(reversed(request_a.markets)))
    request_b = replace(request_a, markets=tuple(reversed(request_a.markets)))
    run_a = derive_analysis_run_id(
        markets=tuple(
            (market.prediction, quote_fingerprint_from_quote(market.quote))
            for market in request_a.markets
        ),
        mode=request_a.mode.value,
        filters=request_a.filters,
        combination_rules_id=request_a.combination_rules.policy_id
        if request_a.combination_rules is not None
        else None,
        provenance=request_a.provenance.value,
    )
    run_b = derive_analysis_run_id(
        markets=tuple(
            (market.prediction, quote_fingerprint_from_quote(market.quote))
            for market in request_b.markets
        ),
        mode=request_b.mode.value,
        filters=request_b.filters,
        combination_rules_id=request_b.combination_rules.policy_id
        if request_b.combination_rules is not None
        else None,
        provenance=request_b.provenance.value,
    )
    assert run_a == run_b
    published = publish_analysis_artifact(paths=paths, request=request_a)
    published_reversed = publish_analysis_artifact(
        paths=paths,
        request=replace(request_a, relative_directory=f"{published.relative_directory}-reversed"),
    )
    assert published.analysis_run_id == published_reversed.analysis_run_id
    assert published.artifact_id == published_reversed.artifact_id


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("quote_phase", "closing"),
        ("source_name", "alternate-feed"),
        ("provider_type", "exchange"),
        ("quote_series_id", "series-alt"),
    ],
)
def test_material_quote_fields_change_analysis_identity(field_name: str, value: str) -> None:
    prediction = _prediction()
    quote_a = _quote(prediction)
    if field_name == "quote_series_id":
        quote_b = replace(
            quote_a,
            selections=tuple(replace(item, quote_series_id=value) for item in quote_a.selections),
        )
    elif field_name == "quote_phase":
        quote_b = replace(quote_a, quote_phase=value)
    elif field_name == "source_name":
        quote_b = replace(quote_a, source_name=value)
    else:
        quote_b = replace(quote_a, provider_type=value)
    filters = OpportunityFilter()
    run_a = derive_analysis_run_id(
        markets=((prediction, quote_fingerprint_from_quote(quote_a)),),
        mode=QuoteEvaluationMode.LIVE_SAFE.value,
        filters=filters,
        combination_rules_id=None,
        provenance=PredictionProvenance.SYNTHETIC_CONTRACT.value,
    )
    run_b = derive_analysis_run_id(
        markets=((prediction, quote_fingerprint_from_quote(quote_b)),),
        mode=QuoteEvaluationMode.LIVE_SAFE.value,
        filters=filters,
        combination_rules_id=None,
        provenance=PredictionProvenance.SYNTHETIC_CONTRACT.value,
    )
    assert run_a != run_b


def test_string_false_rejected_for_boolean_fields() -> None:
    datasets = _typed_datasets()
    row = dict(datasets["opportunity_decisions"][0])
    row["eligible"] = "false"
    with pytest.raises(ArtifactError, match="must be a boolean"):
        validate_dataset_row_schema(
            "opportunity_decisions",
            row,
            version="opportunity-decisions-v2",
        )


def test_numeric_ids_rejected_where_strings_required() -> None:
    datasets = _typed_datasets()
    row = dict(datasets["predictions"][0])
    row["canonical_event_id"] = 123
    with pytest.raises(ArtifactError):
        validate_dataset_row_schema("predictions", row, version="predictions-v2")


def test_malformed_decimal_strings_produce_typed_artifact_error() -> None:
    row = {
        "bet_id": "bet-1",
        "schema_version": "settlements-v2",
        "fold_id": "fold-1",
        "kind": "single",
        "opportunity_ids": ["opportunity-1"],
        "decimal_odds": "not-a-decimal",
        "result": "win",
        "stake_units": "1",
        "returned_units": "2",
        "profit_units": "1",
    }
    with pytest.raises(ArtifactError, match="decimal"):
        validate_dataset_row_schema("settlements", row, version="settlements-v2")


def test_forged_opportunity_edge_rejected_even_with_recomputed_id() -> None:
    opportunity = build_test_opportunity("1", event_id="event-1", start=START)
    row = serialize_opportunity_row(opportunity)
    row["edge"] = float(row["edge"]) + 0.25
    row["opportunity_id"] = "forged-opportunity-id"
    with pytest.raises(ArtifactError, match="does not match canonical identity|inconsistent"):
        validate_dataset_row_schema("opportunities", row, version="opportunities-v2")


def test_forged_market_evaluation_formula_rejected() -> None:
    datasets = _typed_datasets()
    row = dict(datasets["market_evaluations"][0])
    row["expected_value"] = float(row["expected_value"]) + 0.5
    with pytest.raises(ArtifactError, match="inconsistent|does not match"):
        validate_dataset_row_schema("market_evaluations", row, version="market-evaluations-v2")


def test_forged_combination_id_rejected() -> None:
    from tests.unit.predictions.test_surgical_final_integrity import _combination_fixture_rows

    row = _combination_fixture_rows()
    row = dict(row)
    row["combination_id"] = "forged-combination-id"
    with pytest.raises(ArtifactError, match="does not match"):
        validate_dataset_row_schema("combinations", row, version="combinations-v2")


def test_dependency_pairs_outside_leg_set_rejected() -> None:
    from tests.unit.predictions.test_surgical_final_integrity import _combination_fixture_rows

    row = dict(_combination_fixture_rows())
    dependencies = list(row["dependencies"])
    dependencies[0] = {
        **dependencies[0],
        "left_opportunity_id": "orphan-left",
        "right_opportunity_id": "orphan-right",
    }
    row["dependencies"] = dependencies
    with pytest.raises(ArtifactError, match="undeclared leg"):
        validate_dataset_row_schema("combinations", row, version="combinations-v2")


def test_forged_settlement_id_rejected() -> None:
    accepted = build_test_opportunity("1", event_id="event-1", start=START)
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
        opportunity_filter=OpportunityFilter(minimum_edge=-1),
    )
    result = run_backtest(
        (
            FoldBacktestInput(
                fold=fold,
                candidates=(SettledOpportunity(accepted, SettlementResult.WIN),),
            ),
        ),
        strategy=strategy,
    )
    from sports_analytics.artifact_serializers import serialize_settlement_row

    row = serialize_settlement_row(result.bets[0], strategy_id=result.strategy_id)
    row["bet_id"] = "forged-bet-id"
    with pytest.raises(ArtifactError, match="bet_id does not match"):
        validate_dataset_row_schema("settlements", row, version="settlements-v2")


def test_settlement_return_profit_inconsistency_rejected() -> None:
    row = {
        "bet_id": "bet-1",
        "schema_version": "settlements-v2",
        "fold_id": "fold-1",
        "kind": "single",
        "opportunity_ids": ["opportunity-1"],
        "decimal_odds": "2.0",
        "result": "win",
        "stake_units": "1",
        "returned_units": "2.0",
        "profit_units": "0.5",
    }
    with pytest.raises(ArtifactError, match="inconsistent"):
        validate_dataset_row_schema("settlements", row, version="settlements-v2")


def test_missing_opportunity_decision_rejected() -> None:
    datasets = _typed_datasets()
    dataset_map = {name: tuple(rows) for name, rows in datasets.items()}
    dataset_map["opportunity_decisions"] = ()
    with pytest.raises(
        ArtifactError, match="must match opportunity count exactly|must cover every opportunity"
    ):
        validate_cross_dataset_integrity(dataset_map)


def test_eligible_decision_without_rank_rejected() -> None:
    datasets = _typed_datasets()
    row = dict(datasets["opportunity_decisions"][0])
    row["eligible"] = True
    row["accepted_rank"] = None
    with pytest.raises(ArtifactError, match="requires accepted_rank"):
        validate_dataset_row_schema(
            "opportunity_decisions",
            row,
            version="opportunity-decisions-v2",
        )


def test_rejected_decision_with_rank_rejected() -> None:
    datasets = _typed_datasets()
    dataset_map = {name: tuple(rows) for name, rows in datasets.items()}
    decisions = [dict(row) for row in dataset_map["opportunity_decisions"]]
    decisions[0]["eligible"] = False
    decisions[0]["rejection_codes"] = ["edge"]
    decisions[0]["accepted_rank"] = 1
    dataset_map["opportunity_decisions"] = tuple(decisions)
    with pytest.raises(ArtifactError, match="cannot include accepted_rank"):
        validate_cross_dataset_integrity(dataset_map)


def test_logistic_configuration_changes_fold_and_result_identity() -> None:
    accepted = build_test_opportunity("1", event_id="event-1", start=START)
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
        opportunity_filter=OpportunityFilter(minimum_edge=-1),
    )
    base_input = FoldBacktestInput(
        fold=fold,
        candidates=(SettledOpportunity(accepted, SettlementResult.WIN),),
        fold_model_payload={"feature_names": ["a"], "outcome_labels": ["x", "y"]},
    )
    alternate_input = replace(
        base_input,
        fold_model_payload={"feature_names": ["a", "b"], "outcome_labels": ["x", "y"]},
    )
    first = run_backtest((base_input,), strategy=strategy)
    second = run_backtest((alternate_input,), strategy=strategy)
    assert first.backtest_result_id != second.backtest_result_id


def test_end_to_end_analysis_and_backtest_publication_reload(tmp_path: Path) -> None:
    paths = _runtime(tmp_path)
    opportunity = build_test_opportunity("1", event_id="event-1", start=START)
    published = publish_analysis_artifact(
        paths=paths,
        request=AnalysisPublicationRequest(
            markets=(
                AnalysisMarketInput(
                    prediction=_prediction_from_opportunity(opportunity),
                    quote=_quote_from_opportunity(opportunity),
                ),
            ),
            mode=QuoteEvaluationMode.LIVE_SAFE,
            filters=OpportunityFilter(minimum_edge=-1, minimum_expected_value=-1),
            provenance=PredictionProvenance.SYNTHETIC_CONTRACT,
        ),
    )
    loaded = load_typed_analytical_artifact(
        root=paths.exports_directory,
        relative_directory=published.relative_directory,
        expected_kind="analysis",
        expected_schema_version=ANALYSIS_ARTIFACT_SCHEMA,
        expected_checksum=published.checksum_sha256,
    )
    assert loaded.dataset("predictions").row_count >= 1


def test_repeated_logical_runs_remain_deterministic(tmp_path: Path) -> None:
    paths = _runtime(tmp_path)
    opportunity = build_test_opportunity("1", event_id="event-1", start=START)
    request = AnalysisPublicationRequest(
        markets=(
            AnalysisMarketInput(
                prediction=_prediction_from_opportunity(opportunity),
                quote=_quote_from_opportunity(opportunity),
            ),
        ),
        mode=QuoteEvaluationMode.LIVE_SAFE,
        filters=OpportunityFilter(minimum_edge=-1, minimum_expected_value=-1),
        provenance=PredictionProvenance.SYNTHETIC_CONTRACT,
    )
    first = publish_analysis_artifact(paths=paths, request=request)
    second = publish_analysis_artifact(
        paths=paths,
        request=replace(request, relative_directory=f"{first.relative_directory}-repeat"),
    )
    assert first.analysis_run_id == second.analysis_run_id
    assert first.artifact_id == second.artifact_id
    assert first.checksum_sha256 == second.checksum_sha256


def test_trusted_combination_loading_from_analysis_artifact(tmp_path: Path) -> None:
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
    result = build_combinations_from_analysis_artifact(
        paths=paths,
        relative_directory=published.relative_directory,
        expected_checksum=published.checksum_sha256,
        rules=CombinationRules(
            minimum_legs=2,
            maximum_legs=2,
            allow_multiple_sports=True,
            allow_multiple_dates=True,
        ),
    )
    assert len(result.combinations) >= 1


def test_historical_replay_remains_non_production(tmp_path: Path) -> None:
    paths, artifact, trained, vector = _trained_fixture(tmp_path)
    event_start = vector.metadata.scheduled_start_utc
    assert event_start is not None
    prediction = generate_verified_football_1x2_prediction(
        paths=paths,
        request=VerifiedPredictionRequest(
            model_relative_path=trained.final_artifact_relative_directory,
            model_checksum_sha256=trained.final_artifact_checksum,
            feature_relative_directory=artifact.relative_directory,
            feature_manifest_checksum_sha256=artifact.manifest_checksum_sha256,
            canonical_event_id=vector.metadata.canonical_event_id,
            event_start_utc=event_start,
            predicted_at_utc=derive_historical_replay_cutoff_utc(event_start),
            provenance=PredictionProvenance.HISTORICAL_REPLAY,
        ),
    )
    assert prediction.provenance is PredictionProvenance.HISTORICAL_REPLAY
    assert prediction.quality.data_quality_passed is False
    assert prediction.quality.production_eligible is False
