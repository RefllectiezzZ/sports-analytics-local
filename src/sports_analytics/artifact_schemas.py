"""Exact versioned schema registry for typed analytical datasets."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Final, cast

from sports_analytics.artifact_strict import (
    require_bool,
    require_decimal_string,
    require_finite_number,
    require_str,
    require_utc_timestamp_string,
)
from sports_analytics.core.exceptions import ArtifactError
from sports_analytics.data.types import JsonValue, validate_sha256_checksum
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
    """Validate foreign-key relationships between typed datasets."""
    predictions = {row["prediction_id"] for row in datasets.get("predictions", ())}
    opportunities = {row["opportunity_id"] for row in datasets.get("opportunities", ())}
    decisions = datasets.get("opportunity_decisions", ())
    decision_ids = {row["opportunity_id"] for row in decisions}
    if decision_ids != opportunities:
        raise ArtifactError("opportunity decisions must cover every opportunity exactly once")
    for row in datasets.get("market_evaluations", ()):
        if row["prediction_id"] not in predictions:
            raise ArtifactError("orphan market evaluation references missing prediction")
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
    _validate_decision_ranks(datasets.get("opportunity_decisions", ()))
    for row in datasets.get("combinations", ()):
        opportunity_ids = row.get("opportunity_ids", [])
        if not isinstance(opportunity_ids, list):
            raise ArtifactError("combination opportunity_ids must be a list")
        for opportunity_id in opportunity_ids:
            if opportunity_id not in opportunities:
                raise ArtifactError("orphan combination references missing opportunity")
    for row in datasets.get("rejections", ()):
        kind = row.get("rejection_kind")
        if kind == REJECTION_KIND_OPPORTUNITY_FILTER:
            if row.get("opportunity_id") not in opportunities:
                raise ArtifactError("orphan rejection references missing opportunity")
        elif kind == REJECTION_KIND_COMBINATION_BUILDER:
            opportunity_ids = row.get("opportunity_ids", [])
            if not isinstance(opportunity_ids, list):
                raise ArtifactError("combination rejection opportunity_ids must be a list")
            for opportunity_id in opportunity_ids:
                if opportunity_id not in opportunities:
                    raise ArtifactError(
                        "orphan combination rejection references missing opportunity"
                    )
        else:
            raise ArtifactError("rejection_kind is unsupported")
    for row in datasets.get("settlements", ()):
        opportunity_ids = row.get("opportunity_ids", [])
        if not isinstance(opportunity_ids, list):
            raise ArtifactError("settlement opportunity_ids must be a list")
        for opportunity_id in opportunity_ids:
            if opportunity_id not in opportunities:
                raise ArtifactError("orphan settlement references missing opportunity")


def _validate_prediction_row(row: dict[str, JsonValue]) -> None:
    probabilities = row.get("probabilities")
    ordered = row.get("ordered_selection_ids")
    if not isinstance(probabilities, list) or not isinstance(ordered, list):
        raise ArtifactError("prediction probabilities are malformed")
    values: list[float] = []
    ids: list[str] = []
    for item in probabilities:
        if not isinstance(item, dict):
            raise ArtifactError("prediction probability entry is malformed")
        selection_id = item.get("selection_id")
        probability = item.get("probability")
        if type(selection_id) is not str:
            raise ArtifactError("prediction selection_id is malformed")
        _validate_probability(probability, "probability")
        ids.append(selection_id)
        values.append(float(probability))  # type: ignore[arg-type]
    if ids != ordered:
        raise ArtifactError("prediction ordered selection ids mismatch")
    if abs(math.fsum(values) - 1.0) > 1e-9:
        raise ArtifactError("prediction probabilities must sum to one")
    provenance = row.get("provenance")
    if type(provenance) is not str or provenance not in {"historical-replay", "synthetic-contract"}:
        raise ArtifactError("prediction provenance is unsupported")
    lineage = row.get("lineage")
    if not isinstance(lineage, dict):
        raise ArtifactError("prediction lineage is malformed")
    for field in (
        "model_artifact_id",
        "model_checksum_sha256",
        "model_specification_version",
        "feature_artifact_id",
        "feature_manifest_checksum_sha256",
        "feature_specification_version",
        "feature_row_id",
    ):
        if type(lineage.get(field)) is not str or not lineage.get(field):
            raise ArtifactError(f"prediction lineage field {field} is incomplete")
    try:
        validate_sha256_checksum(str(lineage["model_checksum_sha256"]))
        validate_sha256_checksum(str(lineage["feature_manifest_checksum_sha256"]))
    except Exception as exc:
        raise ArtifactError("prediction lineage checksum is malformed") from exc
    if lineage["feature_row_id"] != row["canonical_event_id"]:
        raise ArtifactError("prediction feature_row_id must match canonical_event_id")
    expected_id = _recompute_prediction_id(row)
    if row["prediction_id"] != expected_id:
        raise ArtifactError("prediction_id does not match canonical identity")


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
    if isinstance(overround, bool) or not isinstance(overround, int | float):
        raise ArtifactError("overround must be numeric")
    if not math.isfinite(float(overround)) or float(overround) < 0.0:
        raise ArtifactError("overround must be finite and non-negative")
    complete_total = row.get("complete_market_raw_total")
    if isinstance(complete_total, bool) or not isinstance(complete_total, int | float):
        raise ArtifactError("complete_market_raw_total must be numeric")
    complete_total_f = float(complete_total)
    if not math.isfinite(complete_total_f) or complete_total_f <= 0.0:
        raise ArtifactError("complete_market_raw_total must be positive")
    if abs(complete_total_f - (1.0 + float(overround))) > VALUE_CALCULATION_TOLERANCE:
        raise ArtifactError("complete_market_raw_total is inconsistent with overround")
    odds = row.get("decimal_odds")
    if type(odds) is not str:
        raise ArtifactError("decimal_odds must be a string")
    odds_f = float(odds)
    if not math.isfinite(odds_f) or odds_f <= 1.0:
        raise ArtifactError("decimal_odds must be finite and >1")
    raw = row.get("raw_implied_probability")
    normalized = row.get("normalized_implied_probability")
    model_prob = row.get("model_probability")
    edge = row.get("edge")
    expected_value = row.get("expected_value")
    for field_name, value in (
        ("raw_implied_probability", raw),
        ("normalized_implied_probability", normalized),
        ("model_probability", model_prob),
        ("edge", edge),
        ("expected_value", expected_value),
    ):
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ArtifactError(f"{field_name} must be numeric")
        if not math.isfinite(float(value)):
            raise ArtifactError(f"{field_name} must be finite")
    normalized_f = float(cast(int | float, normalized))
    raw_f = float(cast(int | float, raw))
    model_prob_f = float(cast(int | float, model_prob))
    edge_f = float(cast(int | float, edge))
    expected_value_f = float(cast(int | float, expected_value))
    if normalized_f <= 0.0 or normalized_f > 1.0:
        raise ArtifactError("normalized implied probability must be in (0, 1]")
    expected_raw = 1.0 / odds_f
    if abs(raw_f - expected_raw) > VALUE_CALCULATION_TOLERANCE:
        raise ArtifactError("raw implied probability does not match decimal odds")
    expected_normalized = expected_raw / complete_total_f
    if abs(normalized_f - expected_normalized) > VALUE_CALCULATION_TOLERANCE:
        raise ArtifactError("normalized implied probability is inconsistent")
    expected_edge = model_prob_f - expected_normalized
    if abs(edge_f - expected_edge) > VALUE_CALCULATION_TOLERANCE:
        raise ArtifactError("edge is inconsistent")
    expected_ev = model_prob_f * odds_f - 1.0
    if abs(expected_value_f - expected_ev) > VALUE_CALCULATION_TOLERANCE:
        raise ArtifactError("expected_value is inconsistent")


def _validate_opportunity_row(row: dict[str, JsonValue]) -> None:
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
        if type(row.get(field)) is not str or not row.get(field):
            raise ArtifactError(f"opportunity lineage field {field} is incomplete")
    if row["feature_row_id"] != row["canonical_event_id"]:
        raise ArtifactError("opportunity feature_row_id must match canonical_event_id")
    if type(row.get("prediction_quality_passed")) is not bool:
        raise ArtifactError("prediction_quality_passed must be boolean")
    overround = row.get("overround")
    if isinstance(overround, bool) or not isinstance(overround, int | float):
        raise ArtifactError("overround must be numeric")
    if float(overround) < 0.0:
        raise ArtifactError("overround must be non-negative")
    odds = row.get("decimal_odds")
    if type(odds) is not str:
        raise ArtifactError("decimal_odds must be a string")
    complete_total = 1.0 + float(overround)
    odds_f = float(odds)
    raw = float(row["raw_implied_probability"])  # type: ignore[arg-type]
    normalized = float(row["normalized_implied_probability"])  # type: ignore[arg-type]
    if normalized <= 0.0:
        raise ArtifactError("normalized implied probability must be positive")
    if abs(raw - 1.0 / odds_f) > VALUE_CALCULATION_TOLERANCE:
        raise ArtifactError("opportunity raw implied probability is inconsistent")
    if abs(normalized - raw / complete_total) > VALUE_CALCULATION_TOLERANCE:
        raise ArtifactError("opportunity normalized implied probability is inconsistent")


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


def _validate_combination_row(row: dict[str, JsonValue]) -> None:
    _validate_finite(row.get("expected_value"), "expected_value")
    _validate_finite(row.get("edge", 0.0), "edge") if "edge" in row else None
    _validate_probability(row.get("joint_probability"), "joint_probability")
    odds = row.get("total_decimal_odds")
    if type(odds) is not str or float(odds) <= 1:
        raise ArtifactError("combination total_decimal_odds must be >1")
    joint = row["joint_probability"]
    expected_value = row["expected_value"]
    if not isinstance(joint, int | float) or not isinstance(expected_value, int | float):
        raise ArtifactError("combination probability or expected_value is invalid")
    calculated = float(joint) * float(odds) - 1
    if abs(calculated - float(expected_value)) > 1e-9:
        raise ArtifactError("combination expected_value is inconsistent")
    dependencies = row.get("dependencies")
    if not isinstance(dependencies, list):
        raise ArtifactError("combination dependencies must be a list")
    opportunity_ids = row.get("opportunity_ids", [])
    if not isinstance(opportunity_ids, list):
        raise ArtifactError("combination opportunity_ids must be a list")
    pairs_seen: set[tuple[str, str]] = set()
    for item in dependencies:
        if not isinstance(item, dict):
            raise ArtifactError("combination dependency entry is malformed")
        left = item.get("left_opportunity_id")
        right = item.get("right_opportunity_id")
        if type(left) is not str or type(right) is not str:
            raise ArtifactError("combination dependency ids are malformed")
        if item.get("classification") != "structurally_separate":
            raise ArtifactError(
                "combination dependency classification must be structurally_separate"
            )
        pair = cast(tuple[str, str], tuple(sorted((left, right))))
        if pair in pairs_seen:
            raise ArtifactError("combination dependency pair is duplicated")
        if pair[0] not in opportunity_ids or pair[1] not in opportunity_ids:
            raise ArtifactError("combination dependency pair references undeclared leg")
        pairs_seen.add(pair)
    expected_pairs = len(opportunity_ids) * (len(opportunity_ids) - 1) // 2
    if len(pairs_seen) != expected_pairs:
        raise ArtifactError("combination must contain every dependency pair exactly once")


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
        opportunity_ids = [str(row["opportunity_id"]) for row in filter_rows]
        if len(opportunity_ids) != len(set(opportunity_ids)):
            raise ArtifactError("duplicate opportunity decision under one filter configuration")
        ranks = []
        for row in filter_rows:
            rank = row.get("accepted_rank")
            if row.get("eligible") is True and isinstance(rank, int):
                ranks.append(rank)
        if not ranks:
            continue
        ranks_sorted = sorted(ranks)
        if ranks_sorted != list(range(1, len(ranks_sorted) + 1)):
            raise ArtifactError("accepted ranks must be unique and contiguous")


def _recompute_prediction_id(row: dict[str, JsonValue]) -> str:
    from sports_analytics.predictions.contracts import (
        CanonicalSelectionIdentity,
        PredictionLineage,
        PredictionQualityFlags,
        SelectionProbability,
        derive_prediction_id,
    )
    from sports_analytics.predictions.provenance import parse_prediction_provenance

    lineage_raw = row["lineage"]
    if not isinstance(lineage_raw, dict):
        raise ArtifactError("prediction lineage is malformed")
    quality_raw = row.get("quality")
    if not isinstance(quality_raw, dict):
        raise ArtifactError("prediction quality is malformed")
    probabilities: list[SelectionProbability] = []
    probabilities_raw = row.get("probabilities")
    if not isinstance(probabilities_raw, list):
        raise ArtifactError("prediction probabilities are malformed")
    for item in probabilities_raw:
        if not isinstance(item, dict):
            raise ArtifactError("prediction probability entry is malformed")
        selection_raw = item.get("selection")
        if not isinstance(selection_raw, dict):
            raise ArtifactError("prediction selection is malformed")
        line_value = selection_raw.get("line_value")
        participant = selection_raw.get("canonical_participant_id")
        selection = CanonicalSelectionIdentity(
            sport_code=str(selection_raw["sport_code"]),
            market_family=str(selection_raw["market_family"]),
            market_key=str(selection_raw["market_key"]),
            market_period=str(selection_raw["market_period"]),
            participant_scope=str(selection_raw["participant_scope"]),
            canonical_participant_id=(None if participant is None else str(participant)),
            line_type=str(selection_raw["line_type"]),
            line_value=None if line_value is None else Decimal(str(line_value)),
            outcome_key=str(selection_raw["outcome_key"]),
        )
        probabilities.append(
            SelectionProbability(selection=selection, probability=float(item["probability"]))  # type: ignore[arg-type]
        )
    input_snapshots_raw = lineage_raw.get("input_snapshots", [])
    if not isinstance(input_snapshots_raw, list):
        raise ArtifactError("prediction input snapshots are malformed")
    from sports_analytics.predictions.contracts import PredictionInputSnapshot

    input_snapshots = tuple(
        PredictionInputSnapshot(
            snapshot_id=str(snapshot["snapshot_id"]),
            manifest_checksum_sha256=str(snapshot["manifest_checksum_sha256"]),
            schema_version=str(snapshot.get("schema_version", "snapshot-manifest-v1")),
            source_name=str(snapshot.get("source_name", "unknown-source")),
        )
        for snapshot in input_snapshots_raw
        if isinstance(snapshot, dict)
    )
    lineage = PredictionLineage(
        model_artifact_id=str(lineage_raw["model_artifact_id"]),
        model_checksum_sha256=str(lineage_raw["model_checksum_sha256"]),
        model_specification_version=str(lineage_raw["model_specification_version"]),
        feature_artifact_id=str(lineage_raw["feature_artifact_id"]),
        feature_manifest_checksum_sha256=str(lineage_raw["feature_manifest_checksum_sha256"]),
        feature_specification_version=str(lineage_raw["feature_specification_version"]),
        feature_row_id=str(lineage_raw["feature_row_id"]),
        trained_through_date=date.fromisoformat(str(lineage_raw["trained_through_date"])),
        calibrated_through_date=date.fromisoformat(str(lineage_raw["calibrated_through_date"])),
        input_snapshots=input_snapshots,
    )
    quality = PredictionQualityFlags(
        calibrated=require_bool(quality_raw.get("calibrated", False), field="calibrated"),
        model_artifact_verified=require_bool(
            quality_raw.get("model_artifact_verified", False),
            field="model_artifact_verified",
        ),
        feature_artifact_verified=require_bool(
            quality_raw.get("feature_artifact_verified", False),
            field="feature_artifact_verified",
        ),
        sufficient_history=require_bool(
            quality_raw.get("sufficient_history", False),
            field="sufficient_history",
        ),
        data_quality_passed=require_bool(
            quality_raw.get("data_quality_passed", False),
            field="data_quality_passed",
        ),
    )
    ordered = row.get("ordered_selection_ids")
    ordered_ids = tuple(str(item) for item in ordered) if isinstance(ordered, list) else None
    return derive_prediction_id(
        canonical_event_id=require_str(row["canonical_event_id"], field="canonical_event_id"),
        event_start_utc=require_utc_timestamp_string(
            row["event_start_utc"],
            field="event_start_utc",
        ),
        predicted_at_utc=require_utc_timestamp_string(
            row["predicted_at_utc"],
            field="predicted_at_utc",
        ),
        feature_available_at_utc=require_utc_timestamp_string(
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
