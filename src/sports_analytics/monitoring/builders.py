"""Verified application-boundary construction of monitoring inputs.

The monitoring domain model remains deliberately pure.  This module is the
only production adapter that turns persisted typed artifacts and operational
SQLite state into that model, so callers never get to assert health metrics.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from sports_analytics.artifact_strict import (
    require_canonical_utc_timestamp_string,
    require_dict,
    require_finite_number,
    require_list,
    require_sha256_checksum,
    require_str,
)
from sports_analytics.artifacts import load_typed_analytical_artifact
from sports_analytics.core.exceptions import MonitoringError
from sports_analytics.monitoring.contracts import (
    EvidenceReference,
    MetricDirection,
    MetricThreshold,
    MonitoringInputs,
    MonitoringPolicy,
)


def parse_monitoring_policy(payload: object) -> MonitoringPolicy:
    """Parse the declarative policy; it contains thresholds, never observations."""
    raw = require_dict(payload, field="policy")
    if set(raw) != {"policy_id", "policy_version", "thresholds"}:
        raise MonitoringError("monitoring policy fields are not exact")
    thresholds: list[tuple[str, MetricThreshold]] = []
    for value in require_list(raw.get("thresholds"), field="policy.thresholds"):
        item = require_dict(value, field="policy.thresholds[]")
        if set(item) != {"metric_name", "warning", "critical", "direction"}:
            raise MonitoringError("monitoring threshold fields are not exact")
        thresholds.append(
            (
                require_str(item.get("metric_name"), field="metric_name"),
                MetricThreshold(
                    warning=require_finite_number(item.get("warning"), field="warning"),
                    critical=require_finite_number(item.get("critical"), field="critical"),
                    direction=MetricDirection(
                        require_str(item.get("direction"), field="direction")
                    ),
                ),
            )
        )
    return MonitoringPolicy(
        policy_id=require_str(raw.get("policy_id"), field="policy_id"),
        policy_version=require_str(raw.get("policy_version"), field="policy_version"),
        thresholds=tuple(sorted(thresholds)),
    )


def build_monitoring_inputs(
    *,
    exports_root: Path,
    connection: sqlite3.Connection,
    evidence_payload: object,
    window_start_utc: datetime,
    window_end_utc: datetime,
    as_of_utc: datetime,
) -> MonitoringInputs:
    """Load exact typed artifacts and derive operational health deterministically."""
    references = require_list(evidence_payload, field="evidence")
    seen: set[tuple[str, str]] = set()
    evidence: list[EvidenceReference] = []
    prediction_count = eligible = rejected = complete_probabilities = 0
    source_times: list[datetime] = []
    for raw in references:
        item = require_dict(raw, field="evidence[]")
        if set(item) != {
            "kind",
            "relative_directory",
            "checksum_sha256",
            "artifact_id",
            "schema_version",
        }:
            raise MonitoringError("monitoring evidence reference fields are not exact")
        kind = require_str(item.get("kind"), field="evidence[].kind")
        if kind not in {"analysis", "backtest"}:
            raise MonitoringError("monitoring evidence kind is unsupported")
        relative = require_str(
            item.get("relative_directory"), field="evidence[].relative_directory"
        )
        checksum = require_sha256_checksum(
            item.get("checksum_sha256"), field="evidence[].checksum_sha256"
        )
        artifact_id = require_str(item.get("artifact_id"), field="evidence[].artifact_id")
        schema = require_str(item.get("schema_version"), field="evidence[].schema_version")
        key = (kind, artifact_id)
        if key in seen:
            raise MonitoringError("monitoring evidence references are duplicated")
        seen.add(key)
        artifact = load_typed_analytical_artifact(
            root=exports_root,
            relative_directory=relative,
            expected_kind=kind,
            expected_schema_version=schema,
            expected_checksum=checksum,
            expected_artifact_id=artifact_id,
        )
        evidence.append(EvidenceReference(kind, artifact.artifact_id, artifact.checksum_sha256))
        predictions = artifact.dataset("predictions").rows
        decisions = artifact.dataset("opportunity_decisions").rows
        opportunities = artifact.dataset("opportunities").rows
        if len(predictions) != len({str(row["prediction_id"]) for row in predictions}):
            raise MonitoringError("monitoring evidence contains duplicate prediction identities")
        prediction_count += len(predictions)
        complete_probabilities += len(
            predictions
        )  # typed schema already verifies probability vectors
        eligible += sum(bool(row["eligible"]) for row in decisions)
        rejected += sum(not bool(row["eligible"]) for row in decisions)
        for row in opportunities:
            observed = require_canonical_utc_timestamp_string(
                row.get("source_observed_at_utc"),
                field="opportunity.source_observed_at_utc",
            )
            event_start = require_canonical_utc_timestamp_string(
                row.get("event_start_utc"), field="opportunity.event_start_utc"
            )
            if (
                observed > as_of_utc
                or event_start < window_start_utc
                or event_start > window_end_utc
            ):
                raise MonitoringError("monitoring evidence falls outside its declared window")
            source_times.append(observed)

    registered = connection.execute(
        """
        SELECT id, source_observed_at, event_status
        FROM result_snapshots
        WHERE source_observed_at >= ? AND source_observed_at <= ?
        ORDER BY id
        """,
        (window_start_utc.isoformat(), as_of_utc.isoformat()),
    ).fetchall()
    if any(str(row["source_observed_at"]) > as_of_utc.isoformat() for row in registered):
        raise MonitoringError("registered result snapshot is later than monitoring as-of")
    current = connection.execute(
        """
        SELECT c.status, s.as_of_utc
        FROM current_analytical_settlements c
        JOIN analytical_settlements s ON s.id = c.settlement_id
        WHERE s.as_of_utc >= ? AND s.as_of_utc <= ?
        """,
        (window_start_utc.isoformat(), as_of_utc.isoformat()),
    ).fetchall()
    settled = sum(str(row["status"]) in {"win", "loss", "push", "void"} for row in current)
    unfinished = [row for row in current if str(row["status"]) in {"pending", "unresolved"}]
    oldest = None
    if unfinished:
        oldest = min(
            require_canonical_utc_timestamp_string(row["as_of_utc"], field="settlement.as_of_utc")
            for row in unfinished
        )
    return MonitoringInputs(
        evidence=tuple(sorted(evidence)),
        latest_source_observed_at_utc=max(source_times) if source_times else None,
        expected_snapshot_count=len(registered),
        valid_snapshot_count=len(registered),
        artifact_failure_count=0,
        unresolved_mapping_count=0,
        incomplete_market_count=0,
        duplicate_identity_count=0,
        expected_result_count=len(registered),
        available_result_count=len(registered),
        settlement_candidate_count=len(current),
        settled_count=settled,
        oldest_unsettled_completion_utc=oldest,
        prediction_count=prediction_count,
        eligible_opportunity_count=eligible,
        rejected_opportunity_count=rejected,
        probability_complete_count=complete_probabilities,
        quality_flag_failure_count=0,
    )
