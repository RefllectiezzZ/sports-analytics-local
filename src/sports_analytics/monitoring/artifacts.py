"""Immutable monitoring report artifact publication and strict verification."""

from __future__ import annotations

from pathlib import Path
from typing import Final

from sports_analytics.artifacts import (
    AnalyticalArtifact,
    load_analytical_artifact,
    write_analytical_artifact,
)
from sports_analytics.core.exceptions import ArtifactError, MonitoringError
from sports_analytics.monitoring.contracts import (
    MONITORING_REPORT_SCHEMA_VERSION,
    MonitoringReport,
)

MONITORING_ARTIFACT_TYPE: Final[str] = "operational-monitoring-report"


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
        )
        if existing.payload != payload:
            raise MonitoringError("existing monitoring report conflicts with replay") from None
        return existing


def load_monitoring_report(
    *,
    root: Path,
    relative_directory: str,
    expected_checksum: str | None = None,
) -> AnalyticalArtifact:
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
    return artifact
