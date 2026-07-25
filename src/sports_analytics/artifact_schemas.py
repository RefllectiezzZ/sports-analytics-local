"""Exact versioned schema registry for typed analytical datasets."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Final, cast

from sports_analytics.artifact_strict import (
    require_bool,
    require_canonical_selection_identity,
    require_canonical_utc_timestamp_string,
    require_date_string,
    require_decimal_string,
    require_dict,
    require_finite_number,
    require_list,
    require_positive_int,
    require_probability,
    require_sha256_checksum,
    require_str,
)
from sports_analytics.combinations.contracts import (
    DependencyClass,
    classify_dependency_from_opportunity_rows,
    derive_combination_id,
)
from sports_analytics.core.exceptions import ArtifactError
from sports_analytics.data.types import JsonValue
from sports_analytics.models.identity import content_addressed_id
from sports_analytics.opportunities.identity import (
    OPPORTUNITY_IDENTITY_VERSION,
    VALUE_CALCULATION_TOLERANCE,
    derive_evaluation_id,
    derive_opportunity_id,
)

PREDICTIONS_SCHEMA_VERSION: Final[str] = "predictions-v2"
MARKET_EVALUATIONS_SCHEMA_VERSION: Final[str] = "market-evaluations-v2"
OPPORTUNITY_DECISIONS_SCHEMA_VERSION: Final[str] = "opportunity-decisions-v2"
OPPORTUNITIES_SCHEMA_VERSION: Final[str] = "opportunities-v2"
COMBINATIONS_SCHEMA_VERSION: Final[str] = "combinations-v2"
REJECTIONS_SCHEMA_VERSION: Final[str] = "rejections-v2"
SETTLEMENTS_SCHEMA_VERSION: Final[str] = "settlements-v2"
FOLD_METRICS_SCHEMA_VERSION: Final[str] = "fold-metrics-v2"
AGGREGATE_METRICS_SCHEMA_VERSION: Final[str] = "aggregate-metrics-v2"

REJECTION_KIND_OPPORTUNITY_FILTER = "opportunity-filter"
REJECTION_KIND_COMBINATION_BUILDER = "combination-builder"

DATASET_SCHEMA_VERSIONS: Final[dict[str, str]] = {
    "predictions": PREDICTIONS_SCHEMA_VERSION,
    "market_evaluations": MARKET_EVALUATIONS_SCHEMA_VERSION,
    "opportunity_decisions": OPPORTUNITY_DECISIONS_SCHEMA_VERSION,
    "opportunities": OPPORTUNITIES_SCHEMA_VERSION,
    "combinations": COMBINATIONS_SCHEMA_VERSION,
    "rejections": REJECTIONS_SCHEMA_VERSION,
    "settlements": SETTLEMENTS_SCHEMA_VERSION,
    "fold_metrics": FOLD_METRICS_SCHEMA_VERSION,
    "aggregate_metrics": AGGREGATE_METRICS_SCHEMA_VERSION,
}


@dataclass(frozen=True, slots=True)
class DatasetSchema:
    """One exact dataset schema with required fields and types."""

    name: str
    version: str
    id_field: str
    required_fields: frozenset[str]
    optional_fields: frozenset[str] = frozenset()


SCHEMAS: Final[dict[str, DatasetSchema]] = {
    "predictions": DatasetSchema(
        name="predictions",
        version=PREDICTIONS_SCHEMA_VERSION,
        id_field="prediction_id",
        required_fields=frozenset(
            {
                "prediction_id",
                "schema_version",
                "canonical_event_id",
                "event_start_utc",
                "predicted_at_utc",
                "feature_available_at_utc",
                "provenance",
                "ordered_selection_ids",
                "probabilities",
                "quality",
                "lineage",
            }
        ),
    ),
    "market_evaluations": DatasetSchema(
        name="market_evaluations",
        version=MARKET_EVALUATIONS_SCHEMA_VERSION,
        id_field="evaluation_id",
        required_fields=frozenset(
            {
                "evaluation_id",
                "schema_version",
                "evaluation_version",
                "prediction_id",
                "quote_observation_id",
                "quote_series_id",
                "selection_id",
                "selection",
                "source_name",
                "provider_type",
                "provider_id",
                "evaluation_mode",
                "decimal_odds",
                "model_probability",
                "raw_implied_probability",
                "complete_market_raw_total",
                "overround",
                "normalized_implied_probability",
                "edge",
                "expected_value",
            }
        ),
    ),
    "opportunity_decisions": DatasetSchema(
        name="opportunity_decisions",
        version=OPPORTUNITY_DECISIONS_SCHEMA_VERSION,
        id_field="opportunity_id",
        required_fields=frozenset(
            {
                "opportunity_id",
                "schema_version",
                "filter_config_id",
                "decision_as_of_utc",
                "eligible",
                "rejection_codes",
            }
        ),
        optional_fields=frozenset({"accepted_rank"}),
    ),
    "opportunities": DatasetSchema(
        name="opportunities",
        version=OPPORTUNITIES_SCHEMA_VERSION,
        id_field="opportunity_id",
        required_fields=frozenset(
            {
                "opportunity_id",
                "schema_version",
                "identity_version",
                "evaluation_version",
                "canonical_event_id",
                "event_start_utc",
                "selection",
                "prediction_id",
                "predicted_at_utc",
                "quote_series_id",
                "quote_observation_id",
                "source_name",
                "provider_type",
                "provider_id",
                "evaluation_mode",
                "quoted_at_utc",
                "source_observed_at_utc",
                "decision_as_of_utc",
                "decimal_odds",
                "model_probability",
                "raw_implied_probability",
                "normalized_implied_probability",
                "overround",
                "edge",
                "expected_value",
                "model_artifact_id",
                "model_checksum_sha256",
                "model_specification_version",
                "feature_artifact_id",
                "feature_manifest_checksum_sha256",
                "feature_specification_version",
                "feature_row_id",
                "dependency_keys",
                "participant_ids",
                "dependency_metadata_complete",
                "prediction_quality_passed",
            }
        ),
        optional_fields=frozenset(
            {
                "dependency_metadata_provenance",
                "model_trained_through_date",
                "model_calibrated_through_date",
            }
        ),
    ),
    "combinations": DatasetSchema(
        name="combinations",
        version=COMBINATIONS_SCHEMA_VERSION,
        id_field="combination_id",
        required_fields=frozenset(
            {
                "combination_id",
                "schema_version",
                "policy_id",
                "policy_version",
                "opportunity_ids",
                "dependencies",
                "total_decimal_odds",
                "joint_probability",
                "expected_value",
                "common_decision_time_utc",
                "earliest_event_start_utc",
                "latest_event_start_utc",
                "eligible",
                "rejection_reasons",
            }
        ),
    ),
    "rejections": DatasetSchema(
        name="rejections",
        version=REJECTIONS_SCHEMA_VERSION,
        id_field="rejection_id",
        required_fields=frozenset(
            {
                "rejection_id",
                "schema_version",
                "rejection_kind",
            }
        ),
        optional_fields=frozenset(
            {
                "opportunity_id",
                "filter_config_id",
                "codes",
                "strategy_id",
                "opportunity_ids",
                "rejection_code",
                "reason",
                "policy_id",
                "builder_truncated",
            }
        ),
    ),
    "settlements": DatasetSchema(
        name="settlements",
        version=SETTLEMENTS_SCHEMA_VERSION,
        id_field="bet_id",
        required_fields=frozenset(
            {
                "bet_id",
                "schema_version",
                "fold_id",
                "kind",
                "opportunity_ids",
                "decimal_odds",
                "result",
                "stake_units",
                "returned_units",
                "profit_units",
            }
        ),
        optional_fields=frozenset({"event_start_utc", "strategy_id", "combination_id"}),
    ),
    "fold_metrics": DatasetSchema(
        name="fold_metrics",
        version=FOLD_METRICS_SCHEMA_VERSION,
        id_field="fold_id",
        required_fields=frozenset(
            {
                "fold_id",
                "schema_version",
                "sample_size",
                "accepted_single_count",
                "accepted_combination_count",
                "net_profit_units",
            }
        ),
    ),
    "aggregate_metrics": DatasetSchema(
        name="aggregate_metrics",
        version=AGGREGATE_METRICS_SCHEMA_VERSION,
        id_field="metric_id",
        required_fields=frozenset(
            {
                "metric_id",
                "schema_version",
                "backtest_id",
                "decision_run_id",
                "mode",
                "strategy_id",
                "feature_artifact_id",
                "feature_manifest_checksum_sha256",
                "input_snapshots",
                "random_seed",
                "test_event_count",
                "complete_quote_event_count",
                "quote_coverage",
                "candidate_count",
                "rejection_count",
                "accepted_single_count",
                "accepted_combination_count",
                "bet_count",
                "win_count",
                "loss_count",
                "push_count",
                "void_count",
                "staked_units",
                "returned_units",
                "gross_return_units",
                "net_profit_units",
                "roi",
                "hit_rate",
                "average_decimal_odds",
                "maximum_drawdown_units",
                "cumulative_profit_units",
                "average_model_probability",
                "average_edge",
                "average_expected_value",
                "all_prediction_count",
                "selected_prediction_count",
                "all_log_loss",
                "all_multiclass_brier_score",
                "selected_log_loss",
                "selected_multiclass_brier_score",
                "rejection_counts_by_reason",
                "disclaimer",
            }
        ),
    ),
}


def validate_dataset_row_schema(name: str, row: dict[str, JsonValue], *, version: str) -> None:
    """Validate one row against the exact registered schema."""
    schema = SCHEMAS.get(name)
    if schema is None:
        raise ArtifactError(f"unknown dataset schema: {name}")
    if version != schema.version:
        raise ArtifactError(f"{name} schema version mismatch: expected {schema.version}")
    row_version = row.get("schema_version")
    if row_version is not None and row_version != schema.version:
        raise ArtifactError(f"{name} row schema_version mismatch")
    fields = set(row)
    allowed = schema.required_fields | schema.optional_fields
    if fields - allowed:
        raise ArtifactError(f"{name} row contains unknown fields")
    if schema.required_fields - fields:
        raise ArtifactError(f"{name} row is missing required fields")
    if name == "predictions":
        _validate_prediction_row(row)
    elif name == "market_evaluations":
        _validate_market_evaluation_row(row)
    elif name == "opportunities":
        _validate_opportunity_row(row)
    elif name == "opportunity_decisions":
        _validate_opportunity_decision_row(row)
    elif name == "combinations":
        _validate_combination_row(row)
    elif name == "rejections":
        _validate_rejection_row(row)
    elif name == "settlements":
        _validate_settlement_row(row)
    elif name in {"market_evaluations", "opportunities"}:
        _validate_finite(row.get("expected_value"), "expected_value")
        _validate_finite(row.get("edge"), "edge")


def validate_cross_dataset_integrity(datasets: dict[str, tuple[dict[str, JsonValue], ...]]) -> None:
    """Validate foreign-key relationships and recomputed identities between typed datasets."""
    try:
        _validate_cross_dataset_integrity(datasets)
    except (
        KeyError,
        TypeError,
        ValueError,
        OverflowError,
        InvalidOperation,
        ZeroDivisionError,
    ) as exc:
        raise ArtifactError("cross-dataset integrity validation failed") from exc


def _validate_cross_dataset_integrity(
    datasets: dict[str, tuple[dict[str, JsonValue], ...]],
) -> None:
    predictions = {row["prediction_id"] for row in datasets.get("predictions", ())}
    predictions_by_id: dict[str, dict[str, JsonValue]] = {
        require_str(row.get("prediction_id"), field="prediction_id"): row
        for row in datasets.get("predictions", ())
    }
    opportunities_by_id: dict[str, dict[str, JsonValue]] = {
        require_str(row["opportunity_id"], field="opportunity_id"): row
        for row in datasets.get("opportunities", ())
    }
    decisions = datasets.get("opportunity_decisions", ())
    opportunity_rows = datasets.get("opportunities", ())
    if len(decisions) != len(opportunity_rows):
        raise ArtifactError("opportunity decisions must match opportunity count exactly")
    decision_ids = [
        require_str(row.get("opportunity_id"), field="opportunity_id") for row in decisions
    ]
    if len(decision_ids) != len(set(decision_ids)):
        raise ArtifactError("every opportunity_id must appear exactly once in decisions")
    opportunities = {row["opportunity_id"] for row in opportunity_rows}
    if set(decision_ids) != opportunities:
        raise ArtifactError("opportunity decisions must cover every opportunity exactly once")
    filter_config_ids = {
        require_str(row.get("filter_config_id"), field="filter_config_id") for row in decisions
    }
    if len(filter_config_ids) != 1:
        raise ArtifactError("opportunity decisions must use the same filter_config_id")
    eligible_opportunity_ids = {
        row["opportunity_id"] for row in decisions if row.get("eligible") is True
    }
    for row in datasets.get("market_evaluations", ()):
        if row["prediction_id"] not in predictions:
            raise ArtifactError("orphan market evaluation references missing prediction")
    evaluation_by_key: dict[tuple[str, str, str, str], dict[str, JsonValue]] = {}
    for row in datasets.get("market_evaluations", ()):
        key = (
            require_str(row.get("prediction_id"), field="prediction_id"),
            require_str(row.get("quote_observation_id"), field="quote_observation_id"),
            require_str(row.get("quote_series_id"), field="quote_series_id"),
            require_str(row.get("selection_id"), field="selection_id"),
        )
        if key in evaluation_by_key:
            raise ArtifactError("duplicate market evaluation identity")
        evaluation_by_key[key] = row
    for _opportunity_id, opportunity_row in opportunities_by_id.items():
        prediction_id = require_str(
            opportunity_row.get("prediction_id"),
            field="prediction_id",
        )
        if prediction_id not in predictions:
            raise ArtifactError("orphan opportunity references missing prediction")
        selection = require_canonical_selection_identity(
            opportunity_row.get("selection"),
            field="selection",
        )
        evaluation_key = (
            prediction_id,
            require_str(opportunity_row.get("quote_observation_id"), field="quote_observation_id"),
            require_str(opportunity_row.get("quote_series_id"), field="quote_series_id"),
            selection.selection_id,
        )
        if evaluation_key not in evaluation_by_key:
            raise ArtifactError("opportunity has no matching market evaluation")
        _validate_opportunity_semantic_linkage(
            opportunity_row=opportunity_row,
            prediction_row=predictions_by_id[prediction_id],
            evaluation_row=evaluation_by_key[evaluation_key],
        )
    for row in datasets.get("opportunity_decisions", ()):
        if row["opportunity_id"] not in opportunities:
            raise ArtifactError("orphan opportunity decision references missing opportunity")
        eligible = row.get("eligible")
        codes = row.get("rejection_codes")
        if eligible is True and isinstance(codes, list) and codes:
            raise ArtifactError("eligible decision cannot contain rejection codes")
        if eligible is False and isinstance(codes, list) and not codes:
            raise ArtifactError("rejected decision requires at least one rejection code")
        if eligible is True and row.get("accepted_rank") is None:
            raise ArtifactError("eligible decision requires accepted_rank")
        if eligible is False and row.get("accepted_rank") is not None:
            raise ArtifactError("rejected decision cannot include accepted_rank")
        if row.get("accepted_rank") is not None:
            require_positive_int(row.get("accepted_rank"), field="accepted_rank")
    _validate_decision_ranks(datasets.get("opportunity_decisions", ()))
    combinations_by_id = {row["combination_id"]: row for row in datasets.get("combinations", ())}
    for row in datasets.get("combinations", ()):
        opportunity_ids = require_list(row.get("opportunity_ids"), field="opportunity_ids")
        ordered_ids = [require_str(item, field="opportunity_id") for item in opportunity_ids]
        if ordered_ids != sorted(set(ordered_ids)):
            raise ArtifactError("combination opportunity_ids must be ordered and unique")
        for opportunity_id in ordered_ids:
            if opportunity_id not in opportunities:
                raise ArtifactError("orphan combination references missing opportunity")
            if opportunity_id not in eligible_opportunity_ids:
                raise ArtifactError("combination references ineligible opportunity")
        _validate_combination_against_opportunities(
            row,
            opportunity_ids=ordered_ids,
            opportunities_by_id=opportunities_by_id,
        )
    for row in datasets.get("rejections", ()):
        kind = row.get("rejection_kind")
        if kind == REJECTION_KIND_OPPORTUNITY_FILTER:
            if row.get("opportunity_id") not in opportunities:
                raise ArtifactError("orphan rejection references missing opportunity")
        elif kind == REJECTION_KIND_COMBINATION_BUILDER:
            opportunity_ids_raw = row.get("opportunity_ids", [])
            if not isinstance(opportunity_ids_raw, list):
                raise ArtifactError("combination rejection opportunity_ids must be a list")
            for raw_opportunity_id in opportunity_ids_raw:
                opportunity_id = require_str(raw_opportunity_id, field="opportunity_id")
                if opportunity_id not in opportunities:
                    raise ArtifactError(
                        "orphan combination rejection references missing opportunity"
                    )
        else:
            raise ArtifactError("rejection_kind is unsupported")
    for row in datasets.get("settlements", ()):
        opportunity_ids = require_list(row.get("opportunity_ids"), field="opportunity_ids")
        settlement_opportunity_ids = [
            require_str(item, field="opportunity_id") for item in opportunity_ids
        ]
        for opportunity_id in settlement_opportunity_ids:
            if opportunity_id not in opportunities:
                raise ArtifactError("orphan settlement references missing opportunity")
        kind = require_str(row.get("kind"), field="kind")
        if kind == "single":
            if len(settlement_opportunity_ids) != 1:
                raise ArtifactError("single settlement must reference exactly one opportunity")
            if settlement_opportunity_ids[0] not in eligible_opportunity_ids:
                raise ArtifactError("single settlement references ineligible opportunity")
        elif kind == "combination":
            combination_id = require_str(row.get("combination_id"), field="combination_id")
            if combination_id not in combinations_by_id:
                raise ArtifactError("combination settlement references missing combination")
            combination_row = combinations_by_id[combination_id]
            combination_opportunity_ids = require_list(
                combination_row.get("opportunity_ids"),
                field="opportunity_ids",
            )
            declared_ids = [
                require_str(item, field="opportunity_id") for item in combination_opportunity_ids
            ]
            if settlement_opportunity_ids != declared_ids:
                raise ArtifactError(
                    "combination settlement legs do not match persisted combination"
                )
        else:
            raise ArtifactError("settlement kind is unsupported")


def _validate_prediction_row(row: dict[str, JsonValue]) -> None:
    try:
        probabilities = require_list(row.get("probabilities"), field="probabilities")
        ordered = require_list(row.get("ordered_selection_ids"), field="ordered_selection_ids")
        values: list[float] = []
        ids: list[str] = []
        for index, item in enumerate(probabilities):
            probability_item = require_dict(item, field=f"probabilities[{index}]")
            selection_id = require_str(
                probability_item.get("selection_id"),
                field=f"probabilities[{index}].selection_id",
            )
            selection = require_canonical_selection_identity(
                probability_item.get("selection"),
                field=f"probabilities[{index}].selection",
            )
            if selection_id != selection.selection_id:
                raise ArtifactError("prediction selection_id does not match selection payload")
            probability = require_probability(
                probability_item.get("probability"),
                field=f"probabilities[{index}].probability",
            )
            ids.append(selection_id)
            values.append(probability)
        ordered_ids = [require_str(item, field="ordered_selection_ids[]") for item in ordered]
        if ids != ordered_ids:
            raise ArtifactError("prediction ordered selection ids mismatch")
        if abs(math.fsum(values) - 1.0) > 1e-9:
            raise ArtifactError("prediction probabilities must sum to one")
        provenance = require_str(row.get("provenance"), field="provenance")
        if provenance not in {"historical-replay", "synthetic-contract"}:
            raise ArtifactError("prediction provenance is unsupported")
        lineage = require_dict(row.get("lineage"), field="lineage")
        for field in (
            "model_artifact_id",
            "model_checksum_sha256",
            "model_specification_version",
            "feature_artifact_id",
            "feature_manifest_checksum_sha256",
            "feature_specification_version",
            "feature_row_id",
        ):
            require_str(lineage.get(field), field=f"lineage.{field}")
        require_sha256_checksum(
            lineage.get("model_checksum_sha256"),
            field="lineage.model_checksum_sha256",
        )
        require_sha256_checksum(
            lineage.get("feature_manifest_checksum_sha256"),
            field="lineage.feature_manifest_checksum_sha256",
        )
        if lineage["feature_row_id"] != row["canonical_event_id"]:
            raise ArtifactError("prediction feature_row_id must match canonical_event_id")
        require_canonical_utc_timestamp_string(
            row.get("event_start_utc"),
            field="event_start_utc",
        )
        require_canonical_utc_timestamp_string(
            row.get("predicted_at_utc"),
            field="predicted_at_utc",
        )
        require_canonical_utc_timestamp_string(
            row.get("feature_available_at_utc"),
            field="feature_available_at_utc",
        )
        quality = require_dict(row.get("quality"), field="quality")
        for quality_field in (
            "calibrated",
            "model_artifact_verified",
            "feature_artifact_verified",
            "sufficient_history",
            "data_quality_passed",
        ):
            require_bool(quality.get(quality_field), field=f"quality.{quality_field}")
        expected_id = _recompute_prediction_id(row)
        if row["prediction_id"] != expected_id:
            raise ArtifactError("prediction_id does not match canonical identity")
    except (
        KeyError,
        TypeError,
        ValueError,
        OverflowError,
        InvalidOperation,
        ZeroDivisionError,
    ) as exc:
        raise ArtifactError("prediction row validation failed") from exc


def _validate_market_evaluation_row(row: dict[str, JsonValue]) -> None:
    expected_id = derive_evaluation_id(
        evaluation_version=require_str(row.get("evaluation_version"), field="evaluation_version"),
        prediction_id=require_str(row.get("prediction_id"), field="prediction_id"),
        quote_observation_id=require_str(
            row.get("quote_observation_id"),
            field="quote_observation_id",
        ),
        quote_series_id=require_str(row.get("quote_series_id"), field="quote_series_id"),
        selection_id=require_str(row.get("selection_id"), field="selection_id"),
        source_name=require_str(row.get("source_name"), field="source_name"),
        provider_type=require_str(row.get("provider_type"), field="provider_type"),
        provider_id=require_str(row.get("provider_id"), field="provider_id"),
        evaluation_mode=require_str(row.get("evaluation_mode"), field="evaluation_mode"),
        decimal_odds=require_str(row.get("decimal_odds"), field="decimal_odds"),
        model_probability=require_finite_number(
            row.get("model_probability"),
            field="model_probability",
        ),
        raw_implied_probability=require_finite_number(
            row.get("raw_implied_probability"),
            field="raw_implied_probability",
        ),
        complete_market_raw_total=require_finite_number(
            row.get("complete_market_raw_total"),
            field="complete_market_raw_total",
        ),
        overround=require_finite_number(row.get("overround"), field="overround"),
        normalized_implied_probability=require_finite_number(
            row.get("normalized_implied_probability"),
            field="normalized_implied_probability",
        ),
        edge=require_finite_number(row.get("edge"), field="edge"),
        expected_value=require_finite_number(row.get("expected_value"), field="expected_value"),
    )
    if row["evaluation_id"] != expected_id:
        raise ArtifactError("evaluation_id does not match canonical identity")
    overround = require_finite_number(row.get("overround"), field="overround")
    if overround < 0.0:
        raise ArtifactError("overround must be finite and non-negative")
    complete_total = require_finite_number(
        row.get("complete_market_raw_total"),
        field="complete_market_raw_total",
    )
    if complete_total <= 0.0:
        raise ArtifactError("complete_market_raw_total must be positive")
    if abs(complete_total - (1.0 + overround)) > VALUE_CALCULATION_TOLERANCE:
        raise ArtifactError("complete_market_raw_total is inconsistent with overround")
    odds = require_decimal_string(row.get("decimal_odds"), field="decimal_odds")
    odds_f = float(odds)
    if not math.isfinite(odds_f) or odds_f <= 1.0:
        raise ArtifactError("decimal_odds must be finite and >1")
    raw = require_finite_number(
        row.get("raw_implied_probability"),
        field="raw_implied_probability",
    )
    normalized = require_finite_number(
        row.get("normalized_implied_probability"),
        field="normalized_implied_probability",
    )
    model_prob = require_probability(row.get("model_probability"), field="model_probability")
    edge = require_finite_number(row.get("edge"), field="edge")
    expected_value = require_finite_number(row.get("expected_value"), field="expected_value")
    if normalized <= 0.0 or normalized > 1.0:
        raise ArtifactError("normalized implied probability must be in (0, 1]")
    expected_raw = 1.0 / odds_f
    if abs(raw - expected_raw) > VALUE_CALCULATION_TOLERANCE:
        raise ArtifactError("raw implied probability does not match decimal odds")
    expected_normalized = expected_raw / complete_total
    if abs(normalized - expected_normalized) > VALUE_CALCULATION_TOLERANCE:
        raise ArtifactError("normalized implied probability is inconsistent")
    expected_edge = model_prob - expected_normalized
    if abs(edge - expected_edge) > VALUE_CALCULATION_TOLERANCE:
        raise ArtifactError("edge is inconsistent")
    expected_ev = model_prob * odds_f - 1.0
    if abs(expected_value - expected_ev) > VALUE_CALCULATION_TOLERANCE:
        raise ArtifactError("expected_value is inconsistent")


def _validate_opportunity_row(row: dict[str, JsonValue]) -> None:
    try:
        if row.get("identity_version") != OPPORTUNITY_IDENTITY_VERSION:
            raise ArtifactError("opportunity identity_version is unsupported")
        non_identity_fields = {
            "opportunity_id",
            "schema_version",
            "model_trained_through_date",
            "model_calibrated_through_date",
        }
        payload = {key: row[key] for key in row if key not in non_identity_fields}
        expected_id = derive_opportunity_id(payload=payload)
        if row["opportunity_id"] != expected_id:
            raise ArtifactError("opportunity_id does not match canonical identity")
        for field in (
            "model_artifact_id",
            "model_checksum_sha256",
            "model_specification_version",
            "feature_artifact_id",
            "feature_manifest_checksum_sha256",
            "feature_specification_version",
            "feature_row_id",
        ):
            require_str(row.get(field), field=field)
        require_sha256_checksum(row.get("model_checksum_sha256"), field="model_checksum_sha256")
        require_sha256_checksum(
            row.get("feature_manifest_checksum_sha256"),
            field="feature_manifest_checksum_sha256",
        )
        if row["feature_row_id"] != row["canonical_event_id"]:
            raise ArtifactError("opportunity feature_row_id must match canonical_event_id")
        require_bool(row.get("prediction_quality_passed"), field="prediction_quality_passed")
        require_bool(
            row.get("dependency_metadata_complete"),
            field="dependency_metadata_complete",
        )
        provenance = row.get("dependency_metadata_provenance")
        if provenance not in {None, ""}:
            require_str(provenance, field="dependency_metadata_provenance")
            if provenance not in {"synthetic-contract"}:
                raise ArtifactError("dependency_metadata_provenance is unsupported")
        if row.get("model_trained_through_date") is not None:
            require_date_string(
                row.get("model_trained_through_date"),
                field="model_trained_through_date",
            )
        if row.get("model_calibrated_through_date") is not None:
            require_date_string(
                row.get("model_calibrated_through_date"),
                field="model_calibrated_through_date",
            )
        require_canonical_utc_timestamp_string(
            row.get("event_start_utc"),
            field="event_start_utc",
        )
        require_canonical_utc_timestamp_string(
            row.get("predicted_at_utc"),
            field="predicted_at_utc",
        )
        require_canonical_utc_timestamp_string(
            row.get("source_observed_at_utc"),
            field="source_observed_at_utc",
        )
        require_canonical_utc_timestamp_string(
            row.get("decision_as_of_utc"),
            field="decision_as_of_utc",
        )
        quoted_at = row.get("quoted_at_utc")
        if quoted_at is not None:
            require_canonical_utc_timestamp_string(quoted_at, field="quoted_at_utc")
        require_canonical_selection_identity(row.get("selection"), field="selection")
        dependency_keys = require_list(row.get("dependency_keys"), field="dependency_keys")
        participant_ids = require_list(row.get("participant_ids"), field="participant_ids")
        for index, item in enumerate(dependency_keys):
            require_str(item, field=f"dependency_keys[{index}]")
        for index, item in enumerate(participant_ids):
            require_str(item, field=f"participant_ids[{index}]")
        overround = require_finite_number(row.get("overround"), field="overround")
        if overround < 0.0:
            raise ArtifactError("overround must be non-negative")
        odds = require_decimal_string(row.get("decimal_odds"), field="decimal_odds")
        odds_f = float(odds)
        if not math.isfinite(odds_f) or odds_f <= 1.0:
            raise ArtifactError("decimal_odds must be finite and >1")
        complete_total = 1.0 + overround
        raw = require_finite_number(
            row.get("raw_implied_probability"),
            field="raw_implied_probability",
        )
        normalized = require_finite_number(
            row.get("normalized_implied_probability"),
            field="normalized_implied_probability",
        )
        model_probability = require_probability(
            row.get("model_probability"),
            field="model_probability",
        )
        edge = require_finite_number(row.get("edge"), field="edge")
        expected_value = require_finite_number(row.get("expected_value"), field="expected_value")
        if normalized <= 0.0 or normalized > 1.0:
            raise ArtifactError("normalized implied probability must be in (0, 1]")
        expected_raw = 1.0 / odds_f
        if abs(raw - expected_raw) > VALUE_CALCULATION_TOLERANCE:
            raise ArtifactError("opportunity raw implied probability is inconsistent")
        if abs(normalized - raw / complete_total) > VALUE_CALCULATION_TOLERANCE:
            raise ArtifactError("opportunity normalized implied probability is inconsistent")
        expected_edge = model_probability - normalized
        if abs(edge - expected_edge) > VALUE_CALCULATION_TOLERANCE:
            raise ArtifactError("edge is inconsistent")
        expected_ev = model_probability * odds_f - 1.0
        if abs(expected_value - expected_ev) > VALUE_CALCULATION_TOLERANCE:
            raise ArtifactError("expected_value is inconsistent")
        require_str(row.get("evaluation_version"), field="evaluation_version")
        _validate_opportunity_timing_semantics(row)
    except (
        KeyError,
        TypeError,
        ValueError,
        OverflowError,
        InvalidOperation,
        ZeroDivisionError,
    ) as exc:
        raise ArtifactError("opportunity row validation failed") from exc


def _validate_opportunity_decision_row(row: dict[str, JsonValue]) -> None:
    if type(row.get("eligible")) is not bool:
        raise ArtifactError("eligible must be a boolean")
    codes = row.get("rejection_codes")
    if not isinstance(codes, list) or any(type(item) is not str for item in codes):
        raise ArtifactError("rejection_codes must be a string list")
    eligible = row.get("eligible")
    if eligible is True and row.get("accepted_rank") is None:
        raise ArtifactError("eligible decision requires accepted_rank")
    if eligible is False and row.get("accepted_rank") is not None:
        raise ArtifactError("rejected decision cannot include accepted_rank")
    if row.get("accepted_rank") is not None:
        require_positive_int(row.get("accepted_rank"), field="accepted_rank")


def _validate_combination_row(row: dict[str, JsonValue]) -> None:
    try:
        joint = require_probability(row.get("joint_probability"), field="joint_probability")
        expected_value = require_finite_number(row.get("expected_value"), field="expected_value")
        odds = require_decimal_string(row.get("total_decimal_odds"), field="total_decimal_odds")
        odds_f = float(odds)
        if not math.isfinite(odds_f) or odds_f <= 1.0:
            raise ArtifactError("combination total_decimal_odds must be >1")
        calculated = joint * odds_f - 1.0
        if abs(calculated - expected_value) > 1e-9:
            raise ArtifactError("combination expected_value is inconsistent")
        dependencies = require_list(row.get("dependencies"), field="dependencies")
        opportunity_ids = require_list(row.get("opportunity_ids"), field="opportunity_ids")
        ordered_ids = [require_str(item, field="opportunity_id") for item in opportunity_ids]
        if ordered_ids != sorted(set(ordered_ids)):
            raise ArtifactError("combination opportunity_ids must be ordered and unique")
        pairs_seen: set[tuple[str, str]] = set()
        for index, item in enumerate(dependencies):
            dependency = require_dict(item, field=f"dependencies[{index}]")
            left = require_str(
                dependency.get("left_opportunity_id"),
                field="left_opportunity_id",
            )
            right = require_str(
                dependency.get("right_opportunity_id"),
                field="right_opportunity_id",
            )
            pair = cast(tuple[str, str], tuple(sorted((left, right))))
            if pair in pairs_seen:
                raise ArtifactError("combination dependency pair is duplicated")
            if pair[0] not in ordered_ids or pair[1] not in ordered_ids:
                raise ArtifactError("combination dependency pair references undeclared leg")
            pairs_seen.add(pair)
        expected_pairs = len(ordered_ids) * (len(ordered_ids) - 1) // 2
        if len(pairs_seen) != expected_pairs:
            raise ArtifactError("combination must contain every dependency pair exactly once")
        common_decision_time = require_canonical_utc_timestamp_string(
            row.get("common_decision_time_utc"),
            field="common_decision_time_utc",
        )
        earliest_start = require_canonical_utc_timestamp_string(
            row.get("earliest_event_start_utc"),
            field="earliest_event_start_utc",
        )
        require_canonical_utc_timestamp_string(
            row.get("latest_event_start_utc"),
            field="latest_event_start_utc",
        )
        if common_decision_time >= earliest_start:
            raise ArtifactError("common decision time must be strictly before earliest event start")
        policy_id = require_str(row.get("policy_id"), field="policy_id")
        expected_id = derive_combination_id(
            opportunity_ids=ordered_ids,
            combined_decimal_odds=format(odds, "f"),
            joint_probability=joint,
            expected_value=expected_value,
            common_information_time_utc=require_str(
                row.get("common_decision_time_utc"),
                field="common_decision_time_utc",
            ),
            policy_id=policy_id,
        )
        if row["combination_id"] != expected_id:
            raise ArtifactError("combination_id does not match canonical identity")
    except (
        KeyError,
        TypeError,
        ValueError,
        OverflowError,
        InvalidOperation,
        ZeroDivisionError,
    ) as exc:
        raise ArtifactError("combination row validation failed") from exc


def _validate_rejection_row(row: dict[str, JsonValue]) -> None:
    kind = row.get("rejection_kind")
    if kind == REJECTION_KIND_OPPORTUNITY_FILTER:
        for field in ("opportunity_id", "filter_config_id", "codes"):
            if field not in row:
                raise ArtifactError(f"opportunity-filter rejection missing {field}")
        codes = row.get("codes")
        if not isinstance(codes, list) or not codes:
            raise ArtifactError("opportunity-filter rejection codes must be non-empty")
        expected_id = content_addressed_id(
            identity_type="opportunity-filter-rejection-v1",
            payload={
                "rejection_kind": REJECTION_KIND_OPPORTUNITY_FILTER,
                "opportunity_id": row["opportunity_id"],
                "filter_config_id": row["filter_config_id"],
                "codes": codes,
                **({"strategy_id": row["strategy_id"]} if "strategy_id" in row else {}),
            },
        )
    elif kind == REJECTION_KIND_COMBINATION_BUILDER:
        for field in ("opportunity_ids", "rejection_code", "reason", "policy_id"):
            if field not in row:
                raise ArtifactError(f"combination-builder rejection missing {field}")
        expected_id = content_addressed_id(
            identity_type="combination-builder-rejection-v1",
            payload={
                "rejection_kind": REJECTION_KIND_COMBINATION_BUILDER,
                "opportunity_ids": row["opportunity_ids"],
                "rejection_code": row["rejection_code"],
                "reason": row["reason"],
                "policy_id": row["policy_id"],
                "builder_truncated": row.get("builder_truncated", False),
            },
        )
    else:
        raise ArtifactError("rejection_kind is unsupported")
    if row["rejection_id"] != expected_id:
        raise ArtifactError("rejection_id does not match canonical identity")


def _validate_settlement_row(row: dict[str, JsonValue]) -> None:
    if row.get("result") not in {"win", "loss"}:
        raise ArtifactError("settlement result must be win or loss")
    if row.get("stake_units") != "1":
        raise ArtifactError("flat stake must be exactly one unit")
    profit = require_decimal_string(row.get("profit_units"), field="profit_units")
    returned = require_decimal_string(row.get("returned_units"), field="returned_units")
    odds = require_decimal_string(row.get("decimal_odds"), field="decimal_odds")
    expected_returned = odds if row.get("result") == "win" else Decimal("0")
    if abs(returned - expected_returned) > Decimal("0.000000001"):
        raise ArtifactError("settlement returned_units is inconsistent")
    if abs(returned - (profit + Decimal("1"))) > Decimal("0.000000001"):
        raise ArtifactError("settlement return/profit is inconsistent")
    expected_bet_id = _recompute_settlement_bet_id(row)
    if row["bet_id"] != expected_bet_id:
        raise ArtifactError("bet_id does not match canonical identity")


def _recompute_settlement_bet_id(row: dict[str, JsonValue]) -> str:
    kind = require_str(row.get("kind"), field="kind")
    fold_id = require_str(row.get("fold_id"), field="fold_id")
    opportunity_ids = row.get("opportunity_ids")
    if not isinstance(opportunity_ids, list) or not opportunity_ids:
        raise ArtifactError("settlement opportunity_ids must be a non-empty list")
    strategy_id = row.get("strategy_id")
    if kind == "single":
        if len(opportunity_ids) != 1:
            raise ArtifactError("single settlement must reference exactly one opportunity")
        if type(strategy_id) is not str or not strategy_id:
            raise ArtifactError("single settlement requires strategy_id")
        return content_addressed_id(
            identity_type="backtest-single-v1",
            payload={
                "strategy_id": strategy_id,
                "fold_id": fold_id,
                "opportunity_id": opportunity_ids[0],
            },
        )
    if kind == "combination":
        combination_id = row.get("combination_id")
        if type(strategy_id) is not str or not strategy_id:
            raise ArtifactError("combination settlement requires strategy_id")
        if type(combination_id) is not str or not combination_id:
            raise ArtifactError("combination settlement requires combination_id")
        return content_addressed_id(
            identity_type="backtest-combination-v1",
            payload={
                "strategy_id": strategy_id,
                "fold_id": fold_id,
                "combination_id": combination_id,
            },
        )
    raise ArtifactError("settlement kind is unsupported")


def _validate_decision_ranks(rows: tuple[dict[str, JsonValue], ...]) -> None:
    by_filter: dict[str, list[dict[str, JsonValue]]] = {}
    for row in rows:
        filter_id = row.get("filter_config_id")
        if type(filter_id) is not str:
            raise ArtifactError("filter_config_id must be a string")
        by_filter.setdefault(filter_id, []).append(row)
    for filter_rows in by_filter.values():
        opportunity_ids = [
            require_str(row.get("opportunity_id"), field="opportunity_id") for row in filter_rows
        ]
        if len(opportunity_ids) != len(set(opportunity_ids)):
            raise ArtifactError("duplicate opportunity decision under one filter configuration")
        ranks = []
        for row in filter_rows:
            if row.get("eligible") is True:
                ranks.append(require_positive_int(row.get("accepted_rank"), field="accepted_rank"))
        if not ranks:
            continue
        ranks_sorted = sorted(ranks)
        if ranks_sorted != list(range(1, len(ranks_sorted) + 1)):
            raise ArtifactError("accepted ranks must be unique and contiguous")


def _validate_combination_against_opportunities(
    row: dict[str, JsonValue],
    *,
    opportunity_ids: list[str],
    opportunities_by_id: dict[str, dict[str, JsonValue]],
) -> None:
    legs = [opportunities_by_id[opportunity_id] for opportunity_id in opportunity_ids]
    combined_odds = Decimal("1")
    joint_probability = 1.0
    earliest_start = None
    latest_start = None
    common_decision_time = None
    for leg in legs:
        odds = require_decimal_string(leg.get("decimal_odds"), field="decimal_odds")
        combined_odds *= odds
        joint_probability *= require_probability(
            leg.get("model_probability"),
            field="model_probability",
        )
        event_start = require_canonical_utc_timestamp_string(
            leg.get("event_start_utc"),
            field="event_start_utc",
        )
        decision_time = require_canonical_utc_timestamp_string(
            leg.get("decision_as_of_utc"),
            field="decision_as_of_utc",
        )
        earliest_start = event_start if earliest_start is None else min(earliest_start, event_start)
        latest_start = event_start if latest_start is None else max(latest_start, event_start)
        common_decision_time = (
            decision_time
            if common_decision_time is None
            else max(common_decision_time, decision_time)
        )
    assert earliest_start is not None
    assert latest_start is not None
    assert common_decision_time is not None
    if common_decision_time >= earliest_start:
        raise ArtifactError("common decision time must be strictly before earliest event start")
    declared_odds = require_decimal_string(
        row.get("total_decimal_odds"),
        field="total_decimal_odds",
    )
    if format(declared_odds, "f") != format(combined_odds, "f"):
        raise ArtifactError("combination total_decimal_odds is inconsistent with leg odds")
    declared_joint = require_probability(row.get("joint_probability"), field="joint_probability")
    if abs(declared_joint - joint_probability) > 1e-9:
        raise ArtifactError("combination joint_probability is inconsistent with leg probabilities")
    declared_expected_value = require_finite_number(
        row.get("expected_value"),
        field="expected_value",
    )
    expected_value = joint_probability * float(combined_odds) - 1.0
    if abs(declared_expected_value - expected_value) > 1e-9:
        raise ArtifactError("combination expected_value is inconsistent with leg values")
    from sports_analytics.data.codec import format_utc_timestamp

    if require_str(
        row.get("earliest_event_start_utc"),
        field="earliest_event_start_utc",
    ) != format_utc_timestamp(earliest_start):
        raise ArtifactError("combination earliest_event_start_utc is inconsistent")
    if require_str(
        row.get("latest_event_start_utc"),
        field="latest_event_start_utc",
    ) != format_utc_timestamp(latest_start):
        raise ArtifactError("combination latest_event_start_utc is inconsistent")
    if require_str(
        row.get("common_decision_time_utc"),
        field="common_decision_time_utc",
    ) != format_utc_timestamp(common_decision_time):
        raise ArtifactError("combination common_decision_time_utc is inconsistent")
    expected_id = derive_combination_id(
        opportunity_ids=opportunity_ids,
        combined_decimal_odds=format(combined_odds, "f"),
        joint_probability=joint_probability,
        expected_value=expected_value,
        common_information_time_utc=format_utc_timestamp(common_decision_time),
        policy_id=require_str(row.get("policy_id"), field="policy_id"),
    )
    if row["combination_id"] != expected_id:
        raise ArtifactError("combination_id does not match recomputed leg values")
    _validate_combination_dependencies(
        row,
        opportunity_ids=opportunity_ids,
        opportunities_by_id=opportunities_by_id,
    )


def _validate_combination_dependencies(
    row: dict[str, JsonValue],
    *,
    opportunity_ids: list[str],
    opportunities_by_id: dict[str, dict[str, JsonValue]],
) -> None:
    dependencies = require_list(row.get("dependencies"), field="dependencies")
    pairs_seen: set[tuple[str, str]] = set()
    for index, item in enumerate(dependencies):
        dependency = require_dict(item, field=f"dependencies[{index}]")
        left_id = require_str(dependency.get("left_opportunity_id"), field="left_opportunity_id")
        right_id = require_str(dependency.get("right_opportunity_id"), field="right_opportunity_id")
        pair = cast(tuple[str, str], tuple(sorted((left_id, right_id))))
        if pair in pairs_seen:
            raise ArtifactError("combination dependency pair is duplicated")
        pairs_seen.add(pair)
        recomputed = classify_dependency_from_opportunity_rows(
            opportunities_by_id[left_id],
            opportunities_by_id[right_id],
        )
        if recomputed.classification is not DependencyClass.STRUCTURALLY_SEPARATE:
            raise ArtifactError("combination dependency pair is not structurally separate")
        if dependency.get("classification") != recomputed.classification.value:
            raise ArtifactError("combination dependency classification does not match policy")
        if dependency.get("reason") != recomputed.reason:
            raise ArtifactError("combination dependency reason does not match policy")
        if (
            dependency.get("left_opportunity_id") != recomputed.left_opportunity_id
            or dependency.get("right_opportunity_id") != recomputed.right_opportunity_id
        ):
            raise ArtifactError("combination dependency opportunity ids are misordered")
    expected_pairs = len(opportunity_ids) * (len(opportunity_ids) - 1) // 2
    if len(pairs_seen) != expected_pairs:
        raise ArtifactError("combination must contain every dependency pair exactly once")


def _validate_opportunity_semantic_linkage(
    *,
    opportunity_row: dict[str, JsonValue],
    prediction_row: dict[str, JsonValue],
    evaluation_row: dict[str, JsonValue],
) -> None:
    _validate_opportunity_timing_semantics(opportunity_row)
    selection = require_canonical_selection_identity(
        opportunity_row.get("selection"),
        field="selection",
    )
    evaluation_selection = require_canonical_selection_identity(
        evaluation_row.get("selection"),
        field="evaluation.selection",
    )
    if selection.selection_id != require_str(
        evaluation_row.get("selection_id"),
        field="selection_id",
    ):
        raise ArtifactError("opportunity selection_id does not match market evaluation")
    if selection.identity_payload() != evaluation_selection.identity_payload():
        raise ArtifactError("opportunity selection does not match market evaluation")
    prediction_probability = _prediction_probability_for_selection(
        prediction_row,
        selection.selection_id,
    )
    opportunity_probability = require_probability(
        opportunity_row.get("model_probability"),
        field="model_probability",
    )
    if abs(prediction_probability - opportunity_probability) > VALUE_CALCULATION_TOLERANCE:
        raise ArtifactError("opportunity model probability does not match prediction")
    _require_matching_field(
        opportunity_row,
        prediction_row,
        field="canonical_event_id",
        label="canonical_event_id",
    )
    _require_matching_timestamp(
        opportunity_row,
        prediction_row,
        field="event_start_utc",
        label="event_start_utc",
    )
    _require_matching_timestamp(
        opportunity_row,
        prediction_row,
        field="predicted_at_utc",
        label="predicted_at_utc",
    )
    lineage = require_dict(prediction_row.get("lineage"), field="lineage")
    for field in (
        "model_artifact_id",
        "model_checksum_sha256",
        "model_specification_version",
        "feature_artifact_id",
        "feature_manifest_checksum_sha256",
        "feature_specification_version",
        "feature_row_id",
    ):
        _require_matching_field(
            opportunity_row,
            lineage,
            field=field,
            label=field,
        )
    if opportunity_row.get("model_trained_through_date") is not None:
        _require_matching_field(
            opportunity_row,
            lineage,
            field="trained_through_date",
            label="model_trained_through_date",
            left_field="model_trained_through_date",
        )
    if opportunity_row.get("model_calibrated_through_date") is not None:
        _require_matching_field(
            opportunity_row,
            lineage,
            field="calibrated_through_date",
            label="model_calibrated_through_date",
            left_field="model_calibrated_through_date",
        )
    _require_matching_field(
        opportunity_row,
        evaluation_row,
        field="prediction_id",
        label="prediction_id",
    )
    _require_matching_field(
        opportunity_row,
        evaluation_row,
        field="quote_observation_id",
        label="quote_observation_id",
    )
    _require_matching_field(
        opportunity_row,
        evaluation_row,
        field="quote_series_id",
        label="quote_series_id",
    )
    _require_matching_field(
        opportunity_row,
        evaluation_row,
        field="source_name",
        label="source_name",
    )
    _require_matching_field(
        opportunity_row,
        evaluation_row,
        field="provider_type",
        label="provider_type",
    )
    _require_matching_field(
        opportunity_row,
        evaluation_row,
        field="provider_id",
        label="provider_id",
    )
    _require_matching_field(
        opportunity_row,
        evaluation_row,
        field="evaluation_mode",
        label="evaluation_mode",
    )
    _require_matching_decimal(
        opportunity_row,
        evaluation_row,
        field="decimal_odds",
        label="decimal_odds",
    )
    for field in (
        "model_probability",
        "raw_implied_probability",
        "normalized_implied_probability",
        "overround",
        "edge",
        "expected_value",
    ):
        _require_matching_number(
            opportunity_row,
            evaluation_row,
            field=field,
            label=field,
        )
    evaluation_overround = require_finite_number(
        evaluation_row.get("overround"),
        field="overround",
    )
    complete_total = require_finite_number(
        evaluation_row.get("complete_market_raw_total"),
        field="complete_market_raw_total",
    )
    if abs(complete_total - (1.0 + evaluation_overround)) > VALUE_CALCULATION_TOLERANCE:
        raise ArtifactError("market evaluation complete_market_raw_total is inconsistent")
    expected_quality = _prediction_quality_passed_from_row(prediction_row)
    actual_quality = require_bool(
        opportunity_row.get("prediction_quality_passed"),
        field="prediction_quality_passed",
    )
    if actual_quality != expected_quality:
        raise ArtifactError("prediction_quality_passed does not match prediction quality")
    _require_matching_field(
        opportunity_row,
        evaluation_row,
        field="evaluation_version",
        label="evaluation_version",
    )


def _prediction_quality_passed_from_row(prediction_row: dict[str, JsonValue]) -> bool:
    quality = require_dict(prediction_row.get("quality"), field="quality")
    return all(
        (
            require_bool(quality.get("calibrated"), field="quality.calibrated"),
            require_bool(
                quality.get("model_artifact_verified"),
                field="quality.model_artifact_verified",
            ),
            require_bool(
                quality.get("feature_artifact_verified"),
                field="quality.feature_artifact_verified",
            ),
            require_bool(quality.get("sufficient_history"), field="quality.sufficient_history"),
            require_bool(quality.get("data_quality_passed"), field="quality.data_quality_passed"),
        )
    )


def _validate_opportunity_timing_semantics(row: dict[str, JsonValue]) -> None:
    from sports_analytics.data.codec import format_utc_timestamp

    evaluation_mode = require_str(row.get("evaluation_mode"), field="evaluation_mode")
    event_start = format_utc_timestamp(
        require_canonical_utc_timestamp_string(
            row.get("event_start_utc"),
            field="event_start_utc",
        )
    )
    predicted_at = require_canonical_utc_timestamp_string(
        row.get("predicted_at_utc"),
        field="predicted_at_utc",
    )
    source_observed_at = require_canonical_utc_timestamp_string(
        row.get("source_observed_at_utc"),
        field="source_observed_at_utc",
    )
    decision_as_of = format_utc_timestamp(
        require_canonical_utc_timestamp_string(
            row.get("decision_as_of_utc"),
            field="decision_as_of_utc",
        )
    )
    if evaluation_mode == "live-safe":
        quoted_at_raw = row.get("quoted_at_utc")
        if quoted_at_raw is None:
            raise ArtifactError("live-safe opportunity requires quoted_at_utc")
        quoted_at = require_canonical_utc_timestamp_string(
            quoted_at_raw,
            field="quoted_at_utc",
        )
        expected_decision = format_utc_timestamp(max(predicted_at, quoted_at, source_observed_at))
        if decision_as_of != expected_decision:
            raise ArtifactError("decision_as_of_utc does not match derived live-safe timing")
        if decision_as_of >= event_start:
            raise ArtifactError("decision_as_of_utc must be strictly before event_start_utc")
    elif evaluation_mode == "closing-line-historical-benchmark":
        if decision_as_of != event_start:
            raise ArtifactError("closing benchmark decision_as_of_utc must equal event_start_utc")
    else:
        raise ArtifactError("evaluation_mode is unsupported")


def _prediction_probability_for_selection(
    prediction_row: dict[str, JsonValue],
    selection_id: str,
) -> float:
    probabilities = require_list(prediction_row.get("probabilities"), field="probabilities")
    for index, item in enumerate(probabilities):
        probability_item = require_dict(item, field=f"probabilities[{index}]")
        if (
            require_str(
                probability_item.get("selection_id"),
                field=f"probabilities[{index}].selection_id",
            )
            == selection_id
        ):
            return require_probability(
                probability_item.get("probability"),
                field=f"probabilities[{index}].probability",
            )
    raise ArtifactError("prediction does not contain opportunity selection probability")


def _require_matching_field(
    left: dict[str, JsonValue],
    right: dict[str, JsonValue],
    *,
    field: str,
    label: str,
    left_field: str | None = None,
) -> None:
    left_value = left.get(left_field or field)
    right_value = right.get(field)
    if left_value != right_value:
        raise ArtifactError(f"opportunity {label} does not match authoritative source")


def _require_matching_number(
    left: dict[str, JsonValue],
    right: dict[str, JsonValue],
    *,
    field: str,
    label: str,
) -> None:
    left_value = require_finite_number(left.get(field), field=f"opportunity.{field}")
    right_value = require_finite_number(right.get(field), field=f"authoritative.{field}")
    if abs(left_value - right_value) > VALUE_CALCULATION_TOLERANCE:
        raise ArtifactError(f"opportunity {label} does not match authoritative source")


def _require_matching_decimal(
    left: dict[str, JsonValue],
    right: dict[str, JsonValue],
    *,
    field: str,
    label: str,
) -> None:
    left_value = require_decimal_string(left.get(field), field=f"opportunity.{field}")
    right_value = require_decimal_string(right.get(field), field=f"authoritative.{field}")
    if format(left_value, "f") != format(right_value, "f"):
        raise ArtifactError(f"opportunity {label} does not match authoritative source")


def _require_matching_timestamp(
    left: dict[str, JsonValue],
    right: dict[str, JsonValue],
    *,
    field: str,
    label: str,
) -> None:
    from sports_analytics.data.codec import format_utc_timestamp

    left_value = format_utc_timestamp(
        require_canonical_utc_timestamp_string(left.get(field), field=f"opportunity.{field}")
    )
    right_value = format_utc_timestamp(
        require_canonical_utc_timestamp_string(right.get(field), field=f"authoritative.{field}")
    )
    if left_value != right_value:
        raise ArtifactError(f"opportunity {label} does not match authoritative source")


def _recompute_prediction_id(row: dict[str, JsonValue]) -> str:
    from sports_analytics.predictions.contracts import (
        PredictionInputSnapshot,
        PredictionLineage,
        PredictionQualityFlags,
        SelectionProbability,
        derive_prediction_id,
    )
    from sports_analytics.predictions.provenance import parse_prediction_provenance

    lineage_raw = require_dict(row.get("lineage"), field="lineage")
    quality_raw = require_dict(row.get("quality"), field="quality")
    probabilities: list[SelectionProbability] = []
    probabilities_raw = require_list(row.get("probabilities"), field="probabilities")
    for index, item in enumerate(probabilities_raw):
        probability_item = require_dict(item, field=f"probabilities[{index}]")
        selection_id = require_str(
            probability_item.get("selection_id"),
            field=f"probabilities[{index}].selection_id",
        )
        selection = require_canonical_selection_identity(
            probability_item.get("selection"),
            field=f"probabilities[{index}].selection",
        )
        if selection_id != selection.selection_id:
            raise ArtifactError("prediction selection_id does not match selection payload")
        probabilities.append(
            SelectionProbability(
                selection=selection,
                probability=require_probability(
                    probability_item.get("probability"),
                    field=f"probabilities[{index}].probability",
                ),
            )
        )
    input_snapshots_raw = lineage_raw.get("input_snapshots", [])
    input_snapshots_list = require_list(input_snapshots_raw, field="lineage.input_snapshots")
    input_snapshots = tuple(
        PredictionInputSnapshot(
            snapshot_id=require_str(
                require_dict(snapshot, field="input_snapshot").get("snapshot_id"),
                field="snapshot_id",
            ),
            manifest_checksum_sha256=require_sha256_checksum(
                require_dict(snapshot, field="input_snapshot").get("manifest_checksum_sha256"),
                field="manifest_checksum_sha256",
            ),
            schema_version=require_str(
                require_dict(snapshot, field="input_snapshot").get("schema_version"),
                field="schema_version",
            ),
            source_name=require_str(
                require_dict(snapshot, field="input_snapshot").get("source_name"),
                field="source_name",
            ),
        )
        for snapshot in input_snapshots_list
    )
    lineage = PredictionLineage(
        model_artifact_id=require_str(
            lineage_raw.get("model_artifact_id"),
            field="model_artifact_id",
        ),
        model_checksum_sha256=require_sha256_checksum(
            lineage_raw.get("model_checksum_sha256"),
            field="model_checksum_sha256",
        ),
        model_specification_version=require_str(
            lineage_raw.get("model_specification_version"),
            field="model_specification_version",
        ),
        feature_artifact_id=require_str(
            lineage_raw.get("feature_artifact_id"),
            field="feature_artifact_id",
        ),
        feature_manifest_checksum_sha256=require_sha256_checksum(
            lineage_raw.get("feature_manifest_checksum_sha256"),
            field="feature_manifest_checksum_sha256",
        ),
        feature_specification_version=require_str(
            lineage_raw.get("feature_specification_version"),
            field="feature_specification_version",
        ),
        feature_row_id=require_str(lineage_raw.get("feature_row_id"), field="feature_row_id"),
        trained_through_date=require_date_string(
            lineage_raw.get("trained_through_date"),
            field="trained_through_date",
        ),
        calibrated_through_date=require_date_string(
            lineage_raw.get("calibrated_through_date"),
            field="calibrated_through_date",
        ),
        input_snapshots=input_snapshots,
    )
    quality = PredictionQualityFlags(
        calibrated=require_bool(quality_raw.get("calibrated"), field="quality.calibrated"),
        model_artifact_verified=require_bool(
            quality_raw.get("model_artifact_verified"),
            field="quality.model_artifact_verified",
        ),
        feature_artifact_verified=require_bool(
            quality_raw.get("feature_artifact_verified"),
            field="quality.feature_artifact_verified",
        ),
        sufficient_history=require_bool(
            quality_raw.get("sufficient_history"),
            field="quality.sufficient_history",
        ),
        data_quality_passed=require_bool(
            quality_raw.get("data_quality_passed"),
            field="quality.data_quality_passed",
        ),
    )
    ordered = require_list(row.get("ordered_selection_ids"), field="ordered_selection_ids")
    ordered_ids = tuple(require_str(item, field="ordered_selection_ids[]") for item in ordered)
    return derive_prediction_id(
        canonical_event_id=require_str(row["canonical_event_id"], field="canonical_event_id"),
        event_start_utc=require_canonical_utc_timestamp_string(
            row["event_start_utc"],
            field="event_start_utc",
        ),
        predicted_at_utc=require_canonical_utc_timestamp_string(
            row["predicted_at_utc"],
            field="predicted_at_utc",
        ),
        feature_available_at_utc=require_canonical_utc_timestamp_string(
            row["feature_available_at_utc"],
            field="feature_available_at_utc",
        ),
        lineage=lineage,
        probabilities=tuple(probabilities),
        ordered_selection_ids=ordered_ids,
        quality=quality,
        provenance=parse_prediction_provenance(
            require_str(row.get("provenance"), field="provenance"),
            field_name="provenance",
        ),
    )


def _validate_probability(value: JsonValue | None, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ArtifactError(f"{field} must be a finite number")
    number = float(value)
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        raise ArtifactError(f"{field} must lie in [0, 1]")


def _validate_finite(value: JsonValue | None, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ArtifactError(f"{field} must be a finite number")
    if not math.isfinite(float(value)):
        raise ArtifactError(f"{field} must be finite")


def parse_utc_timestamp(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ArtifactError("timestamp field is malformed") from exc
