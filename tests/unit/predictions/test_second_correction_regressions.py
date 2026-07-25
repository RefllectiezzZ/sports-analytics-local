"""Focused regressions for PR #8 second correction trust-boundary requirements."""

from __future__ import annotations

import json
from dataclasses import fields, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from tests.helpers_snapshots import database_path, prepare, publication_service
from tests.helpers_training import synthetic_season_csv
from tests.unit.predictions.test_prediction_value_layer import _prediction, _quote
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
)
from sports_analytics.artifacts import load_typed_analytical_artifact
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
from sports_analytics.core.exceptions import OpportunityError, PredictionError, ValueEvaluationError
from sports_analytics.core.paths import resolve_paths
from sports_analytics.core.settings import load_settings
from sports_analytics.evaluation.temporal import TemporalSplitConfig
from sports_analytics.features.football.datasets import load_feature_artifact
from sports_analytics.opportunities.contracts import Opportunity, OpportunityFilter
from sports_analytics.opportunities.dependency import (
    DependencyMetadataProvenance,
    MarketDependencyMetadata,
    SelectionDependencyMetadata,
)
from sports_analytics.opportunities.identity import (
    derive_opportunity_id,
    opportunity_identity_payload,
    verify_selection_value_calculations,
)
from sports_analytics.predictions.contracts import (
    PredictionLineage,
    PredictionQualityFlags,
    SelectionProbability,
    build_market_prediction,
)
from sports_analytics.predictions.provenance import (
    PredictionProvenance,
    parse_prediction_provenance,
)
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
from sports_analytics.services.engine_cli import main as engine_main
from sports_analytics.services.training import (
    FeatureBuildRequest,
    TrainRequest,
    build_football_1x2_features,
    train_football_1x2_model,
)
from sports_analytics.value.contracts import (
    CompleteMarketQuote,
    PricedSelection,
    QuoteEvaluationMode,
    evaluate_complete_market,
)

START = datetime(2024, 3, 10, 15, tzinfo=UTC)


