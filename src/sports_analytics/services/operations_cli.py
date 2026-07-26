"""Engine CLI integration for result, settlement, monitoring, and governance operations."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from sports_analytics.artifact_strict import (
    require_bool,
    require_dict,
    require_finite_number,
    require_int,
    require_list,
    require_sha256_checksum,
    require_str,
)
from sports_analytics.artifacts import load_typed_analytical_artifact
from sports_analytics.core.cli import SUCCESS_EXIT
from sports_analytics.core.exceptions import ConfigurationError, SettlementConflictError
from sports_analytics.core.runtime import RuntimeContext, bootstrap_runtime, validate_configuration
from sports_analytics.data.codec import dumps_canonical_json
from sports_analytics.data.database import connect_database, transaction
from sports_analytics.data.repositories.operations import (
    MonitoringRepository,
    ResultSnapshotRegistrationRepository,
    SettlementRepository,
)
from sports_analytics.data.types import JsonValue
from sports_analytics.governance.contracts import (
    ModelEvaluationEvidence,
    ModelRole,
    PromotionPolicy,
    evaluate_challenger,
)
from sports_analytics.governance.evidence import build_model_evaluation_evidence
from sports_analytics.governance.repository import ModelGovernanceRepository
from sports_analytics.monitoring.artifacts import (
    load_monitoring_report,
    publish_monitoring_report,
)
from sports_analytics.monitoring.builders import build_monitoring_inputs, parse_monitoring_policy
from sports_analytics.monitoring.contracts import (
    EvidenceReference,
    MetricDirection,
    MetricThreshold,
    MonitoringInputs,
    MonitoringPolicy,
    PerformanceObservation,
    evaluate_monitoring,
)
from sports_analytics.results.snapshots import load_result_snapshot
from sports_analytics.services.analysis import ANALYSIS_ARTIFACT_SCHEMA
from sports_analytics.services.training import verify_model_artifact
from sports_analytics.settlement.service import (
    load_settlement_report,
    publish_settlement_report,
    settle_analysis_artifact,
)
from sports_analytics.snapshots.paths import resolve_under_root


def add_operational_arguments(parser: argparse.ArgumentParser, mode: Any) -> None:
    """Add focused mutually-exclusive engine operations to the existing mode group."""
    mode.add_argument("--verify-result-snapshot", metavar="RELATIVE_DIRECTORY")
    mode.add_argument("--register-result-snapshot", metavar="RELATIVE_DIRECTORY")
    mode.add_argument("--settle-analysis", metavar="REQUEST_JSON")
    mode.add_argument("--list-settlement-runs", action="store_true")
    mode.add_argument("--verify-settlement-report", metavar="RELATIVE_DIRECTORY")
    mode.add_argument("--run-monitoring", metavar="REQUEST_JSON")
    mode.add_argument("--verify-monitoring-report", metavar="RELATIVE_DIRECTORY")
    mode.add_argument("--register-model", metavar="RELATIVE_PATH")
    mode.add_argument("--list-model-registry", action="store_true")
    mode.add_argument("--evaluate-challenger", metavar="REQUEST_JSON")
    mode.add_argument("--apply-promotion", metavar="DECISION_ID")
    mode.add_argument("--rollback-promotion", metavar="TRANSITION_ID")
    mode.add_argument("--governance-history", action="store_true")
    parser.add_argument("--as-of-utc", default=None, metavar="UTC_TIMESTAMP")
    parser.add_argument("--actor", default="local-operator", metavar="IDENTIFIER")
    parser.add_argument(
        "--model-role",
        choices=[ModelRole.CHAMPION.value, ModelRole.CHALLENGER.value],
        default=ModelRole.CHALLENGER.value,
    )


def operational_mode_values(args: argparse.Namespace) -> tuple[bool, ...]:
    return (
        args.verify_result_snapshot is not None,
        args.register_result_snapshot is not None,
        args.settle_analysis is not None,
        args.list_settlement_runs,
        args.verify_settlement_report is not None,
        args.run_monitoring is not None,
        args.verify_monitoring_report is not None,
        args.register_model is not None,
        args.list_model_registry,
        args.evaluate_challenger is not None,
        args.apply_promotion is not None,
        args.rollback_promotion is not None,
        args.governance_history,
    )


def run_operational_mode(args: argparse.Namespace) -> int | None:
    """Run a selected operational mode, returning None when none is selected."""
    if not any(operational_mode_values(args)):
        return None
    if args.verify_result_snapshot is not None:
        _, paths = validate_configuration(config_path=args.config, env_file=args.env_file)
        snapshot = load_result_snapshot(
            root=paths.snapshots_directory,
            relative_directory=args.verify_result_snapshot,
            expected_checksum=args.checksum,
        )
        _print(
            {
                "snapshot_id": snapshot.snapshot_id,
                "checksum_sha256": snapshot.checksum_sha256,
                "canonical_result_id": snapshot.result.canonical_result_id,
                "canonical_event_id": snapshot.result.canonical_event_id,
                "event_status": snapshot.result.event_status.value,
            }
        )
        return SUCCESS_EXIT
    if args.register_result_snapshot is not None:
        as_of = _as_of(args)
        runtime = _runtime(args)
        snapshot = load_result_snapshot(
            root=runtime.paths.snapshots_directory,
            relative_directory=args.register_result_snapshot,
            expected_checksum=args.checksum,
        )
        with connect_database(runtime.database_path) as connection:
            with transaction(connection, immediate=True):
                ResultSnapshotRegistrationRepository(connection).register(
                    snapshot=snapshot,
                    registered_at=as_of,
                    actor=args.actor,
                )
        _print({"snapshot_id": snapshot.snapshot_id, "registered": True})
        return SUCCESS_EXIT
    if args.settle_analysis is not None:
        return _settle(args)
    if args.list_settlement_runs:
        runtime = _runtime(args)
        with connect_database(runtime.database_path, read_only=True) as connection:
            rows = SettlementRepository(connection).list_runs()
        _print({"settlement_runs": list(rows)})
        return SUCCESS_EXIT
    if args.verify_settlement_report is not None:
        _, paths = validate_configuration(config_path=args.config, env_file=args.env_file)
        report = load_settlement_report(
            root=paths.exports_directory,
            relative_directory=args.verify_settlement_report,
            expected_checksum=args.checksum,
        )
        _print(
            {
                "run_id": report.run_id,
                "checksum_sha256": (
                    None if report.artifact is None else report.artifact.checksum_sha256
                ),
                "verified": True,
            }
        )
        return SUCCESS_EXIT
    if args.run_monitoring is not None:
        return _monitor(args)
    if args.verify_monitoring_report is not None:
        _, paths = validate_configuration(config_path=args.config, env_file=args.env_file)
        artifact = load_monitoring_report(
            root=paths.exports_directory,
            relative_directory=args.verify_monitoring_report,
            expected_checksum=args.checksum,
        )
        _print(
            {
                "artifact_id": artifact.artifact_id,
                "checksum_sha256": artifact.checksum_sha256,
                "verified": True,
            }
        )
        return SUCCESS_EXIT
    if args.register_model is not None:
        as_of = _as_of(args)
        runtime = _runtime(args)
        model_artifact = verify_model_artifact(
            paths=runtime.paths,
            relative_path=args.register_model,
            expected_checksum=args.checksum,
        )
        with connect_database(runtime.database_path) as connection:
            with transaction(connection, immediate=True):
                entry = ModelGovernanceRepository(connection).register_verified_model(
                    artifact=model_artifact,
                    relative_path=args.register_model,
                    registered_at=as_of,
                    actor=args.actor,
                    role=ModelRole(args.model_role),
                    provenance={"source": "engine-cli", "verified": True},
                )
        _print(_entry_json(entry))
        return SUCCESS_EXIT
    if args.list_model_registry:
        runtime = _runtime(args)
        with connect_database(runtime.database_path, read_only=True) as connection:
            entries = ModelGovernanceRepository(connection).list_models()
        _print({"models": [_entry_json(item) for item in entries]})
        return SUCCESS_EXIT
    if args.evaluate_challenger is not None:
        return _evaluate(args)
    if args.apply_promotion is not None or args.rollback_promotion is not None:
        as_of = _as_of(args)
        runtime = _runtime(args)
        with connect_database(runtime.database_path) as connection:
            with transaction(connection, immediate=True):
                repository = ModelGovernanceRepository(connection)
                if args.apply_promotion is not None:
                    transition_id = repository.apply_promotion(
                        decision_id=args.apply_promotion,
                        actor=args.actor,
                        occurred_at=as_of,
                    )
                else:
                    transition_id = repository.rollback_transition(
                        transition_id=args.rollback_promotion,
                        actor=args.actor,
                        occurred_at=as_of,
                    )
        _print({"transition_id": transition_id})
        return SUCCESS_EXIT
    if args.governance_history:
        runtime = _runtime(args)
        with connect_database(runtime.database_path, read_only=True) as connection:
            history = ModelGovernanceRepository(connection).list_history()
        _print({"history": list(history)})
        return SUCCESS_EXIT
    return None


def _settle(args: argparse.Namespace) -> int:
    request = _request(args.settle_analysis)
    _exact_fields(
        request,
        {
            "analysis_relative_directory",
            "analysis_checksum_sha256",
            "result_snapshots",
            "as_of_utc",
            "output_relative_directory",
        },
        "settlement request",
    )
    runtime = _runtime(args)
    artifact = load_typed_analytical_artifact(
        root=runtime.paths.exports_directory,
        relative_directory=_text(request, "analysis_relative_directory"),
        expected_kind="analysis",
        expected_schema_version=ANALYSIS_ARTIFACT_SCHEMA,
        expected_checksum=_optional_checksum(request, "analysis_checksum_sha256"),
    )
    result_refs = require_list(request.get("result_snapshots"), field="result_snapshots")
    snapshots = []
    for raw in result_refs:
        item = require_dict(raw, field="result_snapshots[]")
        if set(item) != {"relative_directory", "checksum_sha256", "snapshot_id"}:
            raise ConfigurationError("result snapshot reference fields are not exact")
        snapshots.append(
            load_result_snapshot(
                root=runtime.paths.snapshots_directory,
                relative_directory=_text(item, "relative_directory"),
                expected_checksum=_optional_checksum(item, "checksum_sha256"),
                expected_snapshot_id=_text(item, "snapshot_id"),
            )
        )
    as_of = _timestamp(request.get("as_of_utc"), "as_of_utc")
    report = settle_analysis_artifact(
        artifact=artifact,
        result_snapshots=tuple(snapshots),
        as_of_utc=as_of,
    )
    published = publish_settlement_report(
        root=runtime.paths.exports_directory,
        relative_directory=_text(request, "output_relative_directory"),
        report=report,
    )
    try:
        with connect_database(runtime.database_path) as connection:
            with transaction(connection, immediate=True):
                registrations = ResultSnapshotRegistrationRepository(connection)
                for snapshot in snapshots:
                    registrations.register(
                        snapshot=snapshot,
                        registered_at=as_of,
                        actor=args.actor,
                    )
                SettlementRepository(connection).persist_report(
                    report=published,
                    actor=args.actor,
                    created_at=as_of,
                )
    except SettlementConflictError:
        with connect_database(runtime.database_path) as connection:
            with transaction(connection, immediate=True):
                SettlementRepository(connection).record_conflicts(
                    report=published,
                    actor=args.actor,
                    occurred_at=as_of,
                )
        raise
    _print(
        {
            "run_id": published.run_id,
            "settlement_count": len(published.settlements),
            "checksum_sha256": published.artifact.checksum_sha256 if published.artifact else None,
        }
    )
    return SUCCESS_EXIT


def _monitor(args: argparse.Namespace) -> int:
    request = _request(args.run_monitoring)
    _exact_fields(
        request,
        {
            "policy",
            "evidence",
            "as_of_utc",
            "window_start_utc",
            "window_end_utc",
            "output_relative_directory",
        },
        "monitoring request",
    )
    runtime = _runtime(args)
    policy = parse_monitoring_policy(request.get("policy"))
    as_of = _timestamp(request.get("as_of_utc"), "as_of_utc")
    start = _timestamp(request.get("window_start_utc"), "window_start_utc")
    end = _timestamp(request.get("window_end_utc"), "window_end_utc")
    with connect_database(runtime.database_path, read_only=True) as connection:
        inputs = build_monitoring_inputs(
            exports_root=runtime.paths.exports_directory,
            connection=connection,
            evidence_payload=request.get("evidence"),
            window_start_utc=start,
            window_end_utc=end,
            as_of_utc=as_of,
        )
    report = evaluate_monitoring(
        inputs=inputs, policy=policy, as_of_utc=as_of, window_start_utc=start, window_end_utc=end
    )
    artifact = publish_monitoring_report(
        root=runtime.paths.exports_directory,
        relative_directory=_text(request, "output_relative_directory"),
        report=report,
    )
    with connect_database(runtime.database_path) as connection:
        with transaction(connection, immediate=True):
            MonitoringRepository(connection).persist(
                report=report,
                artifact=artifact,
                created_at=report.as_of_utc,
                actor=args.actor,
            )
    _print(
        {
            "run_id": report.run_id,
            "summary_status": report.summary_status.value,
            "checksum_sha256": artifact.checksum_sha256,
        }
    )
    return SUCCESS_EXIT


def _evaluate(args: argparse.Namespace) -> int:
    request = _request(args.evaluate_challenger)
    _exact_fields(
        request,
        {
            "champion_model_artifact_id",
            "challenger_model_artifact_id",
            "champion_evidence_reference",
            "challenger_evidence_reference",
            "policy",
            "as_of_utc",
        },
        "challenger evaluation request",
    )
    runtime = _runtime(args)
    with connect_database(runtime.database_path) as connection:
        repository = ModelGovernanceRepository(connection)
        champion = repository.get_model(_text(request, "champion_model_artifact_id"))
        challenger = repository.get_model(_text(request, "challenger_model_artifact_id"))
        if champion is None or challenger is None:
            raise ConfigurationError("champion and challenger must both be registered")
        policy = _promotion_policy(require_dict(request.get("policy"), field="policy"))
        decision = evaluate_challenger(
            champion=champion,
            challenger=challenger,
            champion_evidence=build_model_evaluation_evidence(
                paths=runtime.paths,
                registry_entry=champion,
                payload=request.get("champion_evidence_reference"),
            ),
            challenger_evidence=build_model_evaluation_evidence(
                paths=runtime.paths,
                registry_entry=challenger,
                payload=request.get("challenger_evidence_reference"),
            ),
            policy=policy,
            as_of_utc=_timestamp(request.get("as_of_utc"), "as_of_utc"),
        )
        with transaction(connection, immediate=True):
            repository.record_decision(
                decision=decision,
                actor=args.actor,
                created_at=decision.as_of_utc,
            )
    _print(decision.to_json())
    return SUCCESS_EXIT


def _runtime(args: argparse.Namespace) -> RuntimeContext:
    return bootstrap_runtime(
        "engine",
        config_path=args.config,
        env_file=args.env_file,
    )


def _as_of(args: argparse.Namespace) -> datetime:
    if args.as_of_utc is None:
        raise ConfigurationError("this operation requires explicit --as-of-utc")
    return _timestamp(args.as_of_utc, "as_of_utc")


def _request(path_text: str) -> dict[str, JsonValue]:
    try:
        path = resolve_under_root(
            Path.cwd(),
            path_text.replace("\\", "/"),
            expect_file=True,
            error_type=ConfigurationError,
        )
    except Exception as exc:
        raise ConfigurationError("operation request path must remain under the local root") from exc
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConfigurationError("cannot read canonical operation request JSON") from exc
    if not isinstance(value, dict):
        raise ConfigurationError("operation request must be a JSON object")
    return cast(dict[str, JsonValue], value)


def _timestamp(value: object, field: str) -> datetime:
    try:
        from sports_analytics.artifact_strict import require_canonical_utc_timestamp_string

        return require_canonical_utc_timestamp_string(value, field=field)
    except Exception as exc:
        raise ConfigurationError(f"{field} must be canonical UTC") from exc


def _text(payload: dict[str, JsonValue], field: str) -> str:
    return require_str(payload.get(field), field=field)


def _optional_checksum(payload: dict[str, JsonValue], field: str) -> str | None:
    value = payload.get(field)
    return None if value is None else require_sha256_checksum(value, field=field)


def _policy(payload: dict[str, JsonValue]) -> MonitoringPolicy:
    _exact_fields(payload, {"policy_id", "policy_version", "thresholds"}, "monitoring policy")
    thresholds: list[tuple[str, MetricThreshold]] = []
    for raw in require_list(payload.get("thresholds"), field="thresholds"):
        item = require_dict(raw, field="thresholds[]")
        _exact_fields(
            item,
            {"metric_name", "warning", "critical", "direction"},
            "monitoring threshold",
        )
        thresholds.append(
            (
                _text(item, "metric_name"),
                MetricThreshold(
                    warning=require_finite_number(item.get("warning"), field="warning"),
                    critical=require_finite_number(item.get("critical"), field="critical"),
                    direction=MetricDirection(_text(item, "direction")),
                ),
            )
        )
    return MonitoringPolicy(
        policy_id=_text(payload, "policy_id"),
        policy_version=_text(payload, "policy_version"),
        thresholds=tuple(sorted(thresholds)),
    )


def _monitoring_inputs(payload: dict[str, JsonValue]) -> MonitoringInputs:
    allowed_fields = {
        "evidence",
        "performance",
        "latest_source_observed_at_utc",
        "oldest_unsettled_completion_utc",
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
        "calibration_error",
    }
    if "evidence" not in payload or set(payload) - allowed_fields:
        raise ConfigurationError("monitoring inputs contain missing or unknown fields")
    evidence = tuple(
        EvidenceReference(
            evidence_type=_text(item, "evidence_type"),
            evidence_id=_text(item, "evidence_id"),
            checksum_sha256=require_sha256_checksum(
                item.get("checksum_sha256"),
                field="checksum_sha256",
            ),
        )
        for item in (
            require_dict(raw, field="evidence[]")
            for raw in require_list(payload.get("evidence"), field="evidence")
        )
    )
    for item in (
        require_dict(raw, field="evidence[]")
        for raw in require_list(payload.get("evidence"), field="evidence")
    ):
        _exact_fields(
            item,
            {"evidence_type", "evidence_id", "checksum_sha256"},
            "monitoring evidence",
        )
    performance = tuple(
        PerformanceObservation(
            observation_id=_text(item, "observation_id"),
            probabilities=tuple(
                require_finite_number(value, field="probabilities[]")
                for value in require_list(item.get("probabilities"), field="probabilities")
            ),
            actual_index=require_int(item.get("actual_index"), field="actual_index"),
            settled=require_bool(item.get("settled"), field="settled"),
            won=(None if item.get("won") is None else require_bool(item.get("won"), field="won")),
            profit_units=(
                None
                if item.get("profit_units") is None
                else require_finite_number(item.get("profit_units"), field="profit_units")
            ),
        )
        for item in (
            require_dict(raw, field="performance[]")
            for raw in require_list(payload.get("performance", []), field="performance")
        )
    )
    for item in (
        require_dict(raw, field="performance[]")
        for raw in require_list(payload.get("performance", []), field="performance")
    ):
        _exact_fields(
            item,
            {
                "observation_id",
                "probabilities",
                "actual_index",
                "settled",
                "won",
                "profit_units",
            },
            "performance observation",
        )
    timestamp_fields = {
        "latest_source_observed_at_utc",
        "oldest_unsettled_completion_utc",
    }
    count_fields = {
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
    }
    kwargs: dict[str, object] = {"evidence": evidence, "performance": performance}
    for field in timestamp_fields:
        value = payload.get(field)
        kwargs[field] = None if value is None else _timestamp(value, field)
    for field in count_fields:
        value = payload.get(field)
        kwargs[field] = None if value is None else require_int(value, field=field)
    calibration = payload.get("calibration_error")
    kwargs["calibration_error"] = (
        None
        if calibration is None
        else require_finite_number(calibration, field="calibration_error")
    )
    return MonitoringInputs(**kwargs)  # type: ignore[arg-type]


def _promotion_policy(payload: dict[str, JsonValue]) -> PromotionPolicy:
    _exact_fields(
        payload,
        {
            "policy_id",
            "policy_version",
            "minimum_sample_size",
            "minimum_coverage",
            "minimum_log_loss_improvement",
            "minimum_brier_improvement",
            "minimum_calibration_improvement",
            "require_calibration",
        },
        "promotion policy",
    )
    return PromotionPolicy(
        policy_id=_text(payload, "policy_id"),
        policy_version=_text(payload, "policy_version"),
        minimum_sample_size=require_int(
            payload.get("minimum_sample_size"),
            field="minimum_sample_size",
        ),
        minimum_coverage=require_finite_number(
            payload.get("minimum_coverage"),
            field="minimum_coverage",
        ),
        minimum_log_loss_improvement=require_finite_number(
            payload.get("minimum_log_loss_improvement"),
            field="minimum_log_loss_improvement",
        ),
        minimum_brier_improvement=require_finite_number(
            payload.get("minimum_brier_improvement"),
            field="minimum_brier_improvement",
        ),
        minimum_calibration_improvement=require_finite_number(
            payload.get("minimum_calibration_improvement"),
            field="minimum_calibration_improvement",
        ),
        require_calibration=require_bool(
            payload.get("require_calibration"),
            field="require_calibration",
        ),
    )


def _model_evidence(payload: dict[str, JsonValue]) -> ModelEvaluationEvidence:
    _exact_fields(
        payload,
        {
            "evidence_artifact_id",
            "evidence_checksum_sha256",
            "model_artifact_id",
            "sport_code",
            "market_key",
            "evaluation_mode",
            "window_start_utc",
            "window_end_utc",
            "event_population_id",
            "sample_size",
            "completed_result_count",
            "coverage",
            "log_loss",
            "multiclass_brier_score",
            "calibration_error",
            "hit_rate",
            "roi",
        },
        "model evaluation evidence",
    )
    return ModelEvaluationEvidence(
        evidence_artifact_id=_text(payload, "evidence_artifact_id"),
        evidence_checksum_sha256=require_sha256_checksum(
            payload.get("evidence_checksum_sha256"),
            field="evidence_checksum_sha256",
        ),
        model_artifact_id=_text(payload, "model_artifact_id"),
        sport_code=_text(payload, "sport_code"),
        market_key=_text(payload, "market_key"),
        evaluation_mode=_text(payload, "evaluation_mode"),
        window_start_utc=_timestamp(payload.get("window_start_utc"), "window_start_utc"),
        window_end_utc=_timestamp(payload.get("window_end_utc"), "window_end_utc"),
        event_population_id=_text(payload, "event_population_id"),
        sample_size=require_int(payload.get("sample_size"), field="sample_size"),
        completed_result_count=require_int(
            payload.get("completed_result_count"),
            field="completed_result_count",
        ),
        coverage=require_finite_number(payload.get("coverage"), field="coverage"),
        log_loss=require_finite_number(payload.get("log_loss"), field="log_loss"),
        multiclass_brier_score=require_finite_number(
            payload.get("multiclass_brier_score"),
            field="multiclass_brier_score",
        ),
        calibration_error=_optional_number(payload, "calibration_error"),
        hit_rate=_optional_number(payload, "hit_rate"),
        roi=_optional_number(payload, "roi"),
    )


def _optional_number(payload: dict[str, JsonValue], field: str) -> float | None:
    value = payload.get(field)
    return None if value is None else require_finite_number(value, field=field)


def _entry_json(entry: object) -> dict[str, JsonValue]:
    from sports_analytics.governance.contracts import ModelRegistryEntry

    if not isinstance(entry, ModelRegistryEntry):
        raise ConfigurationError("invalid model registry entry")
    return {
        "model_artifact_id": entry.model_artifact_id,
        "model_checksum_sha256": entry.model_checksum_sha256,
        "model_relative_path": entry.model_relative_path,
        "model_specification_version": entry.model_specification_version,
        "feature_specification_version": entry.feature_specification_version,
        "sport_code": entry.sport_code,
        "market_key": entry.market_key,
        "registered_at": entry.registered_at.isoformat(),
        "role": entry.role.value,
        "lifecycle_status": entry.lifecycle_status.value,
        "superseded_model_artifact_id": entry.superseded_model_artifact_id,
        "version": entry.version,
    }


def _print(payload: dict[str, JsonValue]) -> None:
    print(dumps_canonical_json(payload))


def _exact_fields(
    payload: dict[str, JsonValue],
    expected: set[str],
    description: str,
) -> None:
    if set(payload) != expected:
        raise ConfigurationError(f"{description} fields are not exact")
