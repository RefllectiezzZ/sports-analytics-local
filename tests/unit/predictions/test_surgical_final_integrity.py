"""Focused regressions for PR #8 surgical final integrity correction."""

from __future__ import annotations

from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
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
    build_backtest_datasets,
    serialize_combination_row,
    serialize_opportunity_row,
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
from sports_analytics.combinations.builder import build_combinations
from sports_analytics.combinations.contracts import CombinationRules
from sports_analytics.combinations.evidence import CombinationEvidenceMode
from sports_analytics.core.exceptions import (
    ArtifactError,
    ConfigurationError,
    ModelError,
    PredictionError,
)
from sports_analytics.opportunities.contracts import OpportunityFilter
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
from sports_analytics.services.analysis_json import publish_analysis_with_paths
from sports_analytics.services.historical_analysis import publish_historical_analysis_with_paths
from sports_analytics.value.contracts import (
    CompleteMarketQuote,
    PricedSelection,
    QuoteEvaluationMode,
)


def _two_market_analysis_datasets(tmp_path: Path) -> dict[str, tuple[dict[str, object], ...]]:
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
    return {
        name: tuple(loaded.dataset(name).rows)
        for name in (
            "predictions",
            "market_evaluations",
            "opportunities",
            "opportunity_decisions",
            "combinations",
            "rejections",
        )
    }


def _combination_fixture_rows() -> dict[str, object]:
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
    build = build_combinations(
        (first, second),
        rules=CombinationRules(
            minimum_legs=2,
            maximum_legs=2,
            allow_multiple_sports=True,
            allow_multiple_dates=True,
        ),
        evidence_mode=CombinationEvidenceMode.SYNTHETIC_CONTRACT,
    )
    assert build.combinations
    return serialize_combination_row(build.combinations[0])


def _analysis_payload() -> dict[str, object]:
    prediction = _prediction()
    quote = _quote(prediction)
    return {
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
            "quoted_at_utc": quote.quoted_at_utc.isoformat().replace("+00:00", "Z")
            if quote.quoted_at_utc is not None
            else None,
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
        "filters": {},
    }


def test_generic_json_analysis_rejects_historical_replay(tmp_path: Path) -> None:
    paths = _runtime(tmp_path)
    payload = {**_analysis_payload(), "provenance": "historical-replay"}
    with pytest.raises(ConfigurationError, match="synthetic-contract"):
        publish_analysis_with_paths(payload, paths=paths)


def test_trusted_historical_analysis_verifies_model_and_features(tmp_path: Path) -> None:
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
    quote = CompleteMarketQuote(
        canonical_event_id=vector.metadata.canonical_event_id,
        source_name="feed",
        provider_type="bookmaker",
        provider_id="provider-a",
        quote_phase="current",
        source_observed_at_utc=event_start - timedelta(hours=1),
        quoted_at_utc=event_start - timedelta(hours=2),
        quote_timestamp_precision="exact",
        quote_valid_from_utc=None,
        quote_valid_to_utc=None,
        selections=tuple(
            PricedSelection(
                selection=item.selection,
                decimal_odds=Decimal("2.10"),
                quote_series_id=f"series-{index}",
                quote_observation_id=f"quote-{index}",
            )
            for index, item in enumerate(prediction.probabilities)
        ),
    )
    quote_payload = {
        "canonical_event_id": quote.canonical_event_id,
        "source_name": quote.source_name,
        "provider_type": quote.provider_type,
        "provider_id": quote.provider_id,
        "quote_phase": quote.quote_phase,
        "source_observed_at_utc": quote.source_observed_at_utc.isoformat().replace("+00:00", "Z"),
        "quoted_at_utc": quote.quoted_at_utc.isoformat().replace("+00:00", "Z")
        if quote.quoted_at_utc is not None
        else None,
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
    }
    published = publish_historical_analysis_with_paths(
        {
            "canonical_event_id": vector.metadata.canonical_event_id,
            "event_start_utc": event_start.isoformat().replace("+00:00", "Z"),
            "quote": quote_payload,
            "filters": {"minimum_edge": -1, "minimum_expected_value": -1},
        },
        paths=paths,
        model_relative_path=trained.final_artifact_relative_directory,
        model_checksum_sha256=trained.final_artifact_checksum,
        feature_relative_directory=artifact.relative_directory,
        feature_manifest_checksum_sha256=artifact.manifest_checksum_sha256,
    )
    loaded = load_typed_analytical_artifact(
        root=paths.exports_directory,
        relative_directory=str(published["relative_directory"]),
        expected_kind="analysis",
        expected_schema_version=ANALYSIS_ARTIFACT_SCHEMA,
        expected_checksum=str(published["checksum_sha256"]),
    )
    prediction_row = loaded.dataset("predictions").rows[0]
    assert prediction_row["provenance"] == "historical-replay"
    with pytest.raises((PredictionError, ModelError), match="checksum"):
        publish_historical_analysis_with_paths(
            {
                "canonical_event_id": vector.metadata.canonical_event_id,
                "event_start_utc": event_start.isoformat().replace("+00:00", "Z"),
                "quote": quote_payload,
            },
            paths=paths,
            model_relative_path=trained.final_artifact_relative_directory,
            model_checksum_sha256="f" * 64,
            feature_relative_directory=artifact.relative_directory,
            feature_manifest_checksum_sha256=artifact.manifest_checksum_sha256,
        )


