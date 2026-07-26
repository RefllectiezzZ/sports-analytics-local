"""Deterministic champion-challenger comparison contracts."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final, cast

from sports_analytics.core.exceptions import GovernanceError
from sports_analytics.data.codec import format_utc_timestamp
from sports_analytics.data.types import JsonValue, validate_sha256_checksum
from sports_analytics.models.identity import content_addressed_id
from sports_analytics.sports.contracts import require_utc

GOVERNANCE_DECISION_SCHEMA_VERSION: Final[str] = "champion-challenger-decision-v1"


class ModelRole(StrEnum):
    CHAMPION = "champion"
    CHALLENGER = "challenger"
    ARCHIVED = "archived"


class ModelLifecycleStatus(StrEnum):
    REGISTERED = "registered"
    ELIGIBLE = "eligible"
    PROMOTED = "promoted"
    DEMOTED = "demoted"
    ARCHIVED = "archived"
    REJECTED = "rejected"


class GovernanceDecisionKind(StrEnum):
    PROMOTE = "promote"
    RETAIN = "retain"
    HOLD = "hold"
    REJECT = "reject"


@dataclass(frozen=True, slots=True)
class ModelRegistryEntry:
    model_artifact_id: str
    model_checksum_sha256: str
    model_relative_path: str
    model_specification_version: str
    feature_specification_version: str
    sport_code: str
    market_key: str
    registered_at: datetime
    role: ModelRole
    lifecycle_status: ModelLifecycleStatus
    actor: str
    provenance: JsonValue
    superseded_model_artifact_id: str | None = None
    version: int = 1

    @property
    def scope(self) -> tuple[str, str]:
        return self.sport_code, self.market_key


@dataclass(frozen=True, slots=True)
class ModelEvaluationEvidence:
    evidence_artifact_id: str
    evidence_checksum_sha256: str
    model_artifact_id: str
    sport_code: str
    market_key: str
    evaluation_mode: str
    window_start_utc: datetime
    window_end_utc: datetime
    event_population_id: str
    sample_size: int
    completed_result_count: int
    coverage: float
    log_loss: float
    multiclass_brier_score: float
    calibration_error: float | None
    hit_rate: float | None = None
    roi: float | None = None

    def __post_init__(self) -> None:
        try:
            validate_sha256_checksum(self.evidence_checksum_sha256)
            object.__setattr__(
                self,
                "window_start_utc",
                require_utc(self.window_start_utc, field_name="window_start_utc"),
            )
            object.__setattr__(
                self,
                "window_end_utc",
                require_utc(self.window_end_utc, field_name="window_end_utc"),
            )
        except Exception as exc:
            raise GovernanceError(f"invalid governance evidence identity: {exc}") from exc
        if not self.window_start_utc < self.window_end_utc:
            raise GovernanceError("governance evidence window must be increasing")
        if type(self.sample_size) is not int or self.sample_size < 0:
            raise GovernanceError("governance evidence sample_size must be non-negative")
        if type(self.completed_result_count) is not int or not (
            0 <= self.completed_result_count <= self.sample_size
        ):
            raise GovernanceError("completed result count is outside sample population")
        for field_name in ("coverage", "log_loss", "multiclass_brier_score"):
            value = getattr(self, field_name)
            if (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not math.isfinite(value)
            ):
                raise GovernanceError(f"{field_name} must be finite")
        if not 0 <= self.coverage <= 1:
            raise GovernanceError("coverage must lie in [0,1]")
        expected_coverage = (
            0.0 if self.sample_size == 0 else self.completed_result_count / self.sample_size
        )
        if not math.isclose(self.coverage, expected_coverage, abs_tol=1e-12):
            raise GovernanceError(
                "coverage must equal completed_result_count divided by sample_size"
            )
        for field_name in ("calibration_error", "hit_rate", "roi"):
            value = getattr(self, field_name)
            if value is not None and not math.isfinite(value):
                raise GovernanceError(f"{field_name} must be finite when supplied")

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "evidence_artifact_id": self.evidence_artifact_id,
            "evidence_checksum_sha256": self.evidence_checksum_sha256,
            "model_artifact_id": self.model_artifact_id,
            "sport_code": self.sport_code,
            "market_key": self.market_key,
            "evaluation_mode": self.evaluation_mode,
            "window_start_utc": format_utc_timestamp(self.window_start_utc),
            "window_end_utc": format_utc_timestamp(self.window_end_utc),
            "event_population_id": self.event_population_id,
            "sample_size": self.sample_size,
            "completed_result_count": self.completed_result_count,
            "coverage": self.coverage,
            "log_loss": self.log_loss,
            "multiclass_brier_score": self.multiclass_brier_score,
            "calibration_error": self.calibration_error,
            "hit_rate": self.hit_rate,
            "roi": self.roi,
        }


@dataclass(frozen=True, slots=True)
class PromotionPolicy:
    policy_id: str = "proper-score-champion-challenger"
    policy_version: str = "promotion-policy-v1"
    minimum_sample_size: int = 100
    minimum_coverage: float = 0.95
    minimum_log_loss_improvement: float = 0.01
    minimum_brier_improvement: float = 0.01
    minimum_calibration_improvement: float = 0.0
    require_calibration: bool = True

    def __post_init__(self) -> None:
        if type(self.minimum_sample_size) is not int or self.minimum_sample_size < 1:
            raise GovernanceError("promotion minimum sample size must be positive")
        for field_name in (
            "minimum_coverage",
            "minimum_log_loss_improvement",
            "minimum_brier_improvement",
            "minimum_calibration_improvement",
        ):
            value = getattr(self, field_name)
            if not math.isfinite(value) or value < 0:
                raise GovernanceError(f"{field_name} must be finite and non-negative")
        if self.minimum_coverage > 1:
            raise GovernanceError("minimum coverage must lie in [0,1]")

    @property
    def policy_configuration_id(self) -> str:
        return content_addressed_id(
            identity_type=self.policy_version,
            payload={
                "policy_id": self.policy_id,
                "minimum_sample_size": self.minimum_sample_size,
                "minimum_coverage": self.minimum_coverage,
                "minimum_log_loss_improvement": self.minimum_log_loss_improvement,
                "minimum_brier_improvement": self.minimum_brier_improvement,
                "minimum_calibration_improvement": self.minimum_calibration_improvement,
                "require_calibration": self.require_calibration,
            },
        )


@dataclass(frozen=True, slots=True)
class ComparedMetric:
    name: str
    champion_value: float
    challenger_value: float
    required_improvement: float

    @property
    def improvement(self) -> float:
        return self.champion_value - self.challenger_value

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "name": self.name,
            "champion_value": self.champion_value,
            "challenger_value": self.challenger_value,
            "improvement": self.improvement,
            "required_improvement": self.required_improvement,
        }


@dataclass(frozen=True, slots=True)
class GovernanceDecision:
    decision_id: str
    schema_version: str
    policy_id: str
    policy_version: str
    policy_configuration_id: str
    champion_model_artifact_id: str
    challenger_model_artifact_id: str
    champion_registry_version: int
    challenger_registry_version: int
    champion_evidence: ModelEvaluationEvidence
    challenger_evidence: ModelEvaluationEvidence
    compared_metrics: tuple[ComparedMetric, ...]
    decision: GovernanceDecisionKind
    reasons: tuple[str, ...]
    as_of_utc: datetime

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "decision_id": self.decision_id,
            "schema_version": self.schema_version,
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "policy_configuration_id": self.policy_configuration_id,
            "champion_model_artifact_id": self.champion_model_artifact_id,
            "challenger_model_artifact_id": self.challenger_model_artifact_id,
            "champion_registry_version": self.champion_registry_version,
            "challenger_registry_version": self.challenger_registry_version,
            "champion_evidence": self.champion_evidence.to_json(),
            "challenger_evidence": self.challenger_evidence.to_json(),
            "compared_metrics": [item.to_json() for item in self.compared_metrics],
            "decision": self.decision.value,
            "reasons": list(self.reasons),
            "as_of_utc": format_utc_timestamp(self.as_of_utc),
        }


def evaluate_challenger(
    *,
    champion: ModelRegistryEntry,
    challenger: ModelRegistryEntry,
    champion_evidence: ModelEvaluationEvidence,
    challenger_evidence: ModelEvaluationEvidence,
    policy: PromotionPolicy,
    as_of_utc: datetime,
) -> GovernanceDecision:
    """Compare equivalent proper-score evidence without mutating registry roles."""
    as_of = require_utc(as_of_utc, field_name="as_of_utc")
    reasons: list[str] = []
    decision = GovernanceDecisionKind.RETAIN
    compatible = (
        champion.scope == challenger.scope
        and champion.scope == (champion_evidence.sport_code, champion_evidence.market_key)
        and challenger.scope == (challenger_evidence.sport_code, challenger_evidence.market_key)
        and champion_evidence.model_artifact_id == champion.model_artifact_id
        and challenger_evidence.model_artifact_id == challenger.model_artifact_id
    )
    equivalent = (
        champion_evidence.evaluation_mode == challenger_evidence.evaluation_mode
        and champion_evidence.window_start_utc == challenger_evidence.window_start_utc
        and champion_evidence.window_end_utc == challenger_evidence.window_end_utc
        and champion_evidence.event_population_id == challenger_evidence.event_population_id
        and champion_evidence.sample_size == challenger_evidence.sample_size
        and champion_evidence.completed_result_count == challenger_evidence.completed_result_count
    )
    metrics = [
        ComparedMetric(
            "log_loss",
            champion_evidence.log_loss,
            challenger_evidence.log_loss,
            policy.minimum_log_loss_improvement,
        ),
        ComparedMetric(
            "multiclass_brier_score",
            champion_evidence.multiclass_brier_score,
            challenger_evidence.multiclass_brier_score,
            policy.minimum_brier_improvement,
        ),
    ]
    if (
        champion_evidence.calibration_error is not None
        and challenger_evidence.calibration_error is not None
    ):
        metrics.append(
            ComparedMetric(
                "calibration_error",
                champion_evidence.calibration_error,
                challenger_evidence.calibration_error,
                policy.minimum_calibration_improvement,
            )
        )
    if not compatible:
        decision = GovernanceDecisionKind.REJECT
        reasons.append("incompatible-model-scope-or-identity")
    elif not equivalent:
        decision = GovernanceDecisionKind.REJECT
        reasons.append("unequal-evaluation-window-or-population")
    elif policy.require_calibration and (
        champion_evidence.calibration_error is None or challenger_evidence.calibration_error is None
    ):
        decision = GovernanceDecisionKind.HOLD
        reasons.append("missing-calibration-evidence")
    elif (
        champion_evidence.sample_size < policy.minimum_sample_size
        or challenger_evidence.sample_size < policy.minimum_sample_size
        or champion_evidence.coverage < policy.minimum_coverage
        or challenger_evidence.coverage < policy.minimum_coverage
    ):
        decision = GovernanceDecisionKind.HOLD
        reasons.append("insufficient-sample-or-result-coverage")
    elif any(item.improvement < 0 for item in metrics):
        decision = GovernanceDecisionKind.RETAIN
        reasons.append("challenger-materially-worse")
    elif all(item.improvement >= item.required_improvement for item in metrics) and any(
        item.improvement > 0 for item in metrics
    ):
        decision = GovernanceDecisionKind.PROMOTE
        reasons.append("all-proper-score-requirements-met")
    elif all(item.improvement == 0 for item in metrics):
        decision = GovernanceDecisionKind.RETAIN
        reasons.append("exact-tie")
    else:
        decision = GovernanceDecisionKind.RETAIN
        reasons.append("improvement-below-required-margin")
    payload: dict[str, JsonValue] = {
        "policy_id": policy.policy_id,
        "policy_version": policy.policy_version,
        "policy_configuration_id": policy.policy_configuration_id,
        "champion_model_artifact_id": champion.model_artifact_id,
        "challenger_model_artifact_id": challenger.model_artifact_id,
        "champion_registry_version": champion.version,
        "challenger_registry_version": challenger.version,
        "champion_evidence": champion_evidence.to_json(),
        "challenger_evidence": challenger_evidence.to_json(),
        "compared_metrics": [item.to_json() for item in metrics],
        "decision": decision.value,
        "reasons": cast(list[JsonValue], sorted(reasons)),
        "as_of_utc": format_utc_timestamp(as_of),
    }
    return GovernanceDecision(
        decision_id=content_addressed_id(
            identity_type=GOVERNANCE_DECISION_SCHEMA_VERSION,
            payload=payload,
        ),
        schema_version=GOVERNANCE_DECISION_SCHEMA_VERSION,
        policy_id=policy.policy_id,
        policy_version=policy.policy_version,
        policy_configuration_id=policy.policy_configuration_id,
        champion_model_artifact_id=champion.model_artifact_id,
        challenger_model_artifact_id=challenger.model_artifact_id,
        champion_registry_version=champion.version,
        challenger_registry_version=challenger.version,
        champion_evidence=champion_evidence,
        challenger_evidence=challenger_evidence,
        compared_metrics=tuple(metrics),
        decision=decision,
        reasons=tuple(sorted(reasons)),
        as_of_utc=as_of,
    )