def _runtime(tmp_path: Path):
    settings = load_settings(
        config_path=None,
        env_file=None,
        environ={
            "SPORTS_ANALYTICS_STORAGE__ROOT_DIRECTORY": str(tmp_path / "storage"),
            "SPORTS_ANALYTICS_STORAGE__SNAPSHOTS_DIRECTORY": str(tmp_path / "snapshots"),
            "SPORTS_ANALYTICS_STORAGE__FEATURES_DIRECTORY": str(tmp_path / "features"),
            "SPORTS_ANALYTICS_STORAGE__MODELS_DIRECTORY": str(tmp_path / "models"),
            "SPORTS_ANALYTICS_STORAGE__EXPORTS_DIRECTORY": str(tmp_path / "exports"),
            "SPORTS_ANALYTICS_STORAGE__SQLITE_PATH": str(tmp_path / "operational.sqlite3"),
        },
    )
    paths = resolve_paths(settings, tmp_path)
    for directory in (
        paths.features_directory,
        paths.models_directory,
        paths.exports_directory,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    return paths


def _trained_fixture(tmp_path: Path):
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
    paths = _runtime(tmp_path)
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
    _manifest, vectors, _, _ = load_feature_artifact(
        features_root=paths.features_directory,
        relative_directory=artifact.relative_directory,
        expected_manifest_checksum=artifact.manifest_checksum_sha256,
    )
    vectors_with_start = [item for item in vectors if item.metadata.scheduled_start_utc is not None]
    if not vectors_with_start:
        pytest.skip("synthetic fixture lacks scheduled_start_utc required for historical replay")
    vector = max(vectors_with_start, key=lambda item: item.metadata.event_date)
    return paths, artifact, trained, vector


def test_unsupported_provenance_values_are_rejected() -> None:
    with pytest.raises(PredictionError, match="must be one of"):
        parse_prediction_provenance("live")
    with pytest.raises(PredictionError, match="must be one of"):
        parse_prediction_provenance("production")


def test_historical_feature_row_cannot_be_relabelled_with_future_start(tmp_path: Path) -> None:
    paths, artifact, trained, vector = _trained_fixture(tmp_path)
    event_start = vector.metadata.scheduled_start_utc
    assert event_start is not None
    with pytest.raises(PredictionError, match="scheduled_start_utc"):
        generate_verified_football_1x2_prediction(
            paths=paths,
            request=VerifiedPredictionRequest(
                model_relative_path=trained.final_artifact_relative_directory,
                model_checksum_sha256=trained.final_artifact_checksum,
                feature_relative_directory=artifact.relative_directory,
                feature_manifest_checksum_sha256=artifact.manifest_checksum_sha256,
                canonical_event_id=vector.metadata.canonical_event_id,
                event_start_utc=event_start + timedelta(days=1),
                predicted_at_utc=derive_historical_replay_cutoff_utc(event_start),
                provenance=PredictionProvenance.HISTORICAL_REPLAY,
            ),
        )


def test_cli_requires_independent_model_and_feature_checksums(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    request_path = tmp_path / "request.json"
    request_path.write_text(
        json.dumps(
            {
                "canonical_event_id": "event-1",
                "event_start_utc": "2024-02-10T15:00:00Z",
                "predicted_at_utc": "2024-02-10T12:00:00Z",
                "provenance": "historical-replay",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(SystemExit) as exc:
        engine_main(
            [
                "--generate-verified-predictions",
                str(request_path),
                "--model",
                "models/example",
                "--features",
                "features/example",
            ]
        )
    assert exc.value.code == 2
    assert "model-checksum" in capsys.readouterr().err


def test_all_empty_opportunity_lineage_rejected() -> None:
    opportunity = build_test_opportunity("1", event_id="event-1", start=START)
    payload = opportunity_identity_payload(opportunity)
    payload = {
        **payload,
        "model_artifact_id": "",
        "model_checksum_sha256": "",
        "feature_artifact_id": "",
        "feature_manifest_checksum_sha256": "",
    }
    forged_id = derive_opportunity_id(payload=payload)
    with pytest.raises(OpportunityError, match="lineage fields must be complete"):
        Opportunity(
            opportunity_id=forged_id,
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
            model_artifact_id="",
            model_checksum_sha256="",
            model_specification_version=opportunity.model_specification_version,
            feature_artifact_id="",
            feature_manifest_checksum_sha256="",
            feature_specification_version=opportunity.feature_specification_version,
            feature_row_id=opportunity.feature_row_id,
            dependency_keys=opportunity.dependency_keys,
            participant_ids=opportunity.participant_ids,
            dependency_metadata_complete=opportunity.dependency_metadata_complete,
            prediction_quality_passed=opportunity.prediction_quality_passed,
        )


def test_prediction_quality_defaults_false_on_opportunity_dataclass() -> None:
    default = next(
        field.default for field in fields(Opportunity) if field.name == "prediction_quality_passed"
    )
    assert default is False


def test_negative_overround_rejected() -> None:
    prediction = _prediction()
    quote = _quote(prediction)
    selections = list(quote.selections)
    selections[0] = replace(selections[0], decimal_odds=Decimal("3.0"))
    selections[1] = replace(selections[1], decimal_odds=Decimal("3.0"))
    invalid_quote = replace(quote, selections=tuple(selections))
    with pytest.raises(ValueEvaluationError, match="overround"):
        evaluate_complete_market(
            prediction=prediction,
            quote=invalid_quote,
            mode=QuoteEvaluationMode.LIVE_SAFE,
        )


def test_zero_normalized_probability_raises_typed_error() -> None:
    with pytest.raises(OpportunityError, match="normalized implied probability"):
        verify_selection_value_calculations(
            model_probability=0.5,
            decimal_odds=Decimal("2.0"),
            raw_implied_probability=0.5,
            normalized_implied_probability=0.0,
            overround=0.0,
            edge=0.5,
            expected_value=0.0,
        )


@pytest.mark.parametrize(
    "field_name",
    (
        "edge",
        "expected_value",
        "model_probability",
        "overround",
        "provider_id",
        "prediction_id",
    ),
)
def test_every_material_opportunity_field_affects_id(field_name: str) -> None:
    base = build_test_opportunity("1", event_id="event-1", start=START)
    payload = opportunity_identity_payload(base)
    if field_name == "provider_id":
        payload[field_name] = "provider-b"
    elif field_name == "prediction_id":
        payload[field_name] = "prediction-other"
    elif field_name == "model_probability":
        payload[field_name] = float(payload[field_name]) + 0.01
    else:
        payload[field_name] = float(payload[field_name]) + 0.01
    assert derive_opportunity_id(payload=payload) != base.opportunity_id


def test_decision_ranks_are_unique_and_contiguous(tmp_path: Path) -> None:
    datasets = _publish_analysis_datasets(tmp_path)
    decisions = datasets["opportunity_decisions"]
    accepted = [row for row in decisions if row["eligible"]]
    ranks = [int(row["accepted_rank"]) for row in accepted if row.get("accepted_rank") is not None]
    assert sorted(ranks) == list(range(1, len(ranks) + 1))
    assert len(ranks) == len(set(ranks))


def test_exactly_one_decision_per_opportunity(tmp_path: Path) -> None:
    datasets = _publish_analysis_datasets(tmp_path)
    opportunity_ids = {row["opportunity_id"] for row in datasets["opportunities"]}
    decision_ids = [row["opportunity_id"] for row in datasets["opportunity_decisions"]]
    assert len(decision_ids) == len(set(decision_ids))
    assert set(decision_ids) == opportunity_ids


def test_combination_rejection_rows_publish_and_reload(tmp_path: Path) -> None:
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
    assert rejections
    for row in rejections:
        validate_dataset_row_schema(
            "rejections",
            row,
            version=loaded.dataset("rejections").schema_version,
        )
        assert row["rejection_kind"] in {"opportunity-filter", "combination-builder"}
    dataset_map = {dataset.name: dataset.rows for dataset in loaded.datasets}
    validate_cross_dataset_integrity(dataset_map)


def _dependency_metadata_for_opportunity(
    opportunity: Opportunity,
    *,
    event_key: str,
    participant: str,
) -> MarketDependencyMetadata:
    selection_id = opportunity.selection.selection_id
    return MarketDependencyMetadata(
        by_selection_id={
            selection_id: SelectionDependencyMetadata(
                selection_id=selection_id,
                dependency_keys=frozenset(
                    {
                        f"event:{event_key}",
                        f"sport:{opportunity.selection.sport_code}",
                    }
                ),
                participant_ids=frozenset({participant}),
                dependency_metadata_complete=True,
                metadata_provenance=DependencyMetadataProvenance.SYNTHETIC_CONTRACT,
            )
        }
    )


def test_multi_event_analysis_publication(tmp_path: Path) -> None:
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
    event_ids = {row["canonical_event_id"] for row in loaded.dataset("predictions").rows}
    assert event_ids == {"event-1", "event-2"}
    combinations = loaded.dataset("combinations").rows
    assert len(combinations) >= 1
    opportunity_rows = {row["opportunity_id"]: row for row in loaded.dataset("opportunities").rows}
    for combination in combinations:
        leg_sports = {
            opportunity_rows[opportunity_id]["selection"]["sport_code"]
            for opportunity_id in combination["opportunity_ids"]
        }
        assert len(leg_sports) >= 2


def test_different_filters_produce_different_analysis_paths(tmp_path: Path) -> None:
    paths = _runtime(tmp_path)
    opportunity = build_test_opportunity("1", event_id="event-1", start=START)
    market = AnalysisMarketInput(
        prediction=_prediction_from_opportunity(opportunity),
        quote=_quote_from_opportunity(opportunity),
    )
    loose = publish_analysis_artifact(
        paths=paths,
        request=AnalysisPublicationRequest(
            markets=(market,),
            mode=QuoteEvaluationMode.LIVE_SAFE,
            filters=OpportunityFilter(minimum_edge=-1, minimum_expected_value=-1),
            provenance=PredictionProvenance.SYNTHETIC_CONTRACT,
        ),
    )
    strict = publish_analysis_artifact(
        paths=paths,
        request=AnalysisPublicationRequest(
            markets=(market,),
            mode=QuoteEvaluationMode.LIVE_SAFE,
            filters=OpportunityFilter(minimum_edge=0.99),
            provenance=PredictionProvenance.SYNTHETIC_CONTRACT,
        ),
    )
    assert loose.analysis_run_id != strict.analysis_run_id
    assert loose.relative_directory != strict.relative_directory


def test_different_quotes_produce_different_analysis_run_ids() -> None:
    prediction = _prediction()
    quote_a = _quote(prediction)
    quote_b = replace(
        quote_a,
        provider_id="provider-b",
        selections=tuple(
            replace(item, quote_observation_id=f"{item.quote_observation_id}-b")
            for item in quote_a.selections
        ),
    )
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


def test_complete_metrics_change_backtest_result_id() -> None:
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
        opportunity_filter=OpportunityFilter(minimum_edge=-1, minimum_expected_value=-1),
    )
    win = run_backtest(
        (
            FoldBacktestInput(
                fold=fold,
                candidates=(SettledOpportunity(accepted, SettlementResult.WIN),),
            ),
        ),
        strategy=strategy,
    )
    loss = run_backtest(
        (
            FoldBacktestInput(
                fold=fold,
                candidates=(SettledOpportunity(accepted, SettlementResult.LOSS),),
            ),
        ),
        strategy=strategy,
    )
    assert win.backtest_result_id != loss.backtest_result_id
    assert win.metrics.maximum_drawdown_units != loss.metrics.maximum_drawdown_units


def test_dataset_ids_recompute_on_reload(tmp_path: Path) -> None:
    datasets = _publish_analysis_datasets(tmp_path)
    for row in datasets["predictions"]:
        validate_dataset_row_schema("predictions", row, version="predictions-v2")
    for row in datasets["market_evaluations"]:
        validate_dataset_row_schema("market_evaluations", row, version="market-evaluations-v2")
    for row in datasets["opportunities"]:
        validate_dataset_row_schema("opportunities", row, version="opportunities-v2")


def _publish_analysis_datasets(tmp_path: Path) -> dict[str, tuple[dict[str, object], ...]]:
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
    return {dataset.name: tuple(dataset.rows) for dataset in loaded.datasets}


def _prediction_from_opportunity(opportunity: Opportunity):
    selection = opportunity.selection
    opponent = basketball_selection(
        outcome="b" if selection.outcome_key != "b" else "c",
        sport_code=selection.sport_code,
        market_key=selection.market_key,
    )
    lineage = PredictionLineage(
        model_artifact_id=opportunity.model_artifact_id,
        model_checksum_sha256=opportunity.model_checksum_sha256,
        model_specification_version=opportunity.model_specification_version,
        feature_artifact_id=opportunity.feature_artifact_id,
        feature_manifest_checksum_sha256=opportunity.feature_manifest_checksum_sha256,
        feature_specification_version=opportunity.feature_specification_version,
        feature_row_id=opportunity.feature_row_id,
        trained_through_date=opportunity.model_trained_through_date,
        calibrated_through_date=opportunity.model_calibrated_through_date,
    )
    return build_market_prediction(
        canonical_event_id=opportunity.canonical_event_id,
        event_start_utc=opportunity.event_start_utc,
        predicted_at_utc=opportunity.predicted_at_utc,
        feature_available_at_utc=opportunity.predicted_at_utc - timedelta(hours=1),
        lineage=lineage,
        probabilities=(
            SelectionProbability(selection, opportunity.model_probability),
            SelectionProbability(opponent, 1.0 - opportunity.model_probability),
        ),
        quality=PredictionQualityFlags(
            calibrated=True,
            model_artifact_verified=True,
            feature_artifact_verified=True,
            sufficient_history=True,
            data_quality_passed=True,
        ),
    )


def _quote_from_opportunity(opportunity: Opportunity) -> CompleteMarketQuote:
    selection = opportunity.selection
    opponent = basketball_selection(
        outcome="b" if selection.outcome_key != "b" else "c",
        sport_code=selection.sport_code,
        market_key=selection.market_key,
    )
    return CompleteMarketQuote(
        canonical_event_id=opportunity.canonical_event_id,
        source_name=opportunity.source_name,
        provider_type=opportunity.provider_type,
        provider_id=opportunity.provider_id,
        quote_phase="current",
        source_observed_at_utc=opportunity.source_observed_at_utc,
        quoted_at_utc=opportunity.quoted_at_utc,
        quote_timestamp_precision="exact",
        quote_valid_from_utc=None,
        quote_valid_to_utc=None,
        selections=(
            PricedSelection(
                selection=selection,
                decimal_odds=opportunity.decimal_odds,
                quote_series_id=opportunity.quote_series_id,
                quote_observation_id=opportunity.quote_observation_id,
            ),
            PricedSelection(
                selection=opponent,
                decimal_odds=Decimal("2.0"),
                quote_series_id=f"{opportunity.quote_series_id}-b",
                quote_observation_id=f"{opportunity.quote_observation_id}-b",
            ),
        ),
    )


def test_cli_historical_replay_publication_workflow(tmp_path: Path) -> None:
    paths, artifact, trained, vector = _trained_fixture(tmp_path)
    event_start = vector.metadata.scheduled_start_utc
    assert event_start is not None
    predicted_at = derive_historical_replay_cutoff_utc(event_start)
    request_path = tmp_path / "verified-request.json"
    request_path.write_text(
        json.dumps(
            {
                "canonical_event_id": vector.metadata.canonical_event_id,
                "event_start_utc": event_start.isoformat().replace("+00:00", "Z"),
                "predicted_at_utc": predicted_at.isoformat().replace("+00:00", "Z"),
                "provenance": "historical-replay",
            }
        ),
        encoding="utf-8",
    )
    env_path = tmp_path / "engine.env"
    env_path.write_text(
        "\n".join(
            (
                f"SPORTS_ANALYTICS_STORAGE__ROOT_DIRECTORY={paths.storage_root}",
                f"SPORTS_ANALYTICS_STORAGE__SNAPSHOTS_DIRECTORY={paths.snapshots_directory}",
                f"SPORTS_ANALYTICS_STORAGE__FEATURES_DIRECTORY={paths.features_directory}",
                f"SPORTS_ANALYTICS_STORAGE__MODELS_DIRECTORY={paths.models_directory}",
                f"SPORTS_ANALYTICS_STORAGE__EXPORTS_DIRECTORY={paths.exports_directory}",
                f"SPORTS_ANALYTICS_STORAGE__SQLITE_PATH={paths.sqlite_path}",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    assert (
        engine_main(
            [
                "--env-file",
                str(env_path),
                "--generate-verified-predictions",
                str(request_path),
                "--model",
                trained.final_artifact_relative_directory,
                "--features",
                artifact.relative_directory,
                "--model-checksum",
                trained.final_artifact_checksum,
                "--feature-checksum",
                artifact.manifest_checksum_sha256,
            ]
        )
        == 0
    )