def test_numeric_selection_fields_rejected_instead_of_coerced(tmp_path: Path) -> None:
    paths = _runtime(tmp_path)
    published = publish_analysis_artifact(
        paths=paths,
        request=AnalysisPublicationRequest(
            markets=(
                AnalysisMarketInput(
                    prediction=_prediction_from_opportunity(
                        build_test_opportunity("1", event_id="event-1", start=START)
                    ),
                    quote=_quote_from_opportunity(
                        build_test_opportunity("1", event_id="event-1", start=START)
                    ),
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
    row = dict(loaded.dataset("predictions").rows[0])
    probabilities = deepcopy(row["probabilities"])
    assert isinstance(probabilities, list) and probabilities
    first = dict(probabilities[0])
    selection = dict(first["selection"])
    selection["sport_code"] = 123
    first["selection"] = selection
    probabilities[0] = first
    row["probabilities"] = probabilities
    with pytest.raises(ArtifactError):
        validate_dataset_row_schema("predictions", row, version="predictions-v2")


def test_string_booleans_rejected() -> None:
    from tests.unit.artifacts.test_analytical_artifacts import _typed_datasets

    datasets = _typed_datasets()
    row = dict(datasets["opportunity_decisions"][0])
    row["eligible"] = "false"
    with pytest.raises(ArtifactError, match="boolean"):
        validate_dataset_row_schema(
            "opportunity_decisions",
            row,
            version="opportunity-decisions-v2",
        )


def test_malformed_decimals_raise_artifact_error() -> None:
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


def test_selection_id_must_match_selection_payload(tmp_path: Path) -> None:
    paths = _runtime(tmp_path)
    published = publish_analysis_artifact(
        paths=paths,
        request=AnalysisPublicationRequest(
            markets=(
                AnalysisMarketInput(
                    prediction=_prediction_from_opportunity(
                        build_test_opportunity("1", event_id="event-1", start=START)
                    ),
                    quote=_quote_from_opportunity(
                        build_test_opportunity("1", event_id="event-1", start=START)
                    ),
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
    row = dict(loaded.dataset("predictions").rows[0])
    probabilities = deepcopy(row["probabilities"])
    assert isinstance(probabilities, list) and probabilities
    first = dict(probabilities[0])
    first["selection_id"] = "forged-selection-id"
    probabilities[0] = first
    row["probabilities"] = probabilities
    with pytest.raises(ArtifactError, match="selection_id"):
        validate_dataset_row_schema("predictions", row, version="predictions-v2")


def test_forged_edge_and_ev_rejected_after_opportunity_id_recompute() -> None:
    opportunity = build_test_opportunity("1", event_id="event-1", start=START)
    row = serialize_opportunity_row(opportunity)
    row["edge"] = float(row["edge"]) + 0.25
    row["expected_value"] = float(row["expected_value"]) + 0.25
    row["opportunity_id"] = "forged-opportunity-id"
    with pytest.raises(ArtifactError, match="inconsistent|does not match"):
        validate_dataset_row_schema("opportunities", row, version="opportunities-v2")


def test_forged_combination_id_rejected() -> None:
    row = _combination_fixture_rows()
    forged = dict(row)
    forged["combination_id"] = "forged-combination-id"
    with pytest.raises(ArtifactError, match="does not match"):
        validate_dataset_row_schema("combinations", forged, version="combinations-v2")


def test_forged_total_odds_or_joint_probability_rejected() -> None:
    row = _combination_fixture_rows()
    forged_odds = dict(row)
    forged_odds["total_decimal_odds"] = "999.0"
    with pytest.raises(ArtifactError, match="does not match|inconsistent"):
        validate_dataset_row_schema("combinations", forged_odds, version="combinations-v2")
    forged_joint = dict(row)
    forged_joint["joint_probability"] = 0.99
    with pytest.raises(ArtifactError, match="does not match|inconsistent"):
        validate_dataset_row_schema("combinations", forged_joint, version="combinations-v2")


def test_combination_using_rejected_opportunity_rejected(tmp_path: Path) -> None:
    datasets = {name: list(rows) for name, rows in _two_market_analysis_datasets(tmp_path).items()}
    datasets["settlements"] = []
    decisions = [dict(row) for row in datasets["opportunity_decisions"]]
    decisions[0]["eligible"] = False
    decisions[0]["rejection_codes"] = ["edge"]
    decisions[0]["accepted_rank"] = None
    eligible_rank = 1
    for decision in decisions[1:]:
        if decision["eligible"] is True:
            decision["accepted_rank"] = eligible_rank
            eligible_rank += 1
    datasets["opportunity_decisions"] = tuple(decisions)
    with pytest.raises(ArtifactError, match="ineligible opportunity"):
        validate_cross_dataset_integrity({name: tuple(rows) for name, rows in datasets.items()})


def test_single_settlement_using_rejected_opportunity_rejected(tmp_path: Path) -> None:
    datasets = {name: list(rows) for name, rows in _two_market_analysis_datasets(tmp_path).items()}
    opportunity_row = datasets["opportunities"][0]
    datasets["settlements"] = (
        {
            "bet_id": "forced-bet",
            "schema_version": "settlements-v2",
            "fold_id": "fold-1",
            "kind": "single",
            "opportunity_ids": [opportunity_row["opportunity_id"]],
            "decimal_odds": opportunity_row["decimal_odds"],
            "result": "win",
            "stake_units": "1",
            "returned_units": opportunity_row["decimal_odds"],
            "profit_units": "1.0",
            "strategy_id": "strategy-1",
        },
    )
    decisions = [dict(row) for row in datasets["opportunity_decisions"]]
    eligible_rank = 1
    for decision in decisions:
        if decision["opportunity_id"] == opportunity_row["opportunity_id"]:
            decision["eligible"] = False
            decision["rejection_codes"] = ["edge"]
            decision["accepted_rank"] = None
        elif decision["eligible"] is True:
            decision["accepted_rank"] = eligible_rank
            eligible_rank += 1
    datasets["opportunity_decisions"] = tuple(decisions)
    with pytest.raises(ArtifactError, match="ineligible opportunity"):
        validate_cross_dataset_integrity({name: tuple(rows) for name, rows in datasets.items()})


def test_combination_settlement_without_persisted_combination_rejected(tmp_path: Path) -> None:
    datasets = {name: list(rows) for name, rows in _two_market_analysis_datasets(tmp_path).items()}
    opportunity_ids = [row["opportunity_id"] for row in datasets["opportunities"]]
    datasets["combinations"] = ()
    datasets["settlements"] = (
        {
            "bet_id": "combo-bet",
            "schema_version": "settlements-v2",
            "fold_id": "fold-1",
            "kind": "combination",
            "opportunity_ids": opportunity_ids,
            "decimal_odds": "4.0",
            "result": "win",
            "stake_units": "1",
            "returned_units": "4.0",
            "profit_units": "3.0",
            "strategy_id": "strategy-1",
            "combination_id": "missing-combination",
        },
    )
    with pytest.raises(ArtifactError, match="missing combination"):
        validate_cross_dataset_integrity({name: tuple(rows) for name, rows in datasets.items()})


def test_combination_settlement_leg_mismatch_rejected(tmp_path: Path) -> None:
    datasets = {name: list(rows) for name, rows in _two_market_analysis_datasets(tmp_path).items()}
    combination_row = dict(datasets["combinations"][0])
    opportunity_ids = list(combination_row["opportunity_ids"])
    datasets["settlements"] = (
        {
            "bet_id": "combo-bet",
            "schema_version": "settlements-v2",
            "fold_id": "fold-1",
            "kind": "combination",
            "opportunity_ids": list(reversed(opportunity_ids)),
            "decimal_odds": combination_row["total_decimal_odds"],
            "result": "win",
            "stake_units": "1",
            "returned_units": combination_row["total_decimal_odds"],
            "profit_units": "1.0",
            "strategy_id": "strategy-1",
            "combination_id": combination_row["combination_id"],
        },
    )
    with pytest.raises(ArtifactError, match="do not match"):
        validate_cross_dataset_integrity({name: tuple(rows) for name, rows in datasets.items()})


def test_end_to_end_analysis_and_backtest_artifacts_publish_and_reload(tmp_path: Path) -> None:
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
    datasets = build_backtest_datasets(
        result=result,
        predictions=(),
        evaluations=(),
        feature_artifact_id="feature-1",
        feature_manifest_checksum_sha256="b" * 64,
        input_snapshots=(),
        random_seed=1,
        test_event_count=2,
        complete_quote_event_count=2,
        quote_coverage=1.0,
    )
    assert datasets["combinations"]


def test_deterministic_repeated_publication_unchanged(tmp_path: Path) -> None:
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
