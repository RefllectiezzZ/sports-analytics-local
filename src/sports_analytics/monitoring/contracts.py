"""Versioned monitoring policies, metrics, findings, and deterministic evaluation."""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final

from sports_analytics.core.exceptions import MonitoringError
from sports_analytics.data.codec import format_utc_timestamp
from sports_analytics.data.types import JsonValue, validate_sha256_checksum
from sports_analytics.models.identity import content_addressed_id
from sports_analytics.sports.contracts import require_utc

MONITORING_REPORT_SCHEMA_VERSION: Final[str] = "monitoring-report-v1"


class MetricStatus(StrEnum):
    HEALTHY = "healthy"
    WARNING = "warning"
    CRITICAL = "critical"
    UNKNOWN = "unknown"


class MetricDirection(StrEnum):
    HIGHER_IS_WORSE = "higher-is-worse"
    LOWER_IS_WORSE = "lower-is-worse"


@dataclass(frozen=True, slots=True, order=True)
class EvidenceReference:
    evidence_type: str
    evidence_id: str
    checksum_sha256: str

    def __post_init__(self) -> None:
        if not self.evidence_type or not self.evidence_id:
            raise MonitoringError("monitoring evidence identity must be non-empty")
        try:
            validate_sha256_checksum(self.checksum_sha256)
        except Exception as exc:
            raise MonitoringError("monitoring evidence checksum is malformed") from exc

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "evidence_type": self.evidence_type,
            "evidence_id": self.evidence_id,
            "checksum_sha256": self.checksum_sha256,
        }


@dataclass(frozen=True, slots=True)
class MetricThreshold:
    warning: float
    critical: float
    direction: MetricDirection

    def __post_init__(self) -> None:
        if not math.isfinite(self.warning) or not math.isfinite(self.critical):
            raise MonitoringError("monitoring thresholds must be finite")
        if self.direction is MetricDirection.HIGHER_IS_WORSE and self.warning > self.critical:
            raise MonitoringError("higher-is-worse thresholds must be ascending")
        if self.direction is MetricDirection.LOWER_IS_WORSE and self.warning < self.critical:
            raise MonitoringError("lower-is-worse thresholds must be descending")

    def status(self, value: float) -> MetricStatus:
        if not math.isfinite(value):
            return MetricStatus.UNKNOWN
        if self.direction is MetricDirection.HIGHER_IS_WORSE:
            if value >= self.critical:
                return MetricStatus.CRITICAL
            if value >= self.warning:
                return MetricStatus.WARNING
        else:
            if value <= self.critical:
                return MetricStatus.CRITICAL
            if value <= self.warning:
                return MetricStatus.WARNING
        return MetricStatus.HEALTHY

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "warning": self.warning,
            "critical": self.critical,
            "direction": self.direction.value,
        }


@dataclass(frozen=True, slots=True)
class MonitoringPolicy:
    policy_id: str
    policy_version: str
    thresholds: tuple[tuple[str, MetricThreshold], ...]

    def __post_init__(self) -> None:
        if not self.policy_id or not self.policy_version:
            raise MonitoringError("monitoring policy identity must be non-empty")
        names = [name for name, _ in self.thresholds]
        if len(names) != len(set(names)) or names != sorted(names):
            raise MonitoringError("monitoring thresholds must have unique deterministic names")

    def threshold_for(self, metric_name: str) -> MetricThreshold | None:
        return next((threshold for name, threshold in self.thresholds if name == metric_name), None)

    @property
    def configuration_id(self) -> str:
        return content_addressed_id(
            identity_type=self.policy_version,
            payload={
                "policy_id": self.policy_id,
                "thresholds": [
                    {"metric_name": name, **threshold.to_json()}
                    for name, threshold in self.thresholds
                ],
            },
        )


