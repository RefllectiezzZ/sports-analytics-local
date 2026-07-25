"""Explicit JSON adapters for focused prediction/value/combination/backtest CLI modes."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import cast

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
from sports_analytics.combinations.contracts import (
    Combination,
    CombinationRules,
    validate_combination_manual,
)
from sports_analytics.combinations.evidence import (
    SYNTHETIC_COMBINATION_EVIDENCE_LABEL,
    CombinationEvidenceMode,
)
from sports_analytics.core.exceptions import ConfigurationError
from sports_analytics.core.paths import RuntimePaths
from sports_analytics.data.codec import format_utc_timestamp
from sports_analytics.data.types import JsonValue
from sports_analytics.opportunities.contracts import (
    Opportunity,
    OpportunityFilter,
    OpportunityRankingMode,
    filter_and_rank_opportunities,
    opportunities_from_evaluation,
)
from sports_analytics.opportunities.dependency import (
    DependencyMetadataProvenance,
    MarketDependencyMetadata,
    SelectionDependencyMetadata,
)
from sports_analytics.opportunities.identity import (
    OPPORTUNITY_IDENTITY_VERSION,
    derive_opportunity_id,
    verify_opportunity_identity,
)
from sports_analytics.predictions.contracts import (
    CanonicalSelectionIdentity,
    MarketPrediction,
    PredictionInputSnapshot,
    PredictionLineage,
    PredictionQualityFlags,
    SelectionProbability,
    build_market_prediction,
)
from sports_analytics.predictions.provenance import (
    PredictionProvenance,
    parse_prediction_provenance,
)
from sports_analytics.predictions.synthetic import build_synthetic_market_prediction
from sports_analytics.services.analysis import (
    ANALYSIS_ARTIFACT_SCHEMA,
    AnalysisMarketInput,
    AnalysisPublicationRequest,
    publish_analysis_artifact,
)
from sports_analytics.value.contracts import (
    CompleteMarketQuote,
    PricedSelection,
    QuoteEvaluationMode,
    evaluate_complete_market,
)


def generate_predictions_from_json(payload: object) -> dict[str, JsonValue]:
    """Validate one synthetic complete-market prediction (never production-eligible by default)."""
    prediction = _synthetic_prediction(_mapping(payload, "prediction request"))
    return _prediction_json(prediction)


def evaluate_opportunities_from_json(payload: object) -> dict[str, JsonValue]:
    """Evaluate one complete quote and persist deterministic filter decisions."""
    document = _mapping(payload, "evaluation request")
    prediction = _synthetic_prediction(_mapping(document.get("prediction"), "prediction"))
    quote_payload = _mapping(document.get("quote"), "quote")
    quote = CompleteMarketQuote(
        canonical_event_id=_string(quote_payload, "canonical_event_id"),
        source_name=_string(quote_payload, "source_name"),
        provider_type=_string(quote_payload, "provider_type"),
        provider_id=_string(quote_payload, "provider_id"),
        quote_phase=_string(quote_payload, "quote_phase"),
        source_observed_at_utc=_datetime(quote_payload, "source_observed_at_utc"),
        quoted_at_utc=_optional_datetime(quote_payload.get("quoted_at_utc")),
        quote_timestamp_precision=_string(quote_payload, "quote_timestamp_precision"),
        quote_valid_from_utc=_optional_datetime(quote_payload.get("quote_valid_from_utc")),
        quote_valid_to_utc=_optional_datetime(quote_payload.get("quote_valid_to_utc")),
        selections=tuple(
            PricedSelection(
                selection=_selection(_mapping(item, "priced selection")),
                decimal_odds=_decimal(_mapping(item, "priced selection"), "decimal_odds"),
                quote_series_id=_string(_mapping(item, "priced selection"), "quote_series_id"),
                quote_observation_id=_string(
                    _mapping(item, "priced selection"),
                    "quote_observation_id",
                ),
            )
            for item in _array(quote_payload, "selections")
        ),
    )
    mode = _enum(
        QuoteEvaluationMode,
        document.get("mode", QuoteEvaluationMode.LIVE_SAFE.value),
        "mode",
    )
    evaluation = evaluate_complete_market(prediction=prediction, quote=quote, mode=mode)
    opportunities = opportunities_from_evaluation(evaluation)
    filters = _filters(_mapping(document.get("filters", {}), "filters"))
    search = filter_and_rank_opportunities(opportunities, filters=filters)
    return {
        "evaluation_version": evaluation.evaluation_version,
        "overround": evaluation.overround,
        "opportunities": [_opportunity_json(item) for item in opportunities],
        "decisions": [
            {
                "opportunity_id": item.opportunity_id,
                "filter_config_id": item.filter_config_id,
                "decision_as_of_utc": format_utc_timestamp(item.decision_as_of_utc),
                "eligible": item.eligible,
                "rejection_codes": [code.value for code in item.rejection_codes],
                "accepted_rank": item.accepted_rank,
            }
            for item in search.decisions
        ],
    }


def publish_analysis_with_paths(
    payload: object,
    *,
    paths: RuntimePaths,
) -> dict[str, JsonValue]:
    """Build, filter, combine, atomically publish, and reload one verified analysis artifact."""
    document = _mapping(payload, "analysis publication request")
    provenance = parse_prediction_provenance(
        document.get("provenance", PredictionProvenance.SYNTHETIC_CONTRACT.value),
        field_name="provenance",
    )
    markets_payload = document.get("markets")
    if markets_payload is not None:
        markets = tuple(
            _analysis_market_input(_mapping(item, "market"), provenance=provenance)
            for item in _array(document, "markets")
        )
        mode = _enum(
            QuoteEvaluationMode,
            document.get("mode", QuoteEvaluationMode.LIVE_SAFE.value),
            "mode",
        )
    else:
        mode = _enum(
            QuoteEvaluationMode,
            document.get("mode", QuoteEvaluationMode.LIVE_SAFE.value),
            "mode",
        )
        if provenance is PredictionProvenance.HISTORICAL_REPLAY:
            raise ConfigurationError(
                "historical-replay analysis requires verified prediction markets, not synthetic"
            )
        markets = (
            _analysis_market_input(
                {
                    "prediction": document.get("prediction"),
                    "quote": document.get("quote"),
                },
                provenance=provenance,
            ),
        )
    filters = _filters(_mapping(document.get("filters", {}), "filters"))
    rules_payload = document.get("combination_rules")
    combination_rules = (
        None if rules_payload is None else _rules(_mapping(rules_payload, "combination_rules"))
    )
    relative_directory = _optional_string(document.get("relative_directory"))
    published = publish_analysis_artifact(
        paths=paths,
        request=AnalysisPublicationRequest(
            markets=markets,
            mode=mode,
            filters=filters,
            combination_rules=combination_rules,
            provenance=provenance,
            relative_directory=relative_directory,
        ),
    )
    return {
        "artifact_id": published.artifact_id,
        "checksum_sha256": published.checksum_sha256,
        "relative_directory": published.relative_directory,
        "analysis_run_id": published.analysis_run_id,
        "schema_version": ANALYSIS_ARTIFACT_SCHEMA,
        "provenance": provenance.value,
    }


def build_combinations_from_json(payload: object) -> dict[str, JsonValue]:
    """Build bounded combinations from explicit synthetic-contract opportunity JSON."""
    document = _mapping(payload, "combination build request")
    provenance = parse_prediction_provenance(
        document.get("provenance", PredictionProvenance.SYNTHETIC_CONTRACT.value),
        field_name="provenance",
    )
    if provenance is not PredictionProvenance.SYNTHETIC_CONTRACT:
        raise ConfigurationError("raw combination JSON accepts only synthetic-contract provenance")
    opportunities = tuple(
        _verified_opportunity(_opportunity_entry(item))
        for item in _array(document, "opportunities")
    )
    rules = _rules(_mapping(document.get("rules", {}), "rules"))
    result = build_combinations(
        opportunities,
        rules=rules,
        evidence_mode=CombinationEvidenceMode.SYNTHETIC_CONTRACT,
    )
    return {
        "evidence_label": SYNTHETIC_COMBINATION_EVIDENCE_LABEL,
        "provenance": provenance.value,
        "combinations": [_combination_json(item) for item in result.combinations],
        "rejections": [
            {"opportunity_ids": list(item.opportunity_ids), "reason": item.reason}
            for item in result.rejections
        ],
        "candidates_considered": result.candidates_considered,
        "combinations_evaluated": result.combinations_evaluated,
        "truncated": result.truncated,
    }


def validate_combination_from_json(payload: object) -> dict[str, JsonValue]:
    """Validate one exact manually selected leg set from synthetic-contract JSON."""
    document = _mapping(payload, "manual combination request")
    provenance = parse_prediction_provenance(
        document.get("provenance", PredictionProvenance.SYNTHETIC_CONTRACT.value),
        field_name="provenance",
    )
    if provenance is not PredictionProvenance.SYNTHETIC_CONTRACT:
        raise ConfigurationError(
            "raw combination validation accepts only synthetic-contract provenance"
        )
    result = validate_combination_manual(
        tuple(
            _verified_opportunity(_opportunity_entry(item))
            for item in _array(document, "opportunities")
        ),
        rules=_rules(_mapping(document.get("rules", {}), "rules")),
    )
    if result.combination is None:
        return {
            "evidence_label": SYNTHETIC_COMBINATION_EVIDENCE_LABEL,
            "provenance": provenance.value,
            "eligible": False,
            "rejection_reasons": list(result.rejection_reasons),
            "dependencies": [
                {
                    "left_opportunity_id": relation.left_opportunity_id,
                    "right_opportunity_id": relation.right_opportunity_id,
                    "classification": relation.classification.value,
                    "reason": relation.reason,
                }
                for relation in result.dependencies
            ],
        }
    return _combination_json(result.combination)


def run_backtest_from_json(payload: object) -> dict[str, JsonValue]:
    """Run rolling-origin backtesting from explicit folds and a fixed strategy."""
    document = _mapping(payload, "backtest request")
    strategy_payload = _mapping(document.get("strategy"), "strategy")
    mode = _enum(BacktestMode, strategy_payload.get("mode"), "strategy.mode")
    rules_payload = strategy_payload.get("combination_rules")
    strategy = StrategyConfiguration(
        strategy_version=_string(strategy_payload, "strategy_version"),
        mode=mode,
        opportunity_filter=_filters(
            _mapping(strategy_payload.get("filters", {}), "strategy.filters")
        ),
        include_singles=_boolean(strategy_payload.get("include_singles", True), "include_singles"),
        include_combinations=_boolean(
            strategy_payload.get("include_combinations", False),
            "include_combinations",
        ),
        combination_rules=(
            None if rules_payload is None else _rules(_mapping(rules_payload, "combination_rules"))
        ),
    )
    folds: list[FoldBacktestInput] = []
    for raw_fold in _array(document, "folds"):
        fold_payload = _mapping(raw_fold, "fold")
        fold = BacktestFold(
            fold_id=_string(fold_payload, "fold_id"),
            train_start_date=_date(fold_payload, "train_start_date"),
            train_end_date=_date(fold_payload, "train_end_date"),
            calibration_start_date=_date(fold_payload, "calibration_start_date"),
            calibration_end_date=_date(fold_payload, "calibration_end_date"),
            test_start_date=_date(fold_payload, "test_start_date"),
            test_end_date=_date(fold_payload, "test_end_date"),
        )
        candidates = tuple(
            SettledOpportunity(
                opportunity=_verified_opportunity(
                    _mapping(_mapping(item, "settled candidate").get("opportunity"), "opportunity")
                ),
                result=_enum(
                    SettlementResult,
                    _mapping(item, "settled candidate").get("result"),
                    "result",
                ),
            )
            for item in _array(fold_payload, "candidates")
        )
        folds.append(FoldBacktestInput(fold=fold, candidates=candidates))
    result = run_backtest(tuple(folds), strategy=strategy)
    return {
        "backtest_id": result.backtest_id,
        "strategy_id": result.strategy_id,
        "bet_count": result.metrics.bet_count,
        "candidate_count": result.metrics.candidate_count,
        "rejection_count": result.metrics.rejection_count,
        "accepted_single_count": result.metrics.accepted_single_count,
        "accepted_combination_count": result.metrics.accepted_combination_count,
        "net_profit_units": format(result.metrics.net_profit_units, "f"),
        "gross_return_units": format(result.metrics.gross_return_units, "f"),
        "cumulative_profit_units": [
            format(item, "f") for item in result.metrics.cumulative_profit_units
        ],
        "maximum_drawdown_units": format(result.metrics.maximum_drawdown_units, "f"),
        "all_log_loss": result.metrics.all_log_loss,
        "all_multiclass_brier_score": result.metrics.all_multiclass_brier_score,
        "selected_log_loss": result.metrics.selected_log_loss,
        "selected_multiclass_brier_score": result.metrics.selected_multiclass_brier_score,
    }


def _analysis_market_input(
    document: dict[str, object],
    *,
    provenance: PredictionProvenance,
) -> AnalysisMarketInput:
    if provenance is PredictionProvenance.HISTORICAL_REPLAY:
        prediction = _verified_prediction_row(_mapping(document.get("prediction"), "prediction"))
    else:
        prediction = _synthetic_prediction(_mapping(document.get("prediction"), "prediction"))
    quote = _complete_quote_from_payload(_mapping(document.get("quote"), "quote"))
    metadata_payload = document.get("dependency_metadata")
    dependency_metadata = None
    if metadata_payload is not None:
        dependency_metadata = _dependency_metadata_from_payload(
            _mapping(metadata_payload, "dependency_metadata"),
            provenance=provenance,
        )
    return AnalysisMarketInput(
        prediction=prediction,
        quote=quote,
        dependency_metadata=dependency_metadata,
    )


def _verified_prediction_row(document: dict[str, object]) -> MarketPrediction:
    provenance = parse_prediction_provenance(
        document.get("provenance", PredictionProvenance.HISTORICAL_REPLAY.value),
        field_name="provenance",
    )
    if provenance is not PredictionProvenance.HISTORICAL_REPLAY:
        raise ConfigurationError("verified prediction row requires historical-replay provenance")
    lineage_payload = _mapping(document.get("lineage"), "lineage")
    quality_payload = _mapping(document.get("quality", {}), "quality")
    raw_probabilities = _array(document, "probabilities")
    probabilities = tuple(
        SelectionProbability(
            selection=_selection_from_probability_item(_mapping(item, "probability selection")),
            probability=_number(_mapping(item, "probability selection"), "probability"),
        )
        for item in raw_probabilities
    )
    return build_market_prediction(
        canonical_event_id=_string(document, "canonical_event_id"),
        event_start_utc=_datetime(document, "event_start_utc"),
        predicted_at_utc=_datetime(document, "predicted_at_utc"),
        feature_available_at_utc=_datetime(document, "feature_available_at_utc"),
        lineage=_lineage_from_payload(lineage_payload),
        probabilities=probabilities,
        quality=PredictionQualityFlags(
            calibrated=_boolean(quality_payload.get("calibrated", False), "calibrated"),
            model_artifact_verified=_boolean(
                quality_payload.get("model_artifact_verified", False),
                "model_artifact_verified",
            ),
            feature_artifact_verified=_boolean(
                quality_payload.get("feature_artifact_verified", False),
                "feature_artifact_verified",
            ),
            sufficient_history=_boolean(
                quality_payload.get("sufficient_history", False),
                "sufficient_history",
            ),
            data_quality_passed=_boolean(
                quality_payload.get("data_quality_passed", False),
                "data_quality_passed",
            ),
        ),
        provenance=provenance,
    )


def _dependency_metadata_from_payload(
    document: dict[str, object],
    *,
    provenance: PredictionProvenance,
) -> MarketDependencyMetadata:
    if provenance is not PredictionProvenance.SYNTHETIC_CONTRACT:
        raise ConfigurationError("explicit dependency metadata is synthetic-contract only")
    by_selection: dict[str, SelectionDependencyMetadata] = {}
    for item in _array(document, "selections"):
        payload = _mapping(item, "dependency metadata selection")
        selection_id = _string(payload, "selection_id")
        by_selection[selection_id] = SelectionDependencyMetadata(
            selection_id=selection_id,
            dependency_keys=frozenset(_string_array(payload.get("dependency_keys", []))),
            participant_ids=frozenset(_string_array(payload.get("participant_ids", []))),
            dependency_metadata_complete=_boolean(
                payload.get("dependency_metadata_complete", False),
                "dependency_metadata_complete",
            ),
            metadata_provenance=DependencyMetadataProvenance.SYNTHETIC_CONTRACT,
        )
    return MarketDependencyMetadata(by_selection_id=by_selection)


def _lineage_from_payload(lineage_payload: dict[str, object]) -> PredictionLineage:
    return PredictionLineage(
        model_artifact_id=_string(lineage_payload, "model_artifact_id"),
        model_checksum_sha256=_string(lineage_payload, "model_checksum_sha256"),
        model_specification_version=_string(lineage_payload, "model_specification_version"),
        feature_artifact_id=_string(lineage_payload, "feature_artifact_id"),
        feature_manifest_checksum_sha256=_string(
            lineage_payload,
            "feature_manifest_checksum_sha256",
        ),
        feature_specification_version=_string(lineage_payload, "feature_specification_version"),
        feature_row_id=_string(lineage_payload, "feature_row_id"),
        trained_through_date=_date(lineage_payload, "trained_through_date"),
        calibrated_through_date=_date(lineage_payload, "calibrated_through_date"),
        input_snapshots=tuple(
            PredictionInputSnapshot(
                snapshot_id=_string(_mapping(item, "input snapshot"), "snapshot_id"),
                manifest_checksum_sha256=_string(
                    _mapping(item, "input snapshot"),
                    "manifest_checksum_sha256",
                ),
                schema_version=_string(_mapping(item, "input snapshot"), "schema_version"),
                source_name=_string(_mapping(item, "input snapshot"), "source_name"),
            )
            for item in _array(lineage_payload, "input_snapshots", default=[])
        ),
    )


def _selection_from_probability_item(document: dict[str, object]) -> CanonicalSelectionIdentity:
    selection_payload = document.get("selection")
    if isinstance(selection_payload, dict):
        return _selection(selection_payload)
    return _selection(document)


def _complete_quote_from_payload(quote_payload: dict[str, object]) -> CompleteMarketQuote:
    return CompleteMarketQuote(
        canonical_event_id=_string(quote_payload, "canonical_event_id"),
        source_name=_string(quote_payload, "source_name"),
        provider_type=_string(quote_payload, "provider_type"),
        provider_id=_string(quote_payload, "provider_id"),
        quote_phase=_string(quote_payload, "quote_phase"),
        source_observed_at_utc=_datetime(quote_payload, "source_observed_at_utc"),
        quoted_at_utc=_optional_datetime(quote_payload.get("quoted_at_utc")),
        quote_timestamp_precision=_string(quote_payload, "quote_timestamp_precision"),
        quote_valid_from_utc=_optional_datetime(quote_payload.get("quote_valid_from_utc")),
        quote_valid_to_utc=_optional_datetime(quote_payload.get("quote_valid_to_utc")),
        selections=tuple(
            PricedSelection(
                selection=_selection(_mapping(item, "priced selection")),
                decimal_odds=_decimal(_mapping(item, "priced selection"), "decimal_odds"),
                quote_series_id=_string(_mapping(item, "priced selection"), "quote_series_id"),
                quote_observation_id=_string(
                    _mapping(item, "priced selection"),
                    "quote_observation_id",
                ),
            )
            for item in _array(quote_payload, "selections")
        ),
    )


def _synthetic_prediction(document: dict[str, object]) -> MarketPrediction:
    lineage_payload = _mapping(document.get("lineage"), "lineage")
    quality_payload = _mapping(document.get("quality", {}), "quality")
    raw_probabilities = _array(document, "probabilities")
    probabilities = tuple(
        SelectionProbability(
            selection=_selection(_mapping(item, "probability selection")),
            probability=_number(_mapping(item, "probability selection"), "probability"),
        )
        for item in raw_probabilities
    )
    return build_synthetic_market_prediction(
        canonical_event_id=_string(document, "canonical_event_id"),
        event_start_utc=_datetime(document, "event_start_utc"),
        predicted_at_utc=_datetime(document, "predicted_at_utc"),
        feature_available_at_utc=_datetime(document, "feature_available_at_utc"),
        lineage=PredictionLineage(
            model_artifact_id=_string(lineage_payload, "model_artifact_id"),
            model_checksum_sha256=_string(lineage_payload, "model_checksum_sha256"),
            model_specification_version=_string(
                lineage_payload,
                "model_specification_version",
            ),
            feature_artifact_id=_string(lineage_payload, "feature_artifact_id"),
            feature_manifest_checksum_sha256=_string(
                lineage_payload,
                "feature_manifest_checksum_sha256",
            ),
            feature_specification_version=_string(
                lineage_payload,
                "feature_specification_version",
            ),
            feature_row_id=_string(lineage_payload, "feature_row_id"),
            trained_through_date=_date(lineage_payload, "trained_through_date"),
            calibrated_through_date=_date(lineage_payload, "calibrated_through_date"),
            input_snapshots=tuple(
                PredictionInputSnapshot(
                    snapshot_id=_string(_mapping(item, "input snapshot"), "snapshot_id"),
                    manifest_checksum_sha256=_string(
                        _mapping(item, "input snapshot"),
                        "manifest_checksum_sha256",
                    ),
                    schema_version=_string(
                        _mapping(item, "input snapshot"),
                        "schema_version",
                    ),
                    source_name=_string(_mapping(item, "input snapshot"), "source_name"),
                )
                for item in _array(lineage_payload, "input_snapshots", default=[])
            ),
        ),
        probabilities=probabilities,
        ordered_selection_space=tuple(item.selection for item in probabilities),
        quality=PredictionQualityFlags(
            calibrated=_boolean(quality_payload.get("calibrated", False), "calibrated"),
            model_artifact_verified=_boolean(
                quality_payload.get("model_artifact_verified", False),
                "model_artifact_verified",
            ),
            feature_artifact_verified=_boolean(
                quality_payload.get("feature_artifact_verified", False),
                "feature_artifact_verified",
            ),
            sufficient_history=_boolean(
                quality_payload.get("sufficient_history", False),
                "sufficient_history",
            ),
            data_quality_passed=_boolean(
                quality_payload.get("data_quality_passed", False),
                "data_quality_passed",
            ),
        ),
    )


def _verified_opportunity(document: dict[str, object]) -> Opportunity:
    opportunity = _opportunity(document)
    verify_opportunity_identity(opportunity)
    return opportunity


def _opportunity_entry(item: object) -> dict[str, object]:
    entry = _mapping(item, "opportunity entry")
    payload = entry.get("opportunity", entry)
    return _mapping(payload, "opportunity")


def _selection(document: dict[str, object]) -> CanonicalSelectionIdentity:
    line_value = document.get("line_value")
    return CanonicalSelectionIdentity(
        sport_code=_string(document, "sport_code"),
        market_family=_string(document, "market_family"),
        market_key=_string(document, "market_key"),
        market_period=_string(document, "market_period"),
        participant_scope=_string(document, "participant_scope"),
        canonical_participant_id=_optional_string(document.get("canonical_participant_id")),
        line_type=_string(document, "line_type"),
        line_value=None if line_value is None else _decimal_value(line_value, "line_value"),
        outcome_key=_string(document, "outcome_key"),
    )


def _opportunity(document: dict[str, object]) -> Opportunity:
    evaluation_mode = _enum(
        QuoteEvaluationMode,
        document.get("evaluation_mode"),
        "evaluation_mode",
    )
    predicted_at = _datetime(document, "predicted_at_utc")
    quoted_at = _optional_datetime(document.get("quoted_at_utc"))
    source_observed_at = _datetime(document, "source_observed_at_utc")
    event_start = _datetime(document, "event_start_utc")
    if evaluation_mode is QuoteEvaluationMode.LIVE_SAFE:
        if quoted_at is None:
            raise ConfigurationError("live-safe opportunity requires quoted_at_utc")
        decision_as_of = max(predicted_at, quoted_at, source_observed_at)
    else:
        decision_as_of = event_start
    decimal_odds = _decimal(document, "decimal_odds")
    selection = _selection(_mapping(document.get("selection"), "selection"))
    evaluation_version = (
        _string(document, "evaluation_version")
        if "evaluation_version" in document
        else "complete-market-value-v1"
    )
    quote_series_id = (
        _string(document, "quote_series_id")
        if "quote_series_id" in document
        else _string(document, "quote_observation_id")
    )
    payload: dict[str, JsonValue] = {
        "identity_version": OPPORTUNITY_IDENTITY_VERSION,
        "evaluation_version": evaluation_version,
        "canonical_event_id": _string(document, "canonical_event_id"),
        "event_start_utc": format_utc_timestamp(event_start),
        "selection": selection.identity_payload(),
        "prediction_id": _string(document, "prediction_id"),
        "predicted_at_utc": format_utc_timestamp(predicted_at),
        "quote_series_id": quote_series_id,
        "quote_observation_id": _string(document, "quote_observation_id"),
        "source_name": _string(document, "source_name"),
        "provider_type": _string(document, "provider_type"),
        "provider_id": _string(document, "provider_id"),
        "evaluation_mode": evaluation_mode.value,
        "quoted_at_utc": None if quoted_at is None else format_utc_timestamp(quoted_at),
        "source_observed_at_utc": format_utc_timestamp(source_observed_at),
        "decision_as_of_utc": format_utc_timestamp(decision_as_of),
        "decimal_odds": format(decimal_odds, "f"),
        "model_probability": _number(document, "model_probability"),
        "raw_implied_probability": _number(document, "raw_implied_probability"),
        "normalized_implied_probability": _number(document, "normalized_implied_probability"),
        "overround": _number(document, "overround"),
        "edge": _number(document, "edge"),
        "expected_value": _number(document, "expected_value"),
        "model_artifact_id": _string(document, "model_artifact_id"),
        "model_checksum_sha256": _string(document, "model_checksum_sha256"),
        "model_specification_version": _string(document, "model_specification_version"),
        "feature_artifact_id": _string(document, "feature_artifact_id"),
        "feature_manifest_checksum_sha256": _string(document, "feature_manifest_checksum_sha256"),
        "feature_specification_version": _string(document, "feature_specification_version"),
        "feature_row_id": _string(document, "feature_row_id"),
        "dependency_keys": cast(
            list[JsonValue],
            sorted(_string_array(document.get("dependency_keys", []))),
        ),
        "participant_ids": cast(
            list[JsonValue],
            sorted(_string_array(document.get("participant_ids", []))),
        ),
        "dependency_metadata_complete": _boolean(
            document.get("dependency_metadata_complete", False),
            "dependency_metadata_complete",
        ),
        "dependency_metadata_provenance": (
            ""
            if document.get("dependency_metadata_provenance") in {None, ""}
            else _string(document, "dependency_metadata_provenance")
        ),
        "prediction_quality_passed": _boolean(
            document.get("prediction_quality_passed", False),
            "prediction_quality_passed",
        ),
    }
    opportunity_id = derive_opportunity_id(payload=payload)
    supplied_id = document.get("opportunity_id")
    if type(supplied_id) is str and supplied_id != opportunity_id:
        raise ConfigurationError("opportunity_id does not match canonical identity")
    return Opportunity(
        opportunity_id=opportunity_id,
        canonical_event_id=_string(document, "canonical_event_id"),
        event_start_utc=event_start,
        selection=selection,
        prediction_id=_string(document, "prediction_id"),
        predicted_at_utc=predicted_at,
        model_trained_through_date=_date(document, "model_trained_through_date"),
        model_calibrated_through_date=_date(document, "model_calibrated_through_date"),
        quote_observation_id=_string(document, "quote_observation_id"),
        quote_series_id=quote_series_id,
        quoted_at_utc=quoted_at,
        source_observed_at_utc=source_observed_at,
        source_name=_string(document, "source_name"),
        provider_type=_string(document, "provider_type"),
        provider_id=_string(document, "provider_id"),
        evaluation_mode=evaluation_mode,
        evaluation_version=evaluation_version,
        decimal_odds=decimal_odds,
        model_probability=_number(document, "model_probability"),
        raw_implied_probability=_number(document, "raw_implied_probability"),
        normalized_implied_probability=_number(document, "normalized_implied_probability"),
        overround=_number(document, "overround"),
        edge=_number(document, "edge"),
        expected_value=_number(document, "expected_value"),
        decision_as_of_utc=decision_as_of,
        model_artifact_id=_string(document, "model_artifact_id"),
        model_checksum_sha256=_string(document, "model_checksum_sha256"),
        model_specification_version=_string(document, "model_specification_version"),
        feature_artifact_id=_string(document, "feature_artifact_id"),
        feature_manifest_checksum_sha256=_string(document, "feature_manifest_checksum_sha256"),
        feature_specification_version=_string(document, "feature_specification_version"),
        feature_row_id=_string(document, "feature_row_id"),
        dependency_keys=frozenset(_string_array(document.get("dependency_keys", []))),
        participant_ids=frozenset(_string_array(document.get("participant_ids", []))),
        dependency_metadata_complete=_boolean(
            document.get("dependency_metadata_complete", False),
            "dependency_metadata_complete",
        ),
        dependency_metadata_provenance=(
            ""
            if document.get("dependency_metadata_provenance") in {None, ""}
            else _string(document, "dependency_metadata_provenance")
        ),
        prediction_quality_passed=_boolean(
            document.get("prediction_quality_passed", False),
            "prediction_quality_passed",
        ),
    )


def _filters(document: dict[str, object]) -> OpportunityFilter:
    return OpportunityFilter(
        minimum_probability=_number_default(document, "minimum_probability", 0.0),
        minimum_edge=_number_default(document, "minimum_edge", 0.0),
        minimum_expected_value=_number_default(document, "minimum_expected_value", 0.0),
        selection_minimum_odds=_decimal_default(
            document,
            "selection_minimum_odds",
            "1.0001",
        ),
        selection_maximum_odds=_decimal_default(
            document,
            "selection_maximum_odds",
            "100000",
        ),
        sport_codes=frozenset(_string_array(document.get("sport_codes", []))),
        market_keys=frozenset(_string_array(document.get("market_keys", []))),
        provider_ids=frozenset(_string_array(document.get("provider_ids", []))),
        starts_at_or_after_utc=_optional_datetime(document.get("starts_at_or_after_utc")),
        starts_before_utc=_optional_datetime(document.get("starts_before_utc")),
        include_historical_benchmarks=_boolean(
            document.get("include_historical_benchmarks", False),
            "include_historical_benchmarks",
        ),
        filter_version=str(document.get("filter_version", "opportunity-filter-v1")),
        ranking_mode=_enum(
            OpportunityRankingMode,
            document.get("ranking_mode", OpportunityRankingMode.EXPECTED_VALUE.value),
            "ranking_mode",
        ),
        max_accepted_count=_optional_int(document.get("max_accepted_count")),
    )


def _rules(document: dict[str, object]) -> CombinationRules:
    return CombinationRules(
        minimum_legs=_int_default(document, "minimum_legs", 2),
        maximum_legs=_int_default(document, "maximum_legs", 4),
        selection_minimum_odds=_decimal_default(
            document,
            "selection_minimum_odds",
            "1.0001",
        ),
        selection_maximum_odds=_decimal_default(
            document,
            "selection_maximum_odds",
            "100000",
        ),
        combined_minimum_odds=_decimal_default(
            document,
            "combined_minimum_odds",
            "1.0001",
        ),
        combined_maximum_odds=_decimal_default(
            document,
            "combined_maximum_odds",
            "1000000",
        ),
        allow_unknown_dependencies=_boolean(
            document.get("allow_unknown_dependencies", False),
            "allow_unknown_dependencies",
        ),
        policy_version=str(document.get("policy_version", "combination-policy-v1")),
        allowed_sport_codes=frozenset(_string_array(document.get("allowed_sport_codes", []))),
        allowed_market_keys=frozenset(_string_array(document.get("allowed_market_keys", []))),
        minimum_joint_probability=_number_default(
            document,
            "minimum_joint_probability",
            0.0,
        ),
        minimum_expected_value=_number_default(document, "minimum_expected_value", -1.0),
        maximum_candidates=_int_default(document, "maximum_candidates", 50),
        maximum_evaluated_combinations=_int_default(
            document,
            "maximum_evaluated_combinations",
            10_000,
        ),
        maximum_outputs=_int_default(document, "maximum_outputs", 100),
        maximum_event_horizon=timedelta(
            days=_number_default(document, "maximum_event_horizon_days", 365.0)
        ),
        allow_multiple_sports=_boolean(
            document.get("allow_multiple_sports", True),
            "allow_multiple_sports",
        ),
        allow_multiple_dates=_boolean(
            document.get("allow_multiple_dates", True),
            "allow_multiple_dates",
        ),
    )


def prediction_to_json(item: MarketPrediction) -> dict[str, JsonValue]:
    """Serialize one complete market prediction for CLI output."""
    return _prediction_json(item)


def _prediction_json(item: MarketPrediction) -> dict[str, JsonValue]:
    return {
        "prediction_id": item.prediction_id,
        "schema_version": item.schema_version,
        "canonical_event_id": item.canonical_event_id,
        "event_start_utc": format_utc_timestamp(item.event_start_utc),
        "predicted_at_utc": format_utc_timestamp(item.predicted_at_utc),
        "feature_available_at_utc": format_utc_timestamp(item.feature_available_at_utc),
        "ordered_selection_ids": list(item.ordered_selection_ids),
        "probabilities": [
            {
                "selection_id": value.selection.selection_id,
                "probability": value.probability,
            }
            for value in item.probabilities
        ],
        "production_eligible": item.quality.production_eligible,
    }


def _opportunity_json(item: Opportunity) -> dict[str, JsonValue]:
    return {
        "opportunity_id": item.opportunity_id,
        "canonical_event_id": item.canonical_event_id,
        "event_start_utc": format_utc_timestamp(item.event_start_utc),
        "selection": item.selection.identity_payload(),
        "prediction_id": item.prediction_id,
        "predicted_at_utc": format_utc_timestamp(item.predicted_at_utc),
        "model_trained_through_date": item.model_trained_through_date.isoformat(),
        "model_calibrated_through_date": item.model_calibrated_through_date.isoformat(),
        "quote_observation_id": item.quote_observation_id,
        "quote_series_id": item.quote_series_id,
        "quoted_at_utc": (
            None if item.quoted_at_utc is None else format_utc_timestamp(item.quoted_at_utc)
        ),
        "source_observed_at_utc": format_utc_timestamp(item.source_observed_at_utc),
        "source_name": item.source_name,
        "provider_type": item.provider_type,
        "provider_id": item.provider_id,
        "evaluation_mode": item.evaluation_mode.value,
        "decimal_odds": format(item.decimal_odds, "f"),
        "model_probability": item.model_probability,
        "raw_implied_probability": item.raw_implied_probability,
        "normalized_implied_probability": item.normalized_implied_probability,
        "overround": item.overround,
        "edge": item.edge,
        "expected_value": item.expected_value,
        "model_artifact_id": item.model_artifact_id,
        "model_checksum_sha256": item.model_checksum_sha256,
        "model_specification_version": item.model_specification_version,
        "feature_artifact_id": item.feature_artifact_id,
        "feature_manifest_checksum_sha256": item.feature_manifest_checksum_sha256,
        "feature_specification_version": item.feature_specification_version,
        "feature_row_id": item.feature_row_id,
        "dependency_keys": cast(list[JsonValue], sorted(item.dependency_keys)),
        "participant_ids": cast(list[JsonValue], sorted(item.participant_ids)),
        "dependency_metadata_complete": item.dependency_metadata_complete,
    }


def _combination_json(item: Combination) -> dict[str, JsonValue]:
    return {
        "combination_id": item.combination_id,
        "opportunity_ids": [leg.opportunity_id for leg in item.legs],
        "leg_count": item.leg_count,
        "total_decimal_odds": format(item.total_decimal_odds, "f"),
        "joint_probability": item.joint_probability,
        "expected_value": item.expected_value,
        "earliest_event_start_utc": format_utc_timestamp(item.earliest_event_start_utc),
        "latest_event_start_utc": format_utc_timestamp(item.latest_event_start_utc),
        "common_decision_time_utc": format_utc_timestamp(item.common_information_time_utc),
        "policy_version": item.policy_version,
        "policy_id": item.policy_id,
        "dependencies": [
            {
                "left_opportunity_id": relation.left_opportunity_id,
                "right_opportunity_id": relation.right_opportunity_id,
                "classification": relation.classification.value,
                "reason": relation.reason,
            }
            for relation in item.dependencies
        ],
        "eligible": item.eligible,
        "rejection_reasons": list(item.rejection_reasons),
        "structural_independence_warning": item.structural_independence_warning,
    }


def _mapping(value: object, description: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(type(key) is not str for key in value):
        raise ConfigurationError(f"{description} must be a JSON object")
    return cast(dict[str, object], value)


def _array(
    document: dict[str, object],
    field: str,
    *,
    default: list[object] | None = None,
) -> list[object]:
    value = document.get(field, default)
    if not isinstance(value, list):
        raise ConfigurationError(f"{field} must be a JSON array")
    return cast(list[object], value)


def _string(document: dict[str, object], field: str) -> str:
    value = document.get(field)
    if type(value) is not str or not value:
        raise ConfigurationError(f"{field} must be a non-empty JSON string")
    return value


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    if type(value) is not str or not value:
        raise ConfigurationError("optional string value is malformed")
    return value


def _number(document: dict[str, object], field: str) -> float:
    value = document.get(field)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigurationError(f"{field} must be a JSON number")
    return float(value)


def _number_default(document: dict[str, object], field: str, default: float) -> float:
    return _number({field: document.get(field, default)}, field)


def _datetime(document: dict[str, object], field: str) -> datetime:
    value = document.get(field)
    parsed = _optional_datetime(value)
    if parsed is None:
        raise ConfigurationError(f"{field} must be an ISO UTC timestamp")
    return parsed


def _optional_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if type(value) is not str:
        raise ConfigurationError("timestamp must be an ISO JSON string")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ConfigurationError("timestamp is malformed") from exc


def _date(document: dict[str, object], field: str) -> date:
    value = document.get(field)
    if type(value) is not str:
        raise ConfigurationError(f"{field} must be an ISO date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ConfigurationError(f"{field} is malformed") from exc


def _decimal(document: dict[str, object], field: str) -> Decimal:
    if field not in document:
        raise ConfigurationError(f"{field} is required")
    return _decimal_value(document[field], field)


def _decimal_default(document: dict[str, object], field: str, default: str) -> Decimal:
    return _decimal_value(document.get(field, default), field)


def _decimal_value(value: object, field: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        raise ConfigurationError(f"{field} must be a decimal string or number")
    try:
        return Decimal(str(value))
    except Exception as exc:
        raise ConfigurationError(f"{field} is malformed") from exc


def _boolean(value: object, field: str) -> bool:
    if type(value) is not bool:
        raise ConfigurationError(f"{field} must be a JSON boolean")
    return value


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if type(value) is not int:
        raise ConfigurationError("optional integer value is malformed")
    return value


def _int_default(document: dict[str, object], field: str, default: int) -> int:
    value = document.get(field, default)
    if type(value) is not int:
        raise ConfigurationError(f"{field} must be a JSON integer")
    return value


def _string_array(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or any(type(item) is not str or not item for item in value):
        raise ConfigurationError("string array is malformed")
    return tuple(cast(list[str], value))


def _enum[EnumT: Enum](enum_type: type[EnumT], value: object, field: str) -> EnumT:
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"{field} has an unsupported value") from exc
