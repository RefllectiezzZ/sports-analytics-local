"""Bounded static worker handlers for the closed-loop football lifecycle."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final

from sports_analytics.artifact_strict import (
    require_canonical_utc_timestamp_string,
    require_dict,
    require_list,
    require_str,
)
from sports_analytics.artifacts import (
    AnalyticalArtifact,
    TypedAnalyticalArtifact,
    load_typed_analytical_artifact,
)
from sports_analytics.core.exceptions import (
    ArtifactError,
    MonitoringError,
    PermanentJobError,
    ResultError,
    SettlementError,
)
from sports_analytics.data.database import connect_database, transaction
from sports_analytics.data.repositories.operations import (
    MonitoringRepository,
    SettlementRepository,
)
from sports_analytics.data.repositories.snapshots import SnapshotRepository
from sports_analytics.data.types import JsonValue, SnapshotStatus
from sports_analytics.features.football.datasets import load_finished_events_from_snapshots
from sports_analytics.jobs.context import JobExecutionContext
from sports_analytics.learning.lifecycle import (
    RetrainingPolicy,
    evaluate_retraining_trigger,
    load_training_eligibility_ledger,
)
from sports_analytics.models.football_challengers import (
    CovariateScoreModel,
    challenger_artifact_id,
    fit_covariate_dixon_coles,
    load_challenger_artifact,
    write_challenger_artifact,
)
from sports_analytics.models.football_evaluation import EvaluationProvenance
from sports_analytics.models.football_scores import ScoreModelConfiguration, ScoreTrainingMatch
from sports_analytics.models.football_tournament import TournamentSplitConfiguration
from sports_analytics.models.football_unified_tournament import (
    UnifiedTournament,
    load_unified_tournament_artifact,
    run_unified_tournament,
    unified_tournament_artifact_id,
    write_unified_tournament_artifact,
)
from sports_analytics.models.identity import content_addressed_id
from sports_analytics.monitoring.artifacts import publish_monitoring_report
from sports_analytics.monitoring.builders import build_monitoring_inputs
from sports_analytics.monitoring.contracts import DEFAULT_MONITORING_POLICY, evaluate_monitoring
from sports_analytics.results.football_snapshot_bridge import (
    register_completed_results_from_snapshot,
)
from sports_analytics.results.snapshots import VerifiedResultSnapshot, load_result_snapshot
from sports_analytics.services.analysis import ANALYSIS_ARTIFACT_SCHEMA
from sports_analytics.settlement.service import (
    publish_settlement_report,
    settle_analysis_artifact,
)

REGISTER_RESULTS_JOB_TYPE: Final[str] = "results.register-from-snapshot"
SETTLE_NEW_RESULTS_JOB_TYPE: Final[str] = "settlement.settle-new-results"
REFRESH_MONITORING_JOB_TYPE: Final[str] = "monitoring.refresh"
EVALUATE_RETRAINING_TRIGGER_JOB_TYPE: Final[str] = "training.evaluate-retraining-trigger"
RUN_CHALLENGER_CYCLE_JOB_TYPE: Final[str] = "training.run-challenger-cycle"


@dataclass(frozen=True, slots=True)
class _SnapshotReference:
    snapshot_id: str
    checksum_sha256: str


@dataclass(frozen=True, slots=True)
class _LifecyclePayload:
    snapshots: tuple[_SnapshotReference, ...]
    artifacts: tuple[_SnapshotReference, ...]
    sport_code: str
    competition_id: str
    policy_id: str
    cutoff_utc: datetime


def register_results_handler(context: JobExecutionContext, payload: JsonValue) -> JsonValue:
    """Resolve one registered snapshot ID; arbitrary paths never enter the payload."""
    request = _payload(payload, require_one_snapshot=True)
    if context._database_path is None or context._snapshots_directory is None:
        raise PermanentJobError("result registration requires runtime paths")
    reference = request.snapshots[0]
    relative_path = _registered_manifest(
        context,
        reference,
        expected_snapshot_type="football-ingestion",
    )
    report = register_completed_results_from_snapshot(
        database_path=context._database_path,
        snapshots_directory=context._snapshots_directory,
        relative_manifest_path=relative_path,
        output_relative_root="canonical-results/from-football-snapshot",
        registered_at=request.cutoff_utc,
        actor="worker",
    )
    context.checkpoint()
    return {
        "source_snapshot_id": report.source_snapshot_id,
        "completed_events": report.completed_events,
        "skipped_events": report.skipped_events,
        "registration_report_artifact_id": (
            None if report.report_artifact is None else report.report_artifact.artifact_id
        ),
    }


def settle_new_results_handler(context: JobExecutionContext, payload: JsonValue) -> JsonValue:
    """Settle one exact registered analysis against exact registered results."""
    request = _payload(payload, require_artifacts=True)
    if len(request.artifacts) != 1:
        raise PermanentJobError("settlement refresh requires one exact analysis artifact")
    if (
        context._database_path is None
        or context._snapshots_directory is None
        or context._exports_directory is None
    ):
        raise PermanentJobError("settlement refresh requires bound runtime paths")
    try:
        artifact = _resolve_typed_artifact(
            context._exports_directory,
            request.artifacts[0],
        )
        snapshots = _registered_results(context, request.snapshots)
        report = settle_analysis_artifact(
            artifact=artifact,
            result_snapshots=snapshots,
            as_of_utc=request.cutoff_utc,
        )
        published = publish_settlement_report(
            root=context._exports_directory,
            relative_directory=f"closed-loop-settlements/{report.run_id}",
            report=report,
        )
        context.checkpoint()
        with connect_database(context._database_path) as connection:
            with transaction(connection, immediate=True):
                SettlementRepository(connection).persist_report(
                    report=published,
                    actor="worker",
                    created_at=request.cutoff_utc,
                )
    except (ArtifactError, ResultError, SettlementError, OSError, ValueError) as exc:
        raise PermanentJobError(str(exc)) from exc
    return {
        "state": "settled",
        "run_id": report.run_id,
        "settlement_count": len(report.settlements),
        "settlement_artifact_id": (
            None if published.artifact is None else published.artifact.artifact_id
        ),
    }


def refresh_monitoring_handler(context: JobExecutionContext, payload: JsonValue) -> JsonValue:
    """Evaluate and persist monitoring for exact content-addressed analysis evidence."""
    request = _payload(payload, require_artifacts=True)
    if (
        context._database_path is None
        or context._snapshots_directory is None
        or context._exports_directory is None
    ):
        raise PermanentJobError("monitoring refresh requires bound runtime paths")
    try:
        resolved = tuple(
            _resolve_typed_artifact(context._exports_directory, item) for item in request.artifacts
        )
        event_starts = tuple(
            require_canonical_utc_timestamp_string(
                row["event_start_utc"],
                field="event_start_utc",
            )
            for artifact in resolved
            for row in artifact.dataset("opportunities").rows
        )
        if not event_starts:
            raise MonitoringError("monitoring evidence contains no opportunity window")
        window_start = min(event_starts)
        window_end = max(event_starts)
        if window_start == window_end:
            raise MonitoringError("monitoring evidence requires a non-zero event window")
        evidence: list[JsonValue] = [
            {
                "kind": item.artifact_kind,
                "relative_directory": item.relative_directory,
                "checksum_sha256": item.checksum_sha256,
                "artifact_id": item.artifact_id,
                "schema_version": item.schema_version,
            }
            for item in resolved
        ]
        with connect_database(context._database_path, read_only=True) as connection:
            inputs = build_monitoring_inputs(
                exports_root=context._exports_directory,
                snapshots_root=context._snapshots_directory,
                connection=connection,
                evidence_payload=evidence,
                window_start_utc=window_start,
                window_end_utc=window_end,
                as_of_utc=request.cutoff_utc,
            )
        report = evaluate_monitoring(
            inputs=inputs,
            policy=DEFAULT_MONITORING_POLICY,
            as_of_utc=request.cutoff_utc,
            window_start_utc=window_start,
            window_end_utc=window_end,
        )
        artifact = publish_monitoring_report(
            root=context._exports_directory,
            relative_directory=f"closed-loop-monitoring/{report.run_id}",
            report=report,
        )
        context.checkpoint()
        with connect_database(context._database_path) as connection:
            with transaction(connection, immediate=True):
                MonitoringRepository(connection).persist(
                    report=report,
                    artifact=artifact,
                    created_at=request.cutoff_utc,
                    actor="worker",
                )
    except (ArtifactError, MonitoringError, OSError, ValueError) as exc:
        raise PermanentJobError(str(exc)) from exc
    return {
        "state": "monitoring-refreshed",
        "run_id": report.run_id,
        "monitoring_artifact_id": artifact.artifact_id,
        "summary_status": report.summary_status.value,
    }


def evaluate_retraining_trigger_handler(
    context: JobExecutionContext,
    payload: JsonValue,
) -> JsonValue:
    request = _payload(payload, require_artifacts=True)
    if len(request.artifacts) != 1:
        raise PermanentJobError("retraining trigger requires one eligibility ledger")
    if context._database_path is None:
        raise PermanentJobError("retraining trigger requires a database")
    if context._exports_directory is None:
        raise PermanentJobError("retraining trigger requires an exports directory")
    policy = RetrainingPolicy()
    if request.policy_id != policy.policy_id:
        raise PermanentJobError("unknown retraining policy id")
    ledger = _resolve_training_ledger(context._exports_directory, request.artifacts[0])
    ledger_payload = require_dict(ledger.payload, field="training_eligibility_ledger")
    records = require_list(ledger_payload["records"], field="records")
    scoped = [
        require_dict(item, field="records[]")
        for item in records
        if isinstance(item, dict) and item.get("competition_id") == request.competition_id
    ]
    eligible = sum(item.get("state") == "eligible" for item in scoped)
    competitions = {
        require_str(item["competition_id"], field="competition_id")
        for item in records
        if isinstance(item, dict) and item.get("state") == "eligible"
    }
    with connect_database(context._database_path, read_only=True) as connection:
        active_rows = connection.execute(
            """
                SELECT payload_json FROM jobs
                WHERE job_type = ? AND status = 'running'
                """,
            (RUN_CHALLENGER_CYCLE_JOB_TYPE,),
        ).fetchall()
    active_jobs = 0
    for row in active_rows:
        try:
            active_payload = json.loads(str(row["payload_json"]))
        except json.JSONDecodeError as exc:
            raise PermanentJobError("active retraining job payload is malformed") from exc
        if isinstance(active_payload, dict) and active_payload.get("scope") == {
            "sport_code": request.sport_code,
            "competition_id": request.competition_id,
        }:
            active_jobs += 1
    decision = evaluate_retraining_trigger(
        policy=policy,
        evaluated_at_utc=request.cutoff_utc,
        eligible_new_matches=eligible,
        champion_created_at_utc=request.cutoff_utc,
        last_successful_tournament_at_utc=request.cutoff_utc,
        last_failed_cycle_at_utc=None,
        season_transition_detected=False,
        data_coverage=(eligible / len(scoped)) if scoped else 0.0,
        competition_count=len(competitions),
        active_jobs_for_scope=active_jobs,
    )
    return {
        "policy_id": decision.policy_id,
        "state": decision.state,
        "should_run": decision.should_run,
        "trigger_codes": list(decision.trigger_codes),
        "blocker_codes": list(decision.blocker_codes),
    }


def run_challenger_cycle_handler(
    context: JobExecutionContext,
    payload: JsonValue,
) -> JsonValue:
    """Build immutable challenger/tournament artifacts; never mutate the champion."""
    request = _payload(payload, require_artifacts=True)
    if len(request.artifacts) != 1:
        raise PermanentJobError("challenger cycle requires one eligibility ledger")
    if context._database_path is None or context._snapshots_directory is None:
        raise PermanentJobError("challenger cycle requires registered snapshot paths")
    if context._exports_directory is None:
        raise PermanentJobError("challenger cycle requires an exports directory")
    policy = RetrainingPolicy()
    if request.policy_id != policy.policy_id:
        raise PermanentJobError("unknown retraining policy id")
    ledger = _resolve_training_ledger(context._exports_directory, request.artifacts[0])
    ledger_payload = require_dict(ledger.payload, field="training_eligibility_ledger")
    eligible_event_ids = {
        require_str(item["canonical_event_id"], field="canonical_event_id")
        for raw in require_list(ledger_payload["records"], field="records")
        for item in (require_dict(raw, field="records[]"),)
        if item.get("state") == "eligible" and item.get("competition_id") == request.competition_id
    }
    manifest_paths = tuple(
        _registered_manifest(context, item, expected_snapshot_type="football-ingestion")
        for item in request.snapshots
    )
    events, _identities, _quotes = load_finished_events_from_snapshots(
        snapshots_directory=context._snapshots_directory,
        relative_manifest_paths=manifest_paths,
    )
    if any(item.competition_id != request.competition_id for item in events):
        raise PermanentJobError("challenger snapshot scope does not match payload scope")
    if any(item.canonical_event_id not in eligible_event_ids for item in events):
        raise PermanentJobError("challenger evidence is absent from the eligibility ledger")
    matches = tuple(
        ScoreTrainingMatch(
            canonical_event_id=item.canonical_event_id,
            competition_id=item.competition_id,
            event_date=item.event_date,
            home_team_id=item.home_canonical_participant_id,
            away_team_id=item.away_canonical_participant_id,
            home_goals=item.home_score,
            away_goals=item.away_score,
        )
        for item in events
    )
    split = TournamentSplitConfiguration(
        minimum_training_rows=500,
        calibration_rows=100,
        test_rows=100,
        maximum_folds=3,
    )
    tournament = run_unified_tournament(
        matches,
        split_configuration=split,
        score_configuration=ScoreModelConfiguration(maximum_grid_goals=24),
        provenance=EvaluationProvenance.VERIFIED_HISTORICAL,
    )
    source_snapshot_refs = tuple(
        sorted((item.snapshot_id, item.checksum_sha256) for item in request.snapshots)
    )
    ledger_reference = request.artifacts[0]
    cycle_id = content_addressed_id(
        identity_type="football-challenger-cycle-v1",
        payload={
            "snapshot_refs": [
                {"snapshot_id": snapshot_id, "checksum_sha256": checksum}
                for snapshot_id, checksum in source_snapshot_refs
            ],
            "training_evidence": {
                "artifact_id": ledger_reference.snapshot_id,
                "checksum_sha256": ledger_reference.checksum_sha256,
            },
            "policy_id": request.policy_id,
            "cutoff_utc": request.cutoff_utc.isoformat(),
            "scope": {
                "sport_code": request.sport_code,
                "competition_id": request.competition_id,
            },
        },
    )
    tournament_artifact = _publish_tournament_idempotently(
        root=context._exports_directory,
        relative_directory=f"challenger-cycles/{cycle_id}/tournament",
        tournament=tournament,
    )
    challenger = fit_covariate_dixon_coles(
        matches,
        score_configuration=ScoreModelConfiguration(maximum_grid_goals=24),
    )
    challenger_artifact = _publish_challenger_idempotently(
        root=context._exports_directory,
        relative_directory=f"challenger-cycles/{cycle_id}/challenger",
        model=challenger,
        ensemble_weights=(0.0, 0.0, 1.0),
        training_evidence_artifact_id=ledger_reference.snapshot_id,
        training_evidence_checksum_sha256=ledger_reference.checksum_sha256,
        source_snapshot_refs=source_snapshot_refs,
    )
    context.checkpoint()
    return {
        "cycle_id": cycle_id,
        "state": "challenger-generated-champion-unchanged",
        "challenger_artifact_id": challenger_artifact.artifact_id,
        "tournament_artifact_id": tournament_artifact.artifact_id,
        "provisional_winner_candidate_id": tournament.provisional_winner_candidate_id,
        "production_eligibility_state": tournament.production_eligibility_state,
        "promotion_state": tournament.promotion_state,
    }


def _publish_tournament_idempotently(
    *,
    root: Path,
    relative_directory: str,
    tournament: UnifiedTournament,
) -> AnalyticalArtifact:
    expected_id = unified_tournament_artifact_id(tournament)
    try:
        return write_unified_tournament_artifact(
            root=root,
            relative_directory=relative_directory,
            tournament=tournament,
        )
    except ArtifactError:
        try:
            existing = load_unified_tournament_artifact(
                root=root,
                relative_directory=relative_directory,
            )
        except ArtifactError as exc:
            raise PermanentJobError(
                "existing challenger tournament publication is not a valid replay"
            ) from exc
        if existing.artifact_id != expected_id:
            raise PermanentJobError(
                "existing challenger tournament publication conflicts with this cycle"
            ) from None
        return existing


def _publish_challenger_idempotently(
    *,
    root: Path,
    relative_directory: str,
    model: CovariateScoreModel,
    ensemble_weights: tuple[float, ...],
    training_evidence_artifact_id: str,
    training_evidence_checksum_sha256: str,
    source_snapshot_refs: tuple[tuple[str, str], ...],
) -> AnalyticalArtifact:
    expected_id = challenger_artifact_id(
        model=model,
        ensemble_weights=ensemble_weights,
        training_evidence_artifact_id=training_evidence_artifact_id,
        training_evidence_checksum_sha256=training_evidence_checksum_sha256,
        source_snapshot_refs=source_snapshot_refs,
    )
    try:
        return write_challenger_artifact(
            root=root,
            relative_directory=relative_directory,
            model=model,
            ensemble_weights=ensemble_weights,
            training_evidence_artifact_id=training_evidence_artifact_id,
            training_evidence_checksum_sha256=training_evidence_checksum_sha256,
            source_snapshot_refs=source_snapshot_refs,
        )
    except ArtifactError:
        try:
            existing = load_challenger_artifact(
                root=root,
                relative_directory=relative_directory,
            )
        except ArtifactError as exc:
            raise PermanentJobError(
                "existing challenger model publication is not a valid replay"
            ) from exc
        if existing.artifact_id != expected_id:
            raise PermanentJobError(
                "existing challenger model publication conflicts with this cycle"
            ) from None
        return existing


def _payload(
    value: JsonValue,
    *,
    require_one_snapshot: bool = False,
    require_artifacts: bool = False,
) -> _LifecyclePayload:
    row = require_dict(value, field="payload")
    expected = {"snapshot_refs", "scope", "policy_id", "cutoff_utc"}
    if require_artifacts:
        expected.add("artifact_refs")
    if set(row) != expected:
        raise PermanentJobError("lifecycle payload fields are not exact")
    snapshots: list[_SnapshotReference] = []
    for raw in require_list(row["snapshot_refs"], field="snapshot_refs"):
        item = require_dict(raw, field="snapshot_refs[]")
        if set(item) != {"snapshot_id", "checksum_sha256"}:
            raise PermanentJobError("snapshot reference fields are not exact")
        snapshots.append(
            _SnapshotReference(
                require_str(item["snapshot_id"], field="snapshot_id"),
                require_str(item["checksum_sha256"], field="checksum_sha256"),
            )
        )
    if not snapshots or (require_one_snapshot and len(snapshots) != 1):
        raise PermanentJobError("lifecycle payload contains an invalid snapshot count")
    artifacts: list[_SnapshotReference] = []
    for raw in (
        require_list(row["artifact_refs"], field="artifact_refs") if require_artifacts else []
    ):
        item = require_dict(raw, field="artifact_refs[]")
        if set(item) != {"artifact_id", "checksum_sha256"}:
            raise PermanentJobError("artifact reference fields are not exact")
        artifacts.append(
            _SnapshotReference(
                require_str(item["artifact_id"], field="artifact_id"),
                require_str(item["checksum_sha256"], field="checksum_sha256"),
            )
        )
    if require_artifacts and not artifacts:
        raise PermanentJobError("lifecycle payload requires exact artifact references")
    scope = require_dict(row["scope"], field="scope")
    if set(scope) != {"sport_code", "competition_id"}:
        raise PermanentJobError("lifecycle scope fields are not exact")
    return _LifecyclePayload(
        snapshots=tuple(snapshots),
        artifacts=tuple(artifacts),
        sport_code=require_str(scope["sport_code"], field="sport_code"),
        competition_id=require_str(scope["competition_id"], field="competition_id"),
        policy_id=require_str(row["policy_id"], field="policy_id"),
        cutoff_utc=require_canonical_utc_timestamp_string(
            row["cutoff_utc"],
            field="cutoff_utc",
        ),
    )


def _registered_manifest(
    context: JobExecutionContext,
    reference: _SnapshotReference,
    *,
    expected_snapshot_type: str,
) -> str:
    assert context._database_path is not None
    with connect_database(context._database_path, read_only=True) as connection:
        record = SnapshotRepository(connection).get_snapshot(reference.snapshot_id)
    if (
        record is None
        or record.status is not SnapshotStatus.READY
        or record.snapshot_type != expected_snapshot_type
        or record.checksum_sha256 != reference.checksum_sha256
    ):
        raise PermanentJobError("snapshot reference is not an exact registered READY snapshot")
    return record.relative_path


def _registered_results(
    context: JobExecutionContext,
    references: tuple[_SnapshotReference, ...],
) -> tuple[VerifiedResultSnapshot, ...]:
    assert context._database_path is not None
    assert context._snapshots_directory is not None
    snapshots: list[VerifiedResultSnapshot] = []
    with connect_database(context._database_path, read_only=True) as connection:
        for reference in references:
            row = connection.execute(
                """
                SELECT relative_path, checksum_sha256
                FROM result_snapshots
                WHERE id = ?
                """,
                (reference.snapshot_id,),
            ).fetchone()
            if row is None or str(row["checksum_sha256"]) != reference.checksum_sha256:
                raise PermanentJobError("result reference is not an exact registered snapshot")
            snapshots.append(
                load_result_snapshot(
                    root=context._snapshots_directory,
                    relative_directory=str(row["relative_path"]),
                    expected_snapshot_id=reference.snapshot_id,
                    expected_checksum=reference.checksum_sha256,
                )
            )
    return tuple(snapshots)


def _resolve_typed_artifact(
    exports_root: Path,
    reference: _SnapshotReference,
) -> TypedAnalyticalArtifact:
    matches: list[tuple[str, str]] = []
    for manifest_path in exports_root.rglob("manifest.json"):
        try:
            raw = manifest_path.read_bytes()
            manifest = json.loads(raw)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if (
            not isinstance(manifest, dict)
            or manifest.get("manifest_version") != "typed-analytical-artifact-v1"
            or manifest.get("artifact_id") != reference.snapshot_id
            or manifest.get("artifact_kind") != "analysis"
            or manifest.get("schema_version") != ANALYSIS_ARTIFACT_SCHEMA
        ):
            continue
        checksum = hashlib.sha256(raw).hexdigest()
        if checksum != reference.checksum_sha256:
            raise PermanentJobError("analysis artifact checksum does not match its exact reference")
        matches.append(
            (
                manifest_path.parent.relative_to(exports_root).as_posix(),
                checksum,
            )
        )
    if len(matches) != 1:
        raise PermanentJobError("analysis artifact reference is absent or ambiguous")
    relative, checksum = matches[0]
    return load_typed_analytical_artifact(
        root=exports_root,
        relative_directory=relative,
        expected_kind="analysis",
        expected_schema_version=ANALYSIS_ARTIFACT_SCHEMA,
        expected_checksum=checksum,
        expected_artifact_id=reference.snapshot_id,
    )


def _resolve_training_ledger(
    exports_root: Path,
    reference: _SnapshotReference,
) -> AnalyticalArtifact:
    matches: list[str] = []
    for manifest_path in exports_root.rglob("manifest.json"):
        try:
            raw = manifest_path.read_bytes()
            manifest = json.loads(raw)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if (
            not isinstance(manifest, dict)
            or manifest.get("manifest_version") != "analytical-artifact-manifest-v1"
            or manifest.get("artifact_id") != reference.snapshot_id
            or manifest.get("artifact_type") != "training-eligibility-ledger"
            or manifest.get("schema_version") != "training-eligibility-ledger-v1"
        ):
            continue
        if hashlib.sha256(raw).hexdigest() != reference.checksum_sha256:
            raise PermanentJobError("eligibility ledger checksum does not match its reference")
        matches.append(manifest_path.parent.relative_to(exports_root).as_posix())
    if len(matches) != 1:
        raise PermanentJobError("eligibility ledger reference is absent or ambiguous")
    return load_training_eligibility_ledger(
        root=exports_root,
        relative_directory=matches[0],
        expected_checksum=reference.checksum_sha256,
    )