DEFAULT_MONITORING_POLICY: Final[MonitoringPolicy] = MonitoringPolicy(
    policy_id="local-operational-and-model-health",
    policy_version="monitoring-policy-v1",
    thresholds=tuple(
        sorted(
            {
                "artifact_failure_count": MetricThreshold(
                    1,
                    2,
                    MetricDirection.HIGHER_IS_WORSE,
                ),
                "missing_snapshot_count": MetricThreshold(
                    1,
                    2,
                    MetricDirection.HIGHER_IS_WORSE,
                ),
                "source_observation_age_hours": MetricThreshold(
                    24,
                    72,
                    MetricDirection.HIGHER_IS_WORSE,
                ),
                "unresolved_mapping_count": MetricThreshold(
                    1,
                    5,
                    MetricDirection.HIGHER_IS_WORSE,
                ),
                "missing_result_count": MetricThreshold(
                    1,
                    10,
                    MetricDirection.HIGHER_IS_WORSE,
                ),
                "settlement_backlog_count": MetricThreshold(
                    1,
                    10,
                    MetricDirection.HIGHER_IS_WORSE,
                ),
                "settlement_lag_hours": MetricThreshold(
                    12,
                    48,
                    MetricDirection.HIGHER_IS_WORSE,
                ),
                "result_coverage": MetricThreshold(
                    0.95,
                    0.80,
                    MetricDirection.LOWER_IS_WORSE,
                ),
                "settlement_coverage": MetricThreshold(
                    0.95,
                    0.80,
                    MetricDirection.LOWER_IS_WORSE,
                ),
                "probability_completeness": MetricThreshold(
                    1.0,
                    0.95,
                    MetricDirection.LOWER_IS_WORSE,
                ),
                "log_loss": MetricThreshold(0.90, 1.20, MetricDirection.HIGHER_IS_WORSE),
                "multiclass_brier_score": MetricThreshold(
                    0.70,
                    0.90,
                    MetricDirection.HIGHER_IS_WORSE,
                ),
                "calibration_error": MetricThreshold(
                    0.10,
                    0.20,
                    MetricDirection.HIGHER_IS_WORSE,
                ),
            }.items()
        )
    ),
)


@dataclass(frozen=True, slots=True)
class PerformanceObservation:
    """One completed-event multiclass forecast and analytical return."""

    observation_id: str
    probabilities: tuple[float, ...]
    actual_index: int
    settled: bool
    won: bool | None = None
    profit_units: float | None = None

    def __post_init__(self) -> None:
        if not self.observation_id or len(self.probabilities) < 2:
            raise MonitoringError("performance observation is incomplete")
        if not 0 <= self.actual_index < len(self.probabilities):
            raise MonitoringError("actual outcome index is outside probability space")
        if any(not math.isfinite(value) or value < 0 or value > 1 for value in self.probabilities):
            raise MonitoringError("performance probabilities must be finite in [0,1]")
        if not math.isclose(math.fsum(self.probabilities), 1.0, abs_tol=1e-9):
            raise MonitoringError("performance probabilities must sum to one")
        if self.profit_units is not None and not math.isfinite(self.profit_units):
            raise MonitoringError("profit units must be finite")


@dataclass(frozen=True, slots=True)
class MonitoringInputs:
    """Persisted evidence summary; absent values explicitly mean unavailable evidence."""

    evidence: tuple[EvidenceReference, ...]
    latest_source_observed_at_utc: datetime | None = None
    expected_snapshot_count: int | None = None
    valid_snapshot_count: int | None = None
    artifact_failure_count: int | None = None
    unresolved_mapping_count: int | None = None
    incomplete_market_count: int | None = None
    duplicate_identity_count: int | None = None
    expected_result_count: int | None = None
    available_result_count: int | None = None
    settlement_candidate_count: int | None = None
    settled_count: int | None = None
    oldest_unsettled_completion_utc: datetime | None = None
    prediction_count: int | None = None
    eligible_opportunity_count: int | None = None
    rejected_opportunity_count: int | None = None
    probability_complete_count: int | None = None
    quality_flag_failure_count: int | None = None
    calibration_error: float | None = None
    performance: tuple[PerformanceObservation, ...] = ()

    def __post_init__(self) -> None:
        if len({item.evidence_id for item in self.evidence}) != len(self.evidence):
            raise MonitoringError("monitoring inputs contain duplicate evidence identities")
        for field_name in (
            "expected_snapshot_count",
            "valid_snapshot_count",
            "artifact_failure_count",
            "unresolved_mapping_count",
            "incomplete_market_count",
            "duplicate_identity_count",
            "expected_result_count",
            "available_result_count",
            "settlement_candidate_count",
            "settled_count",
            "prediction_count",
            "eligible_opportunity_count",
            "rejected_opportunity_count",
            "probability_complete_count",
            "quality_flag_failure_count",
        ):
            value = getattr(self, field_name)
            if value is not None and (type(value) is not int or value < 0):
                raise MonitoringError(f"{field_name} must be a non-negative integer or absent")
        for actual, expected, label in (
            (self.valid_snapshot_count, self.expected_snapshot_count, "valid snapshots"),
            (self.available_result_count, self.expected_result_count, "available results"),
            (self.settled_count, self.settlement_candidate_count, "settled positions"),
            (self.probability_complete_count, self.prediction_count, "complete probabilities"),
        ):
            if actual is not None and expected is not None and actual > expected:
                raise MonitoringError(f"{label} exceed their expected population")
        observation_ids = [item.observation_id for item in self.performance]
        if len(observation_ids) != len(set(observation_ids)):
            raise MonitoringError("monitoring inputs contain duplicate performance observations")
        for item in self.performance:
            if item.settled and (item.won is None or item.profit_units is None):
                raise MonitoringError(
                    "settled performance observations require won and profit values"
                )
            if not item.settled and (item.won is not None or item.profit_units is not None):
                raise MonitoringError("unsettled performance observations cannot carry outcomes")


