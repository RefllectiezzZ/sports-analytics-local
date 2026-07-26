"""Idempotent durable worker handlers for deterministic operational services."""

from __future__ import annotations

from datetime import datetime

from sports_analytics.artifact_strict import require_dict, require_list, require_str
from sports_analytics.artifacts import load_typed_analytical_artifact
from sports_analytics.core.exceptions import (
    ArtifactError,
    MonitoringError,
    PermanentJobError,
    ResultError,
    SettlementConflictError,
    SettlementError,
)
from sports_analytics.data.database import connect_database, transaction
from sports_analytics.data.repositories.operations import (
    MonitoringRepository,
    ResultSnapshotRegistrationRepository,
    SettlementRepository,
)
from sports_analytics.data.types import JsonValue
from sports_analytics.jobs.context import JobExecutionContext
from sports_analytics.monitoring.artifacts import (
    load_monitoring_report,
    publish_monitoring_report,
)
from sports_analytics.monitoring.builders import build_monitoring_inputs, parse_monitoring_policy
from sports_analytics.monitoring.contracts import evaluate_monitoring
from sports_analytics.results.snapshots import load_result_snapshot
from sports_analytics.services.analysis import ANALYSIS_ARTIFACT_SCHEMA
from sports_analytics.settlement.service import (
    load_settlement_report,
    publish_settlement_report,
    settle_analysis_artifact,
)

SETTLE_ANALYSIS_JOB_TYPE = "settlement.settle-analysis"
RUN_MONITORING_JOB_TYPE = "monitoring.run"


def settle_analysis_handler(context: JobExecutionContext, payload: JsonValue) -> JsonValue:
    """Settle verified artifacts; publication and SQLite writes replay idempotently."""
    if (
        context._database_path is None
        or context._snapshots_directory is None
        or context._exports_directory is None
    ):
        raise PermanentJobError("settlement handler requires runtime path binding")
    try:
        request = require_dict(payload, field="payload")
        exact = {
            "analysis_relative_directory",
            "analysis_checksum_sha256",
            "result_snapshots",
            "as_of_utc",
            "output_relative_directory",
            "actor",
        }
        if set(request) != exact:
            raise PermanentJobError("settlement job payload fields are not exact")
        artifact = load_typed_analytical_artifact(
            root=context._exports_directory,
            relative_directory=require_str(
                request["analysis_relative_directory"],
                field="analysis_relative_directory",
            ),
            expected_kind="analysis",
            expected_schema_version=ANALYSIS_ARTIFACT_SCHEMA,
            expected_checksum=require_str(
                request["analysis_checksum_sha256"],
                field="analysis_checksum_sha256",
            ),
        )
        snapshots = []
        for raw in require_list(request["result_snapshots"], field="result_snapshots"):
            item = require_dict(raw, field="result_snapshots[]")
            if set(item) != {"relative_directory", "checksum_sha256", "snapshot_id"}:
                raise PermanentJobError("result snapshot reference fields are not exact")
            snapshots.append(
                load_result_snapshot(
                    root=context._snapshots_directory,
                    relative_directory=require_str(
                        item["relative_directory"],
                        field="relative_directory",
                    ),
                    expected_checksum=require_str(
                        item["checksum_sha256"],
                        field="checksum_sha256",
                    ),
                    expected_snapshot_id=require_str(item["snapshot_id"], field="snapshot_id"),
                )
            )
        as_of = _timestamp(request["as_of_utc"], "as_of_utc")
        report = settle_analysis_artifact(
            artifact=artifact,
            result_snapshots=tuple(snapshots),
            as_of_utc=as_of,
        )
        output = require_str(
            request["output_relative_directory"],
            field="output_relative_directory",
        )
        try:
            published = publish_settlement_report(
                root=context._exports_directory,
                relative_directory=output,
                report=report,
            )
        except ArtifactError:
            existing = load_settlement_report(
                root=context._exports_directory,
                relative_directory=output,
            )
            if existing.run_id != report.run_id or existing.artifact is None:
                raise SettlementError("existing settlement output conflicts with replay") from None
            published = report.__class__(
                run_id=report.run_id,
                source_artifact_id=report.source_artifact_id,
                source_artifact_checksum_sha256=report.source_artifact_checksum_sha256,
                policy_id=report.policy_id,
                policy_version=report.policy_version,
                as_of_utc=report.as_of_utc,
                settlements=report.settlements,
                artifact=existing.artifact,
            )
        context.checkpoint()
        actor = require_str(request["actor"], field="actor")
        try:
            with connect_database(context._database_path) as connection:
                with transaction(connection, immediate=True):
                    registrations = ResultSnapshotRegistrationRepository(connection)
                    for snapshot in snapshots:
                        registrations.register(
                            snapshot=snapshot,
                            registered_at=as_of,
                            actor=actor,
                        )
                    SettlementRepository(connection).persist_report(
                        report=published,
                        actor=actor,
                        created_at=as_of,
                    )
        except SettlementConflictError:
            with connect_database(context._database_path) as connection:
                with transaction(connection, immediate=True):
                    SettlementRepository(connection).record_conflicts(
                        report=published,
                        actor=actor,
                        occurred_at=as_of,
                    )
            raise
        return {
            "run_id": report.run_id,
            "settlement_count": len(report.settlements),
            "report_checksum_sha256": (
                None if published.artifact is None else published.artifact.checksum_sha256
            ),
        }
    except (ArtifactError, ResultError, SettlementError, ValueError, TypeError) as exc:
        raise PermanentJobError(str(exc)) from exc


