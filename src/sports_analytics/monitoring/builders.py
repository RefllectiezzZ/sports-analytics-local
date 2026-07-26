"""Verified application-boundary construction of monitoring inputs.

The monitoring domain model remains deliberately pure.  This module is the
only production adapter that turns persisted typed artifacts and operational
SQLite state into that model, so callers never get to assert health metrics.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sports_analytics.artifact_strict import (
    require_bool,
    require_canonical_utc_timestamp_string,
    require_decimal_string,
    require_dict,
    require_finite_number,
    require_int,
    require_list,
    require_sha256_checksum,
    require_str,
    require_str_list,
)
from sports_analytics.artifacts import TypedAnalyticalArtifact, load_typed_analytical_artifact
from sports_analytics.core.exceptions import MonitoringError, ResultError
from sports_analytics.data.codec import format_utc_timestamp
from sports_analytics.monitoring.contracts import (
    EvidenceReference,
    MetricDirection,
    MetricThreshold,
    MonitoringInputs,
    MonitoringPolicy,
    PerformanceObservation,
    VerifiedAggregatePerformance,
)
from sports_analytics.results.contracts import EventResultStatus
from sports_analytics.results.snapshots import VerifiedResultSnapshot, load_result_snapshot

_FINAL_SETTLEMENT_STATUSES = frozenset({"win", "loss", "push", "void"})
_QUALITY_FLAG_FIELDS = (
    "calibrated",
    "model_artifact_verified",
    "feature_artifact_verified",
    "sufficient_history",
    "data_quality_passed",
)


@dataclass(frozen=True, slots=True)
class _RegisteredSnapshotMetadata:
    snapshot_id: str
    relative_path: str
    checksum_sha256: str
    canonical_event_id: str
    event_status: str
    source_observed_at_utc: datetime


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
    snapshots_root: Path,
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
    admitted: list[TypedAnalyticalArtifact] = []
    prediction_count = eligible = rejected = complete_probabilities = 0
    quality_flag_failure_count = 0
    source_times: list[datetime] = []
    analytical_event_ids: set[str] = set()
    position_keys: set[tuple[str, str, str]] = set()

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
        admitted.append(artifact)
        predictions = artifact.dataset("predictions").rows
        decisions = artifact.dataset("opportunity_decisions").rows
        opportunities = artifact.dataset("opportunities").rows
        if len(predictions) != len({str(row["prediction_id"]) for row in predictions}):
            raise MonitoringError("monitoring evidence contains duplicate prediction identities")
        prediction_count += len(predictions)
        complete_probabilities += len(predictions)
        eligible += sum(bool(row["eligible"]) for row in decisions)
        rejected += sum(not bool(row["eligible"]) for row in decisions)
        for row in predictions:
            event_id = require_str(row.get("canonical_event_id"), field="canonical_event_id")
            analytical_event_ids.add(event_id)
            quality = require_dict(row.get("quality"), field="quality")
            if not all(
                require_bool(quality.get(field), field=f"quality.{field}")
                for field in _QUALITY_FLAG_FIELDS
            ):
                quality_flag_failure_count += 1
        opportunity_ids = {
            require_str(row.get("opportunity_id"), field="opportunity_id") for row in opportunities
        }
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
            event_id = require_str(row.get("canonical_event_id"), field="canonical_event_id")
            analytical_event_ids.add(event_id)
            position = (
                artifact.artifact_id,
                "single",
                require_str(row.get("opportunity_id"), field="opportunity_id"),
            )
            if position in position_keys:
                raise MonitoringError("monitoring evidence contains duplicate position identities")
            position_keys.add(position)
        for row in artifact.dataset("combinations").rows:
            combination_id = require_str(row.get("combination_id"), field="combination_id")
            legs = tuple(require_str_list(row.get("opportunity_ids"), field="opportunity_ids"))
            if any(opportunity_id not in opportunity_ids for opportunity_id in legs):
                raise MonitoringError("combination references a missing persisted opportunity")
            position = (artifact.artifact_id, "combination", combination_id)
            if position in position_keys:
                raise MonitoringError("monitoring evidence contains duplicate position identities")
            position_keys.add(position)

    window_start_text = format_utc_timestamp(window_start_utc)
    as_of_text = format_utc_timestamp(as_of_utc)
    registered = connection.execute(
        """
        SELECT id, relative_path, checksum_sha256, canonical_event_id,
               event_status, source_observed_at
        FROM result_snapshots
        WHERE source_observed_at >= ? AND source_observed_at <= ?
        ORDER BY id
        """,
        (window_start_text, as_of_text),
    ).fetchall()
    expected_snapshot_count = len(registered)
    valid_snapshots: list[VerifiedResultSnapshot] = []
    artifact_failure_count = 0
    verified_by_event: dict[str, VerifiedResultSnapshot] = {}
    for row in registered:
        registration = _registration_metadata(row)
        if registration.source_observed_at_utc > as_of_utc:
            raise MonitoringError("registered result snapshot is later than monitoring as-of")
        try:
            snapshot = load_result_snapshot(
                root=snapshots_root,
                relative_directory=registration.relative_path,
                expected_checksum=registration.checksum_sha256,
                expected_snapshot_id=registration.snapshot_id,
            )
            _compare_registration_to_snapshot(registration, snapshot)
        except (ResultError, MonitoringError, OSError, ValueError, TypeError):
            artifact_failure_count += 1
            continue
        valid_snapshots.append(snapshot)
        if snapshot.result.event_status is EventResultStatus.COMPLETED:
            event_id = snapshot.result.canonical_event_id
            existing = verified_by_event.get(event_id)
            if existing is not None and existing.snapshot_id != snapshot.snapshot_id:
                raise MonitoringError(
                    "verified completed result snapshots conflict for one canonical event"
                )
            verified_by_event[event_id] = snapshot

    expected_result_count = len(analytical_event_ids)
    available_result_count = sum(
        1 for event_id in analytical_event_ids if event_id in verified_by_event
    )
    settlement_candidate_count = len(position_keys)
    settled_count = 0
    unfinished_as_of: list[datetime] = []
    if position_keys:
        current_rows = connection.execute(
            """
            SELECT c.source_artifact_id, c.position_type, c.position_id,
                   c.status, s.as_of_utc
            FROM current_analytical_settlements c
            JOIN analytical_settlements s ON s.id = c.settlement_id
            """
        ).fetchall()
        current_by_key: dict[tuple[str, str, str], sqlite3.Row] = {
            (
                str(row["source_artifact_id"]),
                str(row["position_type"]),
                str(row["position_id"]),
            ): row
            for row in current_rows
        }
        for position_key in sorted(position_keys):
            current = current_by_key.get(position_key)
            if current is None:
                continue
            status = str(current["status"])
            if status in _FINAL_SETTLEMENT_STATUSES:
                settled_count += 1
            elif status in {"pending", "unresolved"}:
                unfinished_as_of.append(
                    require_canonical_utc_timestamp_string(
                        current["as_of_utc"], field="settlement.as_of_utc"
                    )
                )

    performance, calibration_error, aggregate_performance = _derive_performance(
        admitted=admitted,
        verified_by_event=verified_by_event,
    )
    return MonitoringInputs(
        evidence=tuple(sorted(evidence)),
        latest_source_observed_at_utc=max(source_times) if source_times else None,
        expected_snapshot_count=expected_snapshot_count,
        valid_snapshot_count=len(valid_snapshots),
        artifact_failure_count=artifact_failure_count,
        unresolved_mapping_count=None,
        incomplete_market_count=None,
        duplicate_identity_count=0,
        expected_result_count=expected_result_count,
        available_result_count=available_result_count,
        settlement_candidate_count=settlement_candidate_count,
        settled_count=settled_count,
        oldest_unsettled_completion_utc=(min(unfinished_as_of) if unfinished_as_of else None),
        prediction_count=prediction_count,
        eligible_opportunity_count=eligible,
        rejected_opportunity_count=rejected,
        probability_complete_count=complete_probabilities,
        quality_flag_failure_count=quality_flag_failure_count,
        calibration_error=calibration_error,
        performance=performance,
        aggregate_performance=aggregate_performance,
    )


def _registration_metadata(row: sqlite3.Row) -> _RegisteredSnapshotMetadata:
    required = (
        "id",
        "relative_path",
        "checksum_sha256",
        "canonical_event_id",
        "event_status",
        "source_observed_at",
    )
    try:
        values = {name: row[name] for name in required}
    except (KeyError, IndexError) as exc:
        raise MonitoringError("result snapshot registration metadata is malformed") from exc
    if any(values[name] is None or values[name] == "" for name in required):
        raise MonitoringError("result snapshot registration metadata is incomplete")
    try:
        return _RegisteredSnapshotMetadata(
            snapshot_id=require_sha256_checksum(values["id"], field="result_snapshots.id"),
            relative_path=require_str(values["relative_path"], field="relative_path"),
            checksum_sha256=require_sha256_checksum(
                values["checksum_sha256"], field="checksum_sha256"
            ),
            canonical_event_id=require_str(
                values["canonical_event_id"], field="canonical_event_id"
            ),
            event_status=require_str(values["event_status"], field="event_status"),
            source_observed_at_utc=require_canonical_utc_timestamp_string(
                values["source_observed_at"],
                field="source_observed_at",
            ),
        )
    except (TypeError, ValueError) as exc:
        raise MonitoringError("result snapshot registration metadata is contradictory") from exc


def _compare_registration_to_snapshot(
    registration: _RegisteredSnapshotMetadata,
    snapshot: VerifiedResultSnapshot,
) -> None:
    if snapshot.snapshot_id != registration.snapshot_id:
        raise MonitoringError("result snapshot identity conflicts with registration")
    if snapshot.checksum_sha256 != registration.checksum_sha256:
        raise MonitoringError("result snapshot checksum conflicts with registration")
    if snapshot.relative_directory != registration.relative_path:
        raise MonitoringError("result snapshot path conflicts with registration")
    if snapshot.result.canonical_event_id != registration.canonical_event_id:
        raise MonitoringError("result snapshot event identity conflicts with registration")
    if snapshot.result.event_status.value != registration.event_status:
        raise MonitoringError("result snapshot status conflicts with registration")
    if snapshot.result.source_observed_at_utc != registration.source_observed_at_utc:
        raise MonitoringError("result snapshot observation time conflicts with registration")


def _derive_performance(
    *,
    admitted: list[TypedAnalyticalArtifact],
    verified_by_event: dict[str, VerifiedResultSnapshot],
) -> tuple[
    tuple[PerformanceObservation, ...],
    float | None,
    VerifiedAggregatePerformance | None,
]:
    observations: list[PerformanceObservation] = []
    aggregate: VerifiedAggregatePerformance | None = None
    calibration_error: float | None = None
    backtests = [item for item in admitted if item.artifact_kind == "backtest"]
    if not backtests:
        return (), None, None
    for artifact in backtests:
        predictions = {
            require_str(row.get("prediction_id"), field="prediction_id"): row
            for row in artifact.dataset("predictions").rows
        }
        opportunities = list(artifact.dataset("opportunities").rows)
        by_prediction: dict[str, list[dict]] = {}
        for row in opportunities:
            prediction_id = require_str(row.get("prediction_id"), field="prediction_id")
            by_prediction.setdefault(prediction_id, []).append(row)
        settlements = list(artifact.dataset("settlements").rows)
        settlement_by_opportunity: dict[str, dict] = {}
        for row in settlements:
            if require_str(row.get("kind"), field="kind") != "single":
                continue
            opportunity_ids = require_str_list(row.get("opportunity_ids"), field="opportunity_ids")
            if len(opportunity_ids) != 1:
                continue
            settlement_by_opportunity[opportunity_ids[0]] = row
        for prediction_id, prediction in sorted(predictions.items()):
            event_id = require_str(prediction.get("canonical_event_id"), field="canonical_event_id")
            snapshot = verified_by_event.get(event_id)
            if snapshot is None:
                continue
            probabilities_raw = require_list(prediction.get("probabilities"), field="probabilities")
            ordered_ids = require_str_list(
                prediction.get("ordered_selection_ids"), field="ordered_selection_ids"
            )
            probability_by_id: dict[str, float] = {}
            for index, item in enumerate(probabilities_raw):
                raw = require_dict(item, field=f"probabilities[{index}]")
                selection_id = require_str(raw.get("selection_id"), field="selection_id")
                probability_by_id[selection_id] = require_finite_number(
                    raw.get("probability"), field="probability"
                )
            try:
                probabilities = tuple(
                    probability_by_id[selection_id] for selection_id in ordered_ids
                )
            except KeyError as exc:
                raise MonitoringError("prediction probability vector is incomplete") from exc
            winners = [
                item.selection.selection_id
                for item in snapshot.result.market_outcomes
                if item.result == "win"
            ]
            if len(winners) != 1 or winners[0] not in ordered_ids:
                continue
            actual_index = ordered_ids.index(winners[0])
            related = by_prediction.get(prediction_id, [])
            settled_rows = [
                settlement_by_opportunity[
                    require_str(row.get("opportunity_id"), field="opportunity_id")
                ]
                for row in related
                if require_str(row.get("opportunity_id"), field="opportunity_id")
                in settlement_by_opportunity
            ]
            settled = bool(settled_rows)
            won: bool | None = None
            profit_units: float | None = None
            if settled:
                wins = [
                    require_str(row.get("result"), field="result") == "win" for row in settled_rows
                ]
                profits = [
                    float(require_decimal_string(row.get("profit_units"), field="profit_units"))
                    for row in settled_rows
                ]
                won = any(wins)
                profit_units = sum(profits)
            observations.append(
                PerformanceObservation(
                    observation_id=f"{artifact.artifact_id}:{prediction_id}",
                    probabilities=probabilities,
                    actual_index=actual_index,
                    settled=settled,
                    won=won,
                    profit_units=profit_units,
                )
            )
        if observations:
            continue
        aggregate_rows = artifact.dataset("aggregate_metrics").rows
        if len(aggregate_rows) != 1:
            raise MonitoringError("backtest aggregate metric selection is missing or ambiguous")
        aggregate = _aggregate_from_verified_row(aggregate_rows[0])
        if aggregate.calibration_error is not None:
            calibration_error = aggregate.calibration_error
    if observations:
        return tuple(sorted(observations, key=lambda item: item.observation_id)), None, None
    return (), calibration_error, aggregate


def _aggregate_from_verified_row(row: dict) -> VerifiedAggregatePerformance:
    sample_size = require_int(row.get("all_prediction_count"), field="all_prediction_count")
    log_loss_raw = row.get("all_log_loss")
    brier_raw = row.get("all_multiclass_brier_score")
    hit_raw = row.get("hit_rate")
    roi_raw = row.get("roi")
    bet_count = require_int(row.get("bet_count"), field="bet_count")
    return VerifiedAggregatePerformance(
        sample_size=sample_size,
        completed_result_count=sample_size,
        log_loss=(
            None
            if log_loss_raw is None
            else require_finite_number(log_loss_raw, field="all_log_loss")
        ),
        multiclass_brier_score=(
            None
            if brier_raw is None
            else require_finite_number(brier_raw, field="all_multiclass_brier_score")
        ),
        calibration_error=None,
        hit_rate=(
            None
            if bet_count <= 0 or hit_raw is None
            else require_finite_number(hit_raw, field="hit_rate")
        ),
        roi=(
            None
            if bet_count <= 0 or roi_raw is None
            else require_finite_number(roi_raw, field="roi")
        ),
    )