@dataclass(frozen=True, slots=True)
class MonitoringMetric:
    metric_id: str
    metric_name: str
    policy_id: str
    policy_version: str
    window_start_utc: datetime
    window_end_utc: datetime
    numerator: float | None
    denominator: float | None
    sample_size: int
    value: float | None
    status: MetricStatus
    threshold: MetricThreshold | None
    evidence: tuple[EvidenceReference, ...]

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "metric_id": self.metric_id,
            "metric_name": self.metric_name,
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "window_start_utc": format_utc_timestamp(self.window_start_utc),
            "window_end_utc": format_utc_timestamp(self.window_end_utc),
            "numerator": self.numerator,
            "denominator": self.denominator,
            "sample_size": self.sample_size,
            "value": self.value,
            "status": self.status.value,
            "threshold": None if self.threshold is None else self.threshold.to_json(),
            "evidence": [item.to_json() for item in self.evidence],
        }


@dataclass(frozen=True, slots=True)
class MonitoringFinding:
    finding_id: str
    metric_id: str
    metric_name: str
    status: MetricStatus
    reason_code: str

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "finding_id": self.finding_id,
            "metric_id": self.metric_id,
            "metric_name": self.metric_name,
            "status": self.status.value,
            "reason_code": self.reason_code,
        }


@dataclass(frozen=True, slots=True)
class MonitoringReport:
    run_id: str
    schema_version: str
    policy_id: str
    policy_version: str
    policy_configuration_id: str
    as_of_utc: datetime
    window_start_utc: datetime
    window_end_utc: datetime
    evidence: tuple[EvidenceReference, ...]
    metrics: tuple[MonitoringMetric, ...]
    findings: tuple[MonitoringFinding, ...]
    summary_status: MetricStatus
    counts_by_status: tuple[tuple[str, int], ...]
    incomplete_evidence_warnings: tuple[str, ...]

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "run_id": self.run_id,
            "schema_version": self.schema_version,
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "policy_configuration_id": self.policy_configuration_id,
            "as_of_utc": format_utc_timestamp(self.as_of_utc),
            "window_start_utc": format_utc_timestamp(self.window_start_utc),
            "window_end_utc": format_utc_timestamp(self.window_end_utc),
            "evidence": [item.to_json() for item in self.evidence],
            "metrics": [item.to_json() for item in self.metrics],
            "findings": [item.to_json() for item in self.findings],
            "summary_status": self.summary_status.value,
            "counts_by_status": [
                {"status": status, "count": count} for status, count in self.counts_by_status
            ],
            "incomplete_evidence_warnings": list(self.incomplete_evidence_warnings),
        }


