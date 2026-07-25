"""Exact versioned schema registry for typed analytical datasets."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sports_analytics.core.exceptions import ArtifactError
from sports_analytics.data.types import JsonValue

PREDICTIONS_SCHEMA_VERSION: Final[str] = "predictions-v1"
MARKET_EVALUATIONS_SCHEMA_VERSION: Final[str] = "market-evaluations-v1"
OPPORTUNITY_DECISIONS_SCHEMA_VERSION: Final[str] = "opportunity-decisions-v1"
OPPORTUNITIES_SCHEMA_VERSION: Final[str] = "opportunities-v1"
COMBINATIONS_SCHEMA_VERSION: Final[str] = "combinations-v1"
REJECTIONS_SCHEMA_VERSION: Final[str] = "rejections-v1"
SETTLEMENTS_SCHEMA_VERSION: Final[str] = "settlements-v1"
FOLD_METRICS_SCHEMA_VERSION: Final[str] = "fold-metrics-v1"
AGGREGATE_METRICS_SCHEMA_VERSION: Final[str] = "aggregate-metrics-v1"

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
                "ordered_selection_ids",
                "probabilities",
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
                "prediction_id",
                "quote_observation_id",
                "selection_id",
                "expected_value",
                "edge",
                "raw_implied_probability",
                "normalized_implied_probability",
                "overround",
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
                "canonical_event_id",
                "event_start_utc",
                "decision_as_of_utc",
                "prediction_id",
                "quote_observation_id",
                "provider_id",
                "decimal_odds",
                "model_probability",
                "edge",
                "expected_value",
                "raw_implied_probability",
                "normalized_implied_probability",
                "model_artifact_id",
                "model_checksum_sha256",
                "feature_artifact_id",
                "feature_manifest_checksum_sha256",
                "feature_row_id",
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
                "opportunity_ids",
                "total_decimal_odds",
                "joint_probability",
                "expected_value",
                "common_decision_time_utc",
                "earliest_event_start_utc",
                "policy_id",
            }
        ),
    ),
    "rejections": DatasetSchema(
        name="rejections",
        version=REJECTIONS_SCHEMA_VERSION,
        id_field="rejection_id",
        required_fields=frozenset({"rejection_id", "schema_version", "opportunity_id", "codes"}),
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
                "profit_units",
            }
        ),
    ),
    "fold_metrics": DatasetSchema(
        name="fold_metrics",
        version=FOLD_METRICS_SCHEMA_VERSION,
        id_field="fold_id",
        required_fields=frozenset({"fold_id", "schema_version", "sample_size"}),
    ),
    "aggregate_metrics": DatasetSchema(
        name="aggregate_metrics",
        version=AGGREGATE_METRICS_SCHEMA_VERSION,
        id_field="metric_id",
        required_fields=frozenset({"metric_id", "schema_version", "backtest_id"}),
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
    if name in {"market_evaluations", "opportunities", "combinations"}:
        _validate_finite(row.get("expected_value"), "expected_value")
    if name in {"market_evaluations", "opportunities"}:
        _validate_finite(row.get("edge"), "edge")
    if name == "opportunities":
        _validate_probability(row.get("model_probability"), "model_probability")
        _validate_probability(row.get("normalized_implied_probability"), "normalized")
    if name == "combinations":
        _validate_probability(row.get("joint_probability"), "joint_probability")
        odds = row.get("total_decimal_odds")
        if not isinstance(odds, str | int | float) or float(odds) <= 1:
            raise ArtifactError("combination total_decimal_odds must be >1")
        joint = row["joint_probability"]
        expected_value = row["expected_value"]
        if not isinstance(joint, int | float) or not isinstance(odds, str | int | float):
            raise ArtifactError("combination probability or odds is invalid")
        if not isinstance(expected_value, int | float):
            raise ArtifactError("combination expected_value is invalid")
        calculated = float(joint) * float(odds) - 1
        if abs(calculated - float(expected_value)) > 1e-9:
            raise ArtifactError("combination expected_value is inconsistent")
    if name == "settlements":
        if row.get("result") not in {"win", "loss"}:
            raise ArtifactError("settlement result must be win or loss")
        if row.get("stake_units") != "1":
            raise ArtifactError("flat stake must be exactly one unit")
        returned = float(row["profit_units"]) + 1.0  # type: ignore[arg-type]
        if row.get("result") == "win":
            expected = float(row["decimal_odds"])  # type: ignore[arg-type]
        else:
            expected = 0.0
        if abs(returned - expected) > 1e-9:
            raise ArtifactError("settlement return/profit is inconsistent")


def validate_cross_dataset_integrity(datasets: dict[str, tuple[dict[str, JsonValue], ...]]) -> None:
    """Validate foreign-key relationships between typed datasets."""
    predictions = {row["prediction_id"] for row in datasets.get("predictions", ())}
    opportunities = {row["opportunity_id"] for row in datasets.get("opportunities", ())}
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
    for row in datasets.get("combinations", ()):
        opportunity_ids = row.get("opportunity_ids", [])
        if not isinstance(opportunity_ids, list):
            raise ArtifactError("combination opportunity_ids must be a list")
        for opportunity_id in opportunity_ids:
            if opportunity_id not in opportunities:
                raise ArtifactError("orphan combination references missing opportunity")
    for row in datasets.get("rejections", ()):
        if row["opportunity_id"] not in opportunities:
            raise ArtifactError("orphan rejection references missing opportunity")
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