def run_monitoring_handler(context: JobExecutionContext, payload: JsonValue) -> JsonValue:
    """Evaluate and persist an immutable monitoring report without external calls."""
    if (
        context._database_path is None
        or context._exports_directory is None
        or context._snapshots_directory is None
    ):
        raise PermanentJobError("monitoring handler requires runtime path binding")
    try:
        request = require_dict(payload, field="payload")
        exact = {
            "policy",
            "evidence",
            "as_of_utc",
            "window_start_utc",
            "window_end_utc",
            "output_relative_directory",
        }
        if set(request) != exact:
            raise PermanentJobError("monitoring job payload fields are not exact")
        as_of = _timestamp(request["as_of_utc"], "as_of_utc")
        start = _timestamp(request["window_start_utc"], "window_start_utc")
        end = _timestamp(request["window_end_utc"], "window_end_utc")
        with connect_database(context._database_path, read_only=True) as connection:
            inputs = build_monitoring_inputs(
                exports_root=context._exports_directory,
                snapshots_root=context._snapshots_directory,
                connection=connection,
                evidence_payload=request["evidence"],
                window_start_utc=start,
                window_end_utc=end,
                as_of_utc=as_of,
            )
        report = evaluate_monitoring(
            inputs=inputs,
            policy=parse_monitoring_policy(request["policy"]),
            as_of_utc=as_of,
            window_start_utc=start,
            window_end_utc=end,
        )
        output = require_str(
            request["output_relative_directory"],
            field="output_relative_directory",
        )
        artifact = publish_monitoring_report(
            root=context._exports_directory,
            relative_directory=output,
            report=report,
        )
        reused = load_monitoring_report(
            root=context._exports_directory,
            relative_directory=output,
            expected_checksum=artifact.checksum_sha256,
            expected_run_id=report.run_id,
        )
        if reused.report.run_id != report.run_id:
            raise MonitoringError("existing monitoring output conflicts with replay")
        if reused.checksum_sha256 != artifact.checksum_sha256:
            raise MonitoringError("existing monitoring checksum conflicts with replay")
        artifact = reused.artifact
        context.checkpoint()
        with connect_database(context._database_path) as connection:
            with transaction(connection, immediate=True):
                MonitoringRepository(connection).persist(
                    report=report,
                    artifact=artifact,
                    created_at=report.as_of_utc,
                    actor="worker",
                )
        return {
            "run_id": report.run_id,
            "summary_status": report.summary_status.value,
            "report_checksum_sha256": artifact.checksum_sha256,
        }
    except (ArtifactError, MonitoringError, ValueError, TypeError) as exc:
        raise PermanentJobError(str(exc)) from exc


def _timestamp(value: object, field: str) -> datetime:
    try:
        from sports_analytics.artifact_strict import require_canonical_utc_timestamp_string

        return require_canonical_utc_timestamp_string(value, field=field)
    except Exception as exc:
        raise PermanentJobError(f"{field} must be canonical UTC") from exc