def evaluate_monitoring(
    *,
    inputs: MonitoringInputs,
    policy: MonitoringPolicy,
    as_of_utc: datetime,
    window_start_utc: datetime,
    window_end_utc: datetime,
) -> MonitoringReport:
    """Evaluate persisted evidence without any network or hidden-clock access."""
    as_of = require_utc(as_of_utc, field_name="as_of_utc")
    start = require_utc(window_start_utc, field_name="window_start_utc")
    end = require_utc(window_end_utc, field_name="window_end_utc")
    if not start < end <= as_of:
        raise MonitoringError("monitoring window must satisfy start < end <= as_of")
    evidence = tuple(sorted(inputs.evidence))
    values: dict[str, tuple[float | None, float | None, float | None, int]] = {}
    source_age = (
        None
        if inputs.latest_source_observed_at_utc is None
        else (
            as_of
            - require_utc(
                inputs.latest_source_observed_at_utc,
                field_name="latest_source_observed_at_utc",
            )
        ).total_seconds()
        / 3600
    )
    if source_age is not None and source_age < 0:
        raise MonitoringError("source observation cannot be later than monitoring as-of")
    values["source_observation_age_hours"] = (
        source_age,
        None,
        None,
        0 if source_age is None else 1,
    )
    values["artifact_failure_count"] = _count(inputs.artifact_failure_count)
    missing_snapshots = (
        None
        if inputs.expected_snapshot_count is None or inputs.valid_snapshot_count is None
        else max(0, inputs.expected_snapshot_count - inputs.valid_snapshot_count)
    )
    values["missing_snapshot_count"] = _count(missing_snapshots)
    values["unresolved_mapping_count"] = _count(inputs.unresolved_mapping_count)
    values["incomplete_market_count"] = _count(inputs.incomplete_market_count)
    values["duplicate_identity_count"] = _count(inputs.duplicate_identity_count)
    missing_result = (
        None
        if inputs.expected_result_count is None or inputs.available_result_count is None
        else max(0, inputs.expected_result_count - inputs.available_result_count)
    )
    values["missing_result_count"] = _count(missing_result)
    result_coverage = _ratio(inputs.available_result_count, inputs.expected_result_count)
    values["result_coverage"] = result_coverage
    backlog = (
        None
        if inputs.settlement_candidate_count is None or inputs.settled_count is None
        else max(0, inputs.settlement_candidate_count - inputs.settled_count)
    )
    values["settlement_backlog_count"] = _count(backlog)
    lag = (
        None
        if inputs.oldest_unsettled_completion_utc is None
        else (
            as_of
            - require_utc(
                inputs.oldest_unsettled_completion_utc,
                field_name="oldest_unsettled_completion_utc",
            )
        ).total_seconds()
        / 3600
    )
    if lag is not None and lag < 0:
        raise MonitoringError("unsettled completion cannot be later than monitoring as-of")
    values["settlement_lag_hours"] = (lag, None, None, 0 if lag is None else 1)
    values["settlement_coverage"] = _ratio(inputs.settled_count, inputs.settlement_candidate_count)
    for name in (
        "prediction_count",
        "eligible_opportunity_count",
        "rejected_opportunity_count",
        "probability_complete_count",
        "quality_flag_failure_count",
    ):
        values[name] = _count(getattr(inputs, name))
    values["probability_completeness"] = _ratio(
        inputs.probability_complete_count,
        inputs.prediction_count,
    )
    completed = inputs.performance
    if completed:
        log_loss = math.fsum(
            -math.log(max(item.probabilities[item.actual_index], 1e-15)) for item in completed
        ) / len(completed)
        brier = math.fsum(
            math.fsum(
                (probability - (1.0 if index == item.actual_index else 0.0)) ** 2
                for index, probability in enumerate(item.probabilities)
            )
            for item in completed
        ) / len(completed)
        wins = [item for item in completed if item.settled and item.won is not None]
        profits = [
            item.profit_units
            for item in completed
            if item.settled and item.profit_units is not None
        ]
        values["log_loss"] = (log_loss, None, None, len(completed))
        values["multiclass_brier_score"] = (brier, None, None, len(completed))
        values["hit_rate"] = (
            None if not wins else sum(1 for item in wins if item.won) / len(wins),
            None if not wins else float(sum(1 for item in wins if item.won)),
            None if not wins else float(len(wins)),
            len(wins),
        )
        values["roi"] = (
            None
            if not profits
            else math.fsum(value for value in profits if value is not None) / len(profits),
            None if not profits else math.fsum(value for value in profits if value is not None),
            None if not profits else float(len(profits)),
            len(profits),
        )
    else:
        for name in ("log_loss", "multiclass_brier_score", "hit_rate", "roi"):
            values[name] = (None, None, None, 0)
    values["calibration_error"] = (
        inputs.calibration_error,
        None,
        None,
        len(completed) if inputs.calibration_error is not None else 0,
    )
    metrics = tuple(
        _metric(
            name=name,
            value=value,
            numerator=numerator,
            denominator=denominator,
            sample_size=sample_size,
            policy=policy,
            start=start,
            end=end,
            evidence=evidence,
        )
        for name, (value, numerator, denominator, sample_size) in sorted(values.items())
    )
    findings = tuple(
        _finding(metric) for metric in metrics if metric.status is not MetricStatus.HEALTHY
    )
    counts = tuple(
        (status.value, sum(1 for metric in metrics if metric.status is status))
        for status in MetricStatus
    )
    summary = _summary(metric.status for metric in metrics)
    warnings = tuple(
        f"missing-evidence:{metric.metric_name}"
        for metric in metrics
        if metric.status is MetricStatus.UNKNOWN
    )
    run_id = content_addressed_id(
        identity_type=MONITORING_REPORT_SCHEMA_VERSION,
        payload={
            "policy_id": policy.policy_id,
            "policy_version": policy.policy_version,
            "policy_configuration_id": policy.configuration_id,
            "as_of_utc": format_utc_timestamp(as_of),
            "window_start_utc": format_utc_timestamp(start),
            "window_end_utc": format_utc_timestamp(end),
            "evidence": [item.to_json() for item in evidence],
            "metric_ids": [item.metric_id for item in metrics],
        },
    )
    return MonitoringReport(
        run_id=run_id,
        schema_version=MONITORING_REPORT_SCHEMA_VERSION,
        policy_id=policy.policy_id,
        policy_version=policy.policy_version,
        policy_configuration_id=policy.configuration_id,
        as_of_utc=as_of,
        window_start_utc=start,
        window_end_utc=end,
        evidence=evidence,
        metrics=metrics,
        findings=findings,
        summary_status=summary,
        counts_by_status=counts,
        incomplete_evidence_warnings=warnings,
    )


