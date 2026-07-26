"""Immutable monitoring report artifact publication and strict verification."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Final, cast

from sports_analytics.artifact_strict import (
    require_canonical_utc_timestamp_string,
    require_dict,
    require_finite_number,
    require_int,
    require_list,
    require_str,
)
from sports_analytics.artifacts import (
    AnalyticalArtifact,
    load_analytical_artifact,
    write_analytical_artifact,
)
from sports_analytics.core.exceptions import ArtifactError, MonitoringError
from sports_analytics.data.codec import format_utc_timestamp
from sports_analytics.models.identity import content_addressed_id
from sports_analytics.monitoring.contracts import (
    MONITORING_REPORT_SCHEMA_VERSION,
    EvidenceReference,
    MetricDirection,
    MetricStatus,
    MetricThreshold,
    MonitoringFinding,
    MonitoringMetric,
    MonitoringPolicy,
    MonitoringReport,
    _finding,
    _metric,
    _summary,
)

MONITORING_ARTIFACT_TYPE: Final[str] = "operational-monitoring-report"


class MonitoringReportTrust(StrEnum):
    """Explicit trust boundary for a reconstructed monitoring report."""

    INTERNALLY_CONSISTENT = "internally_consistent"
    EXTERNALLY_VERIFIED = "externally_verified"


@dataclass(frozen=True, slots=True)
class VerifiedMonitoringReport:
    """A strict artifact envelope paired with its parsed monitoring report."""

    artifact: AnalyticalArtifact
    report: MonitoringReport

    @property
    def artifact_id(self) -> str:
        return self.artifact.artifact_id

    @property
    def checksum_sha256(self) -> str:
        return self.artifact.checksum_sha256


def publish_monitoring_report(
    *,
    root: Path,
    relative_directory: str,
    report: MonitoringReport,
) -> AnalyticalArtifact:
    payload = report.to_json()
    try:
        return write_analytical_artifact(
            root=root,
            relative_directory=relative_directory,
            artifact_type=MONITORING_ARTIFACT_TYPE,
            schema_version=MONITORING_REPORT_SCHEMA_VERSION,
            payload=payload,
        )
    except ArtifactError:
        existing = load_monitoring_report(
            root=root,
            relative_directory=relative_directory,
            expected_checksum=None,
            expected_run_id=report.run_id,
        )
        if existing.artifact.payload != payload:
            raise MonitoringError("existing monitoring report conflicts with replay") from None
        return existing.artifact


def load_monitoring_report(
    *,
    root: Path,
    relative_directory: str,
    expected_checksum: str | None = None,
    expected_run_id: str | None = None,
) -> VerifiedMonitoringReport:
    try:
        artifact = load_analytical_artifact(
            root=root,
            relative_directory=relative_directory,
            expected_artifact_type=MONITORING_ARTIFACT_TYPE,
            expected_schema_version=MONITORING_REPORT_SCHEMA_VERSION,
            expected_checksum=expected_checksum,
        )
    except ArtifactError as exc:
        raise MonitoringError(f"monitoring report verification failed: {exc}") from exc
    if not isinstance(artifact.payload, dict):
        raise MonitoringError("monitoring report payload must be an object")
    exact = {
        "run_id",
        "schema_version",
        "policy_id",
        "policy_version",
        "policy_configuration_id",
        "as_of_utc",
        "window_start_utc",
        "window_end_utc",
        "evidence",
        "metrics",
        "findings",
        "summary_status",
        "counts_by_status",
        "incomplete_evidence_warnings",
    }
    if set(artifact.payload) != exact:
        raise MonitoringError("monitoring report fields are not exact")
    if artifact.payload["schema_version"] != MONITORING_REPORT_SCHEMA_VERSION:
        raise MonitoringError("monitoring report schema version mismatch")
    report = _verify_report_payload(artifact.payload)
    if expected_run_id is not None and report.run_id != expected_run_id:
        raise MonitoringError("monitoring report run identity does not match expected run")
    return VerifiedMonitoringReport(artifact=artifact, report=report)


def verify_monitoring_report_trust(
    *,
    root: Path,
    relative_directory: str,
    expected_checksum: str | None = None,
    expected_run_id: str | None = None,
) -> tuple[VerifiedMonitoringReport, MonitoringReportTrust]:
    """Classify a report as internally consistent or externally verified.

    Nested content addressing alone does not prevent complete malicious rewriting
    of all identities. External verification therefore requires an expected
    checksum supplied by the caller.
    """
    verified = load_monitoring_report(
        root=root,
        relative_directory=relative_directory,
        expected_checksum=expected_checksum,
        expected_run_id=expected_run_id,
    )
    if expected_checksum is None:
        return verified, MonitoringReportTrust.INTERNALLY_CONSISTENT
    return verified, MonitoringReportTrust.EXTERNALLY_VERIFIED


def _verify_report_payload(payload: dict) -> MonitoringReport:
    """Reconstruct report semantics instead of trusting a checksummed JSON blob."""
    try:
        as_of = _canonical_time(payload.get("as_of_utc"), "as_of_utc")
        start = _canonical_time(payload.get("window_start_utc"), "window_start_utc")
        end = _canonical_time(payload.get("window_end_utc"), "window_end_utc")
        if not start < end <= as_of:
            raise MonitoringError("monitoring report window is invalid")
        evidence = tuple(
            _evidence(item) for item in require_list(payload.get("evidence"), field="evidence")
        )
        if evidence != tuple(sorted(evidence)) or len(set(evidence)) != len(evidence):
            raise MonitoringError("monitoring report evidence ordering is invalid")
        metrics: tuple[MonitoringMetric, ...] = tuple(
            _metric_from_json(item, start, end, evidence)
            for item in require_list(payload.get("metrics"), field="metrics")
        )
        if metrics != tuple(sorted(metrics, key=lambda item: item.metric_name)):
            raise MonitoringError("monitoring report metrics are not uniquely ordered")
        if len({item.metric_name for item in metrics}) != len(metrics):
            raise MonitoringError("monitoring report metrics are duplicated")
        thresholds = tuple(
            sorted(
                (item.metric_name, item.threshold) for item in metrics if item.threshold is not None
            )
        )
        policy = MonitoringPolicy(
            policy_id=require_str(payload.get("policy_id"), field="policy_id"),
            policy_version=require_str(payload.get("policy_version"), field="policy_version"),
            thresholds=thresholds,
        )
        if payload.get("policy_configuration_id") != policy.configuration_id:
            raise MonitoringError("monitoring report policy configuration identity is invalid")
        if any(
            item.policy_id != policy.policy_id or item.policy_version != policy.policy_version
            for item in metrics
        ):
            raise MonitoringError("monitoring report metric policy identity is invalid")
        expected_findings = tuple(
            _finding(item) for item in metrics if item.status is not MetricStatus.HEALTHY
        )
        findings: tuple[MonitoringFinding, ...] = tuple(
            _finding_from_json(item, metrics)
            for item in require_list(payload.get("findings"), field="findings")
        )
        if findings != expected_findings:
            raise MonitoringError("monitoring report findings do not exactly match metrics")
        expected_counts = tuple(
            (status.value, sum(item.status is status for item in metrics))
            for status in MetricStatus
        )
        counts_raw = require_list(payload.get("counts_by_status"), field="counts_by_status")
        counts = tuple(
            (
                require_str(
                    require_dict(item, field="counts_by_status[]").get("status"), field="status"
                ),
                require_int(
                    require_dict(item, field="counts_by_status[]").get("count"), field="count"
                ),
            )
            for item in counts_raw
        )
        if counts != expected_counts:
            raise MonitoringError("monitoring report status counts are invalid")
        summary = _summary(item.status for item in metrics)
        if payload.get("summary_status") != summary.value:
            raise MonitoringError("monitoring report summary status is invalid")
        warnings = tuple(
            require_str(item, field="incomplete_evidence_warnings[]")
            for item in require_list(
                payload.get("incomplete_evidence_warnings"), field="incomplete_evidence_warnings"
            )
        )
        expected_warnings = tuple(
            f"missing-evidence:{item.metric_name}"
            for item in metrics
            if item.status is MetricStatus.UNKNOWN
        )
        if warnings != expected_warnings:
            raise MonitoringError("monitoring report incomplete-evidence warnings are invalid")
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
        if payload.get("run_id") != run_id:
            raise MonitoringError("monitoring report run identity is invalid")
        return MonitoringReport(
            run_id,
            MONITORING_REPORT_SCHEMA_VERSION,
            policy.policy_id,
            policy.policy_version,
            policy.configuration_id,
            as_of,
            start,
            end,
            evidence,
            metrics,
            findings,
            summary,
            counts,
            warnings,
        )
    except (TypeError, ValueError, KeyError) as exc:
        raise MonitoringError("monitoring report semantic verification failed") from exc


def _canonical_time(value: object, field: str) -> datetime:
    parsed = require_canonical_utc_timestamp_string(value, field=field)
    if require_str(value, field=field) != format_utc_timestamp(parsed):
        raise MonitoringError(f"{field} is not canonical")
    return parsed


def _evidence(value: object) -> EvidenceReference:
    raw = require_dict(value, field="evidence[]")
    if set(raw) != {"evidence_type", "evidence_id", "checksum_sha256"}:
        raise MonitoringError("monitoring report evidence fields are not exact")
    return EvidenceReference(
        require_str(raw.get("evidence_type"), field="evidence_type"),
        require_str(raw.get("evidence_id"), field="evidence_id"),
        require_str(raw.get("checksum_sha256"), field="checksum_sha256"),
    )


def _metric_from_json(
    value: object,
    start: datetime,
    end: datetime,
    evidence: tuple[EvidenceReference, ...],
) -> MonitoringMetric:
    raw = require_dict(value, field="metrics[]")
    if set(raw) != {
        "metric_id",
        "metric_name",
        "policy_id",
        "policy_version",
        "window_start_utc",
        "window_end_utc",
        "numerator",
        "denominator",
        "sample_size",
        "value",
        "status",
        "threshold",
        "evidence",
    }:
        raise MonitoringError("monitoring report metric fields are not exact")
    if (
        _canonical_time(raw.get("window_start_utc"), "metric.window_start_utc") != start
        or _canonical_time(raw.get("window_end_utc"), "metric.window_end_utc") != end
    ):
        raise MonitoringError("monitoring report metric window is inconsistent")
    metric_evidence = tuple(
        _evidence(item) for item in require_list(raw.get("evidence"), field="metric.evidence")
    )
    if metric_evidence != evidence:
        raise MonitoringError("monitoring report metric evidence is inconsistent")
    threshold_raw = raw.get("threshold")
    threshold = None
    if threshold_raw is not None:
        threshold_dict = require_dict(threshold_raw, field="threshold")
        if set(threshold_dict) != {"warning", "critical", "direction"}:
            raise MonitoringError("monitoring report threshold fields are not exact")
        threshold = MetricThreshold(
            _json_number(threshold_dict.get("warning"), "warning"),
            _json_number(threshold_dict.get("critical"), "critical"),
            MetricDirection(require_str(threshold_dict.get("direction"), field="direction")),
        )

    def number(field: str) -> float | None:
        candidate = raw.get(field)
        return None if candidate is None else require_finite_number(candidate, field=field)

    numerator, denominator, metric_value = (
        number("numerator"),
        number("denominator"),
        number("value"),
    )
    sample = require_int(raw.get("sample_size"), field="sample_size")
    if (
        sample < 0
        or (denominator is not None and denominator < 0)
        or (numerator is not None and numerator < 0)
    ):
        raise MonitoringError("monitoring report metric population is invalid")
    if denominator is not None:
        if (
            denominator == 0
            or numerator is None
            or metric_value is None
            or not math.isclose(metric_value, numerator / denominator, abs_tol=1e-12)
        ):
            raise MonitoringError("monitoring report metric ratio is invalid")
        if raw.get("metric_name") in {
            "result_coverage",
            "settlement_coverage",
            "probability_completeness",
        } and sample != int(denominator):
            raise MonitoringError("monitoring report metric sample size is inconsistent")
    status = MetricStatus(require_str(raw.get("status"), field="status"))
    name = require_str(raw.get("metric_name"), field="metric_name")
    policy = MonitoringPolicy(
        require_str(raw.get("policy_id"), field="policy_id"),
        require_str(raw.get("policy_version"), field="policy_version"),
        ((name, threshold),) if threshold else (),
    )
    reconstructed = _metric(
        name=name,
        value=metric_value,
        numerator=numerator,
        denominator=denominator,
        sample_size=sample,
        policy=policy,
        start=start,
        end=end,
        evidence=evidence,
    )
    if (
        reconstructed.status is not status
        or reconstructed.metric_id != raw.get("metric_id")
        or reconstructed.threshold != threshold
    ):
        raise MonitoringError("monitoring report metric identity or status is invalid")
    return reconstructed


def _finding_from_json(value: object, metrics: tuple[MonitoringMetric, ...]) -> MonitoringFinding:
    raw = require_dict(value, field="findings[]")
    if set(raw) != {"finding_id", "metric_id", "metric_name", "status", "reason_code"}:
        raise MonitoringError("monitoring report finding fields are not exact")
    matching = [item for item in metrics if item.metric_id == raw.get("metric_id")]
    if len(matching) != 1:
        raise MonitoringError("monitoring report finding metric is missing")
    expected = _finding(matching[0])
    if expected.to_json() != raw:
        raise MonitoringError("monitoring report finding identity is invalid")
    return expected


def _json_number(value: object, field: str) -> int | float:
    require_finite_number(value, field=field)
    if type(value) is int:
        return value
    return float(cast(float, value))
