"""Fail-closed, immutable economic eligibility evidence for football proposals."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final, cast

from sports_analytics.artifacts import (
    AnalyticalArtifact,
    load_analytical_artifact,
    write_analytical_artifact,
)
from sports_analytics.core.exceptions import ArtifactError, EvaluationError
from sports_analytics.data.codec import format_utc_timestamp, parse_utc_timestamp
from sports_analytics.data.types import JsonValue, validate_sha256_checksum
from sports_analytics.models.identity import content_addressed_id
from sports_analytics.sports.contracts import require_utc

FOOTBALL_ECONOMIC_EVIDENCE_TYPE: Final[str] = "football-economic-evidence"
FOOTBALL_ECONOMIC_EVIDENCE_SCHEMA: Final[str] = "football-economic-evidence-v1"
FOOTBALL_ECONOMIC_POLICY_VERSION: Final[str] = "football-economic-eligibility-policy-v1"
_MODES: Final[frozenset[str]] = frozenset(
    {"prospective-operator", "historical-closing-benchmark", "synthetic-contract"}
)
_REFERENCE_FIELDS: Final[frozenset[str]] = frozenset(
    {"relative_directory", "artifact_id", "checksum_sha256", "artifact_type", "schema_version"}
)
_EVIDENCE_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "sport_code",
        "competition_id",
        "market_key",
        "model_artifact_id",
        "model_checksum_sha256",
        "champion_role_revision",
        "champion_transition_id",
        "evaluation_mode",
        "evidence_window_start_utc",
        "evidence_window_end_utc",
        "evaluated_at_utc",
        "prediction_population_id",
        "quote_population_id",
        "result_population_id",
        "settlement_population_id",
        "monitoring_population_id",
        "policy_id",
        "policy_version",
        "policy_configuration_id",
        "prospective_prediction_count",
        "timestamped_quote_count",
        "completed_settlement_count",
        "settlement_coverage",
        "log_loss",
        "multiclass_brier_score",
        "ranked_probability_score",
        "calibration_error",
        "market_baseline_log_loss",
        "market_baseline_brier_score",
        "market_baseline_rps",
        "realised_turnover",
        "realised_profit_loss",
        "realised_roi",
        "maximum_drawdown",
        "unresolved_settlement_count",
        "stale_or_invalid_quote_count",
        "source_classification",
        "evidence_derivation_version",
        "upstream_artifacts",
    }
)


@dataclass(frozen=True, slots=True, order=True)
class EconomicArtifactReference:
    relative_directory: str
    artifact_id: str
    checksum_sha256: str
    artifact_type: str
    schema_version: str

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "relative_directory": self.relative_directory,
            "artifact_id": self.artifact_id,
            "checksum_sha256": self.checksum_sha256,
            "artifact_type": self.artifact_type,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True, slots=True)
class FootballEconomicEligibilityPolicy:
    """Conservative code policy; a caller never supplies an eligibility result."""

    minimum_prospective_prediction_count: int = 200
    minimum_timestamped_quote_count: int = 200
    minimum_completed_settlement_count: int = 150
    minimum_settlement_coverage: float = 0.90
    maximum_calibration_error: float = 0.05
    maximum_log_loss: float = 1.10
    maximum_brier_score: float = 0.70
    maximum_rps: float = 0.35
    maximum_market_baseline_degradation: float = 0.01
    require_positive_roi: bool = True
    minimum_realised_roi: float = 0.02
    maximum_drawdown: float = 0.20
    maximum_unresolved_settlement_count: int = 0
    maximum_stale_or_invalid_quote_count: int = 0
    maximum_evidence_age: timedelta = timedelta(days=7)
    policy_id: str = "football-production-economic-eligibility"
    policy_version: str = FOOTBALL_ECONOMIC_POLICY_VERSION

    def __post_init__(self) -> None:
        if any(
            type(getattr(self, name)) is not int or getattr(self, name) < 0
            for name in (
                "minimum_prospective_prediction_count",
                "minimum_timestamped_quote_count",
                "minimum_completed_settlement_count",
                "maximum_unresolved_settlement_count",
                "maximum_stale_or_invalid_quote_count",
            )
        ):
            raise EvaluationError("economic policy counts are invalid")
        for name in (
            "minimum_settlement_coverage",
            "maximum_calibration_error",
            "maximum_log_loss",
            "maximum_brier_score",
            "maximum_rps",
            "maximum_market_baseline_degradation",
            "minimum_realised_roi",
            "maximum_drawdown",
        ):
            value = getattr(self, name)
            if (
                type(value) not in (int, float)
                or not math.isfinite(float(value))
                or float(value) < 0
            ):
                raise EvaluationError("economic policy metrics are invalid")
        if (
            self.maximum_evidence_age <= timedelta(0)
            or not self.policy_id
            or self.policy_version != FOOTBALL_ECONOMIC_POLICY_VERSION
        ):
            raise EvaluationError("economic policy identity is invalid")

    @property
    def configuration_id(self) -> str:
        return content_addressed_id(
            identity_type=FOOTBALL_ECONOMIC_POLICY_VERSION, payload=self.to_json()
        )

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "minimum_prospective_prediction_count": self.minimum_prospective_prediction_count,
            "minimum_timestamped_quote_count": self.minimum_timestamped_quote_count,
            "minimum_completed_settlement_count": self.minimum_completed_settlement_count,
            "minimum_settlement_coverage": self.minimum_settlement_coverage,
            "maximum_calibration_error": self.maximum_calibration_error,
            "maximum_log_loss": self.maximum_log_loss,
            "maximum_brier_score": self.maximum_brier_score,
            "maximum_rps": self.maximum_rps,
            "maximum_market_baseline_degradation": self.maximum_market_baseline_degradation,
            "require_positive_roi": self.require_positive_roi,
            "minimum_realised_roi": self.minimum_realised_roi,
            "maximum_drawdown": self.maximum_drawdown,
            "maximum_unresolved_settlement_count": self.maximum_unresolved_settlement_count,
            "maximum_stale_or_invalid_quote_count": self.maximum_stale_or_invalid_quote_count,
            "maximum_evidence_age_seconds": int(self.maximum_evidence_age.total_seconds()),
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
        }


@dataclass(frozen=True, slots=True)
class FootballEconomicEvidence:
    artifact: AnalyticalArtifact
    payload: dict[str, JsonValue]


@dataclass(frozen=True, slots=True)
class EconomicEligibilityDecision:
    opportunity_analysis_eligible: bool
    bet_proposal_eligible: bool
    promotion_eligible: bool
    hold_reasons: tuple[str, ...]


def write_football_economic_evidence(
    *, root: Path, relative_directory: str, payload: dict[str, JsonValue]
) -> AnalyticalArtifact:
    checked = _validate_payload(payload)
    return write_analytical_artifact(
        root=root,
        relative_directory=relative_directory,
        artifact_type=FOOTBALL_ECONOMIC_EVIDENCE_TYPE,
        schema_version=FOOTBALL_ECONOMIC_EVIDENCE_SCHEMA,
        payload=checked,
    )


def load_football_economic_evidence(
    *,
    root: Path,
    relative_directory: str,
    expected_artifact_id: str | None = None,
    expected_checksum: str | None = None,
) -> FootballEconomicEvidence:
    artifact = load_analytical_artifact(
        root=root,
        relative_directory=relative_directory,
        expected_artifact_type=FOOTBALL_ECONOMIC_EVIDENCE_TYPE,
        expected_schema_version=FOOTBALL_ECONOMIC_EVIDENCE_SCHEMA,
        expected_artifact_id=expected_artifact_id,
        expected_checksum=expected_checksum,
    )
    try:
        payload = _validate_payload(cast(dict[str, JsonValue], artifact.payload))
        for raw_reference in cast(list[object], payload["upstream_artifacts"]):
            reference = _reference(raw_reference)
            load_analytical_artifact(
                root=root,
                relative_directory=reference.relative_directory,
                expected_artifact_type=reference.artifact_type,
                expected_schema_version=reference.schema_version,
                expected_artifact_id=reference.artifact_id,
                expected_checksum=reference.checksum_sha256,
            )
    except (EvaluationError, TypeError) as exc:
        raise ArtifactError(str(exc)) from exc
    return FootballEconomicEvidence(artifact=artifact, payload=payload)


def evaluate_football_economic_evidence(
    *,
    evidence: FootballEconomicEvidence,
    policy: FootballEconomicEligibilityPolicy,
    model_artifact_id: str,
    model_checksum_sha256: str,
    competition_id: str,
    market_key: str,
    champion_role_revision: int,
    champion_transition_id: str | None,
    evaluated_at_utc: datetime,
) -> EconomicEligibilityDecision:
    p = cast(dict[str, Any], evidence.payload)
    holds: list[str] = []
    if p["evaluation_mode"] != "prospective-operator":
        holds.append(
            "historical-closing-only-evidence"
            if p["evaluation_mode"] == "historical-closing-benchmark"
            else "invalid-economic-evidence-provenance"
        )
    if p["evaluation_mode"] != "prospective-operator":
        holds.append("no-prospective-timestamped-evidence")
    if p["evaluation_mode"] == "historical-closing-benchmark":
        holds.extend(("negative-historical-closing-backtest", "market-baseline-materially-better"))
    if (
        p["model_artifact_id"] != model_artifact_id
        or p["model_checksum_sha256"] != model_checksum_sha256
    ):
        holds.append("economic-evidence-model-lineage-mismatch")
    if p["competition_id"] != competition_id:
        holds.append("economic-evidence-competition-mismatch")
    if p["market_key"] != market_key or p["sport_code"] != "football":
        holds.append("economic-evidence-market-mismatch")
    if (
        p["champion_role_revision"] != champion_role_revision
        or p["champion_transition_id"] != champion_transition_id
    ):
        holds.append("economic-evidence-model-lineage-mismatch")
    cutoff = require_utc(evaluated_at_utc, field_name="evaluated_at_utc")
    end = _utc(cast(str, p["evidence_window_end_utc"]), "evidence_window_end_utc")
    if cutoff - end > policy.maximum_evidence_age:
        holds.append("economic-evidence-stale")
    if (
        p["policy_id"] != policy.policy_id
        or p["policy_version"] != policy.policy_version
        or p["policy_configuration_id"] != policy.configuration_id
    ):
        holds.append("invalid-economic-evidence-provenance")
    if p["prospective_prediction_count"] < policy.minimum_prospective_prediction_count:
        holds.append("insufficient-prospective-sample")
    if p["timestamped_quote_count"] < policy.minimum_timestamped_quote_count:
        holds.append("insufficient-timestamped-quote-sample")
    if p["completed_settlement_count"] < policy.minimum_completed_settlement_count:
        holds.extend(
            ("insufficient-completed-settlement-sample", "no-prospective-settlement-cycle")
        )
    if p["settlement_coverage"] < policy.minimum_settlement_coverage:
        holds.append("insufficient-settlement-coverage")
    if p["unresolved_settlement_count"] > policy.maximum_unresolved_settlement_count:
        holds.append("unresolved-settlements-present")
    if p["stale_or_invalid_quote_count"] > policy.maximum_stale_or_invalid_quote_count:
        holds.append("stale-or-invalid-quotes-present")
    if p["calibration_error"] > policy.maximum_calibration_error:
        holds.append("calibration-threshold-failed")
    if (
        p["log_loss"] > policy.maximum_log_loss
        or p["multiclass_brier_score"] > policy.maximum_brier_score
        or (
            p["ranked_probability_score"] is not None
            and p["ranked_probability_score"] > policy.maximum_rps
        )
    ):
        holds.append("proper-score-threshold-failed")
    baseline = p["market_baseline_log_loss"]
    if (
        baseline is not None
        and p["log_loss"] > baseline + policy.maximum_market_baseline_degradation
    ):
        holds.append("market-baseline-threshold-failed")
    if policy.require_positive_roi and p["realised_roi"] < policy.minimum_realised_roi:
        holds.append("economic-return-threshold-failed")
    if p["maximum_drawdown"] > policy.maximum_drawdown:
        holds.append("drawdown-threshold-failed")
    canonical = tuple(sorted(set(holds)))
    proposal = not canonical
    return EconomicEligibilityDecision(True, proposal, proposal, canonical)


def _validate_payload(value: object) -> dict[str, JsonValue]:
    if not isinstance(value, dict) or set(value) != _EVIDENCE_FIELDS:
        raise EvaluationError("economic evidence fields are not exact")
    p = cast(dict[str, Any], value)
    for name in (
        "sport_code",
        "competition_id",
        "market_key",
        "model_artifact_id",
        "model_checksum_sha256",
        "prediction_population_id",
        "quote_population_id",
        "result_population_id",
        "settlement_population_id",
        "monitoring_population_id",
        "policy_id",
        "policy_version",
        "policy_configuration_id",
        "source_classification",
        "evidence_derivation_version",
    ):
        if (
            type(p[name]) is not str
            or not p[name]
            or p[name] != p[name].strip()
            or any(
                x in p[name].lower()
                for x in ("http", "cookie", "token", "selector", "<", ">", "\\\\")
            )
        ):
            raise EvaluationError("economic evidence text is invalid")
    if p["sport_code"] != "football" or p["evaluation_mode"] not in _MODES:
        raise EvaluationError("economic evidence scope or mode is invalid")
    for name in ("model_checksum_sha256",):
        try:
            validate_sha256_checksum(cast(str, p[name]))
        except Exception as exc:
            raise EvaluationError("economic evidence checksum is invalid") from exc
    if (
        type(p["champion_role_revision"]) is not int
        or p["champion_role_revision"] < 0
        or (
            p["champion_transition_id"] is not None and type(p["champion_transition_id"]) is not str
        )
    ):
        raise EvaluationError("economic evidence champion lineage is invalid")
    start, end, evaluated = (
        _utc(cast(str, p[n]), n)
        for n in ("evidence_window_start_utc", "evidence_window_end_utc", "evaluated_at_utc")
    )
    if not start < end <= evaluated:
        raise EvaluationError("economic evidence window is invalid")
    counts = (
        "prospective_prediction_count",
        "timestamped_quote_count",
        "completed_settlement_count",
        "unresolved_settlement_count",
        "stale_or_invalid_quote_count",
    )
    if any(type(p[n]) is not int or p[n] < 0 for n in counts):
        raise EvaluationError("economic evidence counts are invalid")
    if p["completed_settlement_count"] + p["unresolved_settlement_count"] > min(
        p["prospective_prediction_count"], p["timestamped_quote_count"]
    ):
        raise EvaluationError("economic evidence settlement populations are inconsistent")
    numbers = (
        "settlement_coverage",
        "log_loss",
        "multiclass_brier_score",
        "calibration_error",
        "realised_turnover",
        "realised_profit_loss",
        "realised_roi",
        "maximum_drawdown",
    )
    for n in numbers:
        if type(p[n]) not in (int, float) or not math.isfinite(float(p[n])):
            raise EvaluationError("economic evidence metric is invalid")
    if not 0 <= p["settlement_coverage"] <= 1 or p["settlement_coverage"] != p[
        "completed_settlement_count"
    ] / min(p["prospective_prediction_count"], p["timestamped_quote_count"]):
        raise EvaluationError("economic evidence coverage is inconsistent")
    for n in (
        "ranked_probability_score",
        "market_baseline_log_loss",
        "market_baseline_brier_score",
        "market_baseline_rps",
    ):
        if p[n] is not None and (type(p[n]) not in (int, float) or not math.isfinite(float(p[n]))):
            raise EvaluationError("economic evidence optional metric is invalid")
    refs = p["upstream_artifacts"]
    if not isinstance(refs, list) or not refs:
        raise EvaluationError("economic evidence upstream references are invalid")
    normalized = [_reference(item) for item in refs]
    if normalized != sorted(normalized) or len(set(normalized)) != len(normalized):
        raise EvaluationError("economic evidence upstream references are not canonical")
    return cast(dict[str, JsonValue], p)


def _reference(value: object) -> EconomicArtifactReference:
    if not isinstance(value, dict) or set(value) != _REFERENCE_FIELDS:
        raise EvaluationError("economic evidence reference fields are not exact")
    vals = [
        value[n]
        for n in (
            "relative_directory",
            "artifact_id",
            "checksum_sha256",
            "artifact_type",
            "schema_version",
        )
    ]
    if (
        any(type(x) is not str or not x or x != x.strip() for x in vals)
        or ".." in cast(str, vals[0]).split("/")
        or cast(str, vals[0]).startswith("/")
        or "\\" in cast(str, vals[0])
    ):
        raise EvaluationError("economic evidence reference is unsafe")
    try:
        validate_sha256_checksum(cast(str, vals[2]))
    except Exception as exc:
        raise EvaluationError("economic evidence reference checksum is invalid") from exc
    return EconomicArtifactReference(*cast(tuple[str, str, str, str, str], tuple(vals)))


def _utc(value: str, field: str) -> datetime:
    if not value.endswith("Z"):
        raise EvaluationError(f"{field} must be canonical UTC")
    try:
        parsed = parse_utc_timestamp(value)
    except Exception as exc:
        raise EvaluationError(f"{field} is invalid") from exc
    if format_utc_timestamp(parsed) != value:
        raise EvaluationError(f"{field} must be canonical UTC")
    return parsed