def _metric(
    *,
    name: str,
    value: float | None,
    numerator: float | None,
    denominator: float | None,
    sample_size: int,
    policy: MonitoringPolicy,
    start: datetime,
    end: datetime,
    evidence: tuple[EvidenceReference, ...],
) -> MonitoringMetric:
    threshold = policy.threshold_for(name)
    status = (
        MetricStatus.UNKNOWN
        if value is None
        else MetricStatus.UNKNOWN
        if threshold is None
        and name
        in {
            "artifact_failure_count",
            "incomplete_market_count",
            "duplicate_identity_count",
            "quality_flag_failure_count",
        }
        and value > 0
        else MetricStatus.HEALTHY
        if threshold is None
        else threshold.status(value)
    )
    payload: dict[str, JsonValue] = {
        "metric_name": name,
        "policy_id": policy.policy_id,
        "policy_version": policy.policy_version,
        "window_start_utc": format_utc_timestamp(start),
        "window_end_utc": format_utc_timestamp(end),
        "numerator": numerator,
        "denominator": denominator,
        "sample_size": sample_size,
        "value": value,
        "status": status.value,
        "threshold": None if threshold is None else threshold.to_json(),
        "evidence": [item.to_json() for item in evidence],
    }
    return MonitoringMetric(
        metric_id=content_addressed_id(identity_type="monitoring-metric-v1", payload=payload),
        metric_name=name,
        policy_id=policy.policy_id,
        policy_version=policy.policy_version,
        window_start_utc=start,
        window_end_utc=end,
        numerator=numerator,
        denominator=denominator,
        sample_size=sample_size,
        value=value,
        status=status,
        threshold=threshold,
        evidence=evidence,
    )


def _finding(metric: MonitoringMetric) -> MonitoringFinding:
    reason = (
        "missing-evidence"
        if metric.status is MetricStatus.UNKNOWN
        else f"threshold-{metric.status.value}"
    )
    finding_id = content_addressed_id(
        identity_type="monitoring-finding-v1",
        payload={
            "metric_id": metric.metric_id,
            "status": metric.status.value,
            "reason_code": reason,
        },
    )
    return MonitoringFinding(
        finding_id, metric.metric_id, metric.metric_name, metric.status, reason
    )


def _ratio(
    numerator: int | None, denominator: int | None
) -> tuple[float | None, float | None, float | None, int]:
    if numerator is None or denominator is None or denominator == 0:
        return (
            None,
            None if numerator is None else float(numerator),
            None if denominator is None else float(denominator),
            0,
        )
    return numerator / denominator, float(numerator), float(denominator), denominator


def _count(value: int | None) -> tuple[float | None, float | None, float | None, int]:
    return (None, None, None, 0) if value is None else (float(value), float(value), 1.0, value)


def _summary(statuses: Iterable[MetricStatus]) -> MetricStatus:
    values = set(statuses)
    for status in (
        MetricStatus.CRITICAL,
        MetricStatus.WARNING,
        MetricStatus.UNKNOWN,
        MetricStatus.HEALTHY,
    ):
        if status in values:
            return status
    return MetricStatus.UNKNOWN
