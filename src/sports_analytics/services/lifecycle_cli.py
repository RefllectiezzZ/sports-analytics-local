"""Granular safe operator CLI for PR #13 lifecycle and policy boundaries."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path

from sports_analytics.artifacts import AnalyticalArtifact, load_analytical_artifact
from sports_analytics.core.exceptions import SportsAnalyticsError
from sports_analytics.core.runtime import RuntimeContext, bootstrap_runtime
from sports_analytics.data.database import connect_database, transaction
from sports_analytics.data.repositories.snapshots import SnapshotRepository
from sports_analytics.data.types import JsonValue
from sports_analytics.features.football.real_audit import (
    audit_verified_real_snapshots,
    load_real_data_audit,
    write_real_data_audit,
)
from sports_analytics.governance.contracts import ModelRole
from sports_analytics.governance.repository import ModelGovernanceRepository
from sports_analytics.jobs.context import JobExecutionContext
from sports_analytics.learning.jobs import run_challenger_cycle_handler
from sports_analytics.learning.lifecycle import (
    RetrainingPolicy,
    evaluate_retraining_trigger,
    load_training_eligibility_ledger,
)
from sports_analytics.learning.offline_proof import (
    OfflineClosedLoopProof,
    load_offline_closed_loop_proof,
    write_offline_closed_loop_proof,
)
from sports_analytics.models.football_challengers import load_challenger_artifact
from sports_analytics.models.football_unified_tournament import (
    load_unified_tournament_artifact,
)
from sports_analytics.models.identity import content_addressed_id
from sports_analytics.players.evidence import (
    PlayerEvidenceBundle,
    load_player_evidence_artifact,
    parse_player_import_bundle_csv,
    parse_player_import_bundle_json,
    player_capability_matrix,
    player_csv_template,
    player_json_template,
    publish_player_evidence_artifact,
)
from sports_analytics.policies.proposal import (
    PublishedProposalPolicy,
    load_published_proposal_policy,
    parse_proposal_policy,
    proposal_policy_template,
    publish_proposal_policy,
)
from sports_analytics.results.football_snapshot_bridge import (
    register_completed_results_from_snapshot,
)
from sports_analytics.services.football_product import (
    FOOTBALL_PRODUCT_READ_MODEL_SCHEMA,
    FOOTBALL_PRODUCT_READ_MODEL_TYPE,
)
from sports_analytics.services.football_product_cli import run_football_product_json
from sports_analytics.services.training import verify_model_artifact

SUCCESS = 0
INVALID = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/settings.toml")
    modes = parser.add_mutually_exclusive_group(required=True)
    for name in (
        "audit-real-historical-data",
        "verify-data-audit",
        "register-completed-results",
        "list-training-eligible-results",
        "inspect-retraining-trigger",
        "run-challenger-cycle",
        "verify-challenger-cycle",
        "compare-challenger-with-champion",
        "apply-explicit-promotion",
        "verify-champion",
        "rollback-promotion",
        "export-player-csv-template",
        "export-player-json-template",
        "validate-player-evidence",
        "import-player-evidence",
        "verify-player-artifact",
        "inspect-player-capability",
        "export-proposal-policy-template",
        "validate-proposal-policy",
        "publish-proposal-policy",
        "verify-proposal-policy",
        "run-product-from-policy",
        "verify-product-artifact",
        "run-offline-closed-loop-proof",
        "verify-offline-closed-loop-proof",
    ):
        modes.add_argument(f"--{name}", action="store_true")
    parser.add_argument("--snapshot-id")
    parser.add_argument("--competition")
    parser.add_argument("--input")
    parser.add_argument("--artifact")
    parser.add_argument("--policy-artifact")
    parser.add_argument("--decision-id")
    parser.add_argument("--transition-id")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        if args.export_player_csv_template:
            print(player_csv_template(), end="")
            return SUCCESS
        if args.export_player_json_template:
            print(player_json_template(), end="")
            return SUCCESS
        if args.export_proposal_policy_template:
            print(proposal_policy_template(), end="")
            return SUCCESS
        if args.inspect_player_capability:
            _print({"capabilities": list(player_capability_matrix())})
            return SUCCESS
        runtime = bootstrap_runtime("engine", config_path=args.config)
        if args.audit_real_historical_data:
            return _audit(runtime)
        if args.verify_data_audit:
            artifact = load_real_data_audit(
                root=runtime.paths.exports_directory,
                relative_directory=_required(args.artifact, "--artifact"),
            )
            return _verified(artifact)
        if args.register_completed_results:
            return _register_results(runtime, _required(args.snapshot_id, "--snapshot-id"))
        if args.list_training_eligible_results:
            return _list_results(runtime)
        if args.inspect_retraining_trigger:
            return _inspect_trigger(
                runtime,
                _required(args.artifact, "--artifact"),
            )
        if args.run_challenger_cycle:
            return _run_cycle(
                runtime,
                _required(args.competition, "--competition"),
                _required(args.artifact, "--artifact"),
            )
        if args.verify_challenger_cycle:
            artifact = load_challenger_artifact(
                root=runtime.paths.exports_directory,
                relative_directory=_required(args.artifact, "--artifact"),
            )
            return _verified(artifact)
        if args.compare_challenger_with_champion:
            artifact = load_unified_tournament_artifact(
                root=runtime.paths.exports_directory,
                relative_directory=_required(args.artifact, "--artifact"),
            )
            _print(
                {
                    "artifact_id": artifact.artifact_id,
                    "comparison_state": "verified-manual-governance-required",
                    "payload": artifact.payload,
                }
            )
            return SUCCESS
        if args.apply_explicit_promotion:
            return _apply_promotion(runtime, _required(args.decision_id, "--decision-id"))
        if args.verify_champion:
            return _verify_champions(runtime)
        if args.rollback_promotion:
            return _rollback_promotion(
                runtime,
                _required(args.transition_id, "--transition-id"),
            )
        if args.validate_player_evidence:
            bundle = _parse_player_file(_required(args.input, "--input"))
            _print(
                {
                    "state": "valid",
                    "players": len(bundle.players),
                    "unresolved_players": sum(
                        item.canonical_player_id is None for item in bundle.observations
                    ),
                    "observations": len(bundle.observations),
                }
            )
            return SUCCESS
        if args.import_player_evidence:
            return _import_players(runtime, _required(args.input, "--input"))
        if args.verify_player_artifact:
            artifact, bundle = load_player_evidence_artifact(
                root=runtime.paths.exports_directory,
                relative_directory=_required(args.artifact, "--artifact"),
            )
            _print(
                {
                    "state": "verified",
                    "artifact_id": artifact.artifact_id,
                    "model_use_state": bundle.model_use_state,
                }
            )
            return SUCCESS
        if args.validate_proposal_policy:
            policy = _read_policy(_required(args.input, "--input"))
            _print({"state": "valid", "configuration_id": policy.configuration_id})
            return SUCCESS
        if args.publish_proposal_policy:
            policy = _read_policy(_required(args.input, "--input"))
            artifact = publish_proposal_policy(
                root=runtime.paths.exports_directory,
                relative_directory=f"proposal-policies/{policy.configuration_id}",
                policy=policy,
            )
            return _verified(artifact)
        if args.verify_proposal_policy:
            artifact, policy = load_published_proposal_policy(
                root=runtime.paths.exports_directory,
                relative_directory=_required(args.artifact, "--artifact"),
            )
            _print(
                {
                    "state": "verified",
                    "artifact_id": artifact.artifact_id,
                    "configuration_id": policy.configuration_id,
                }
            )
            return SUCCESS
        if args.run_product_from_policy:
            policy_artifact, policy = load_published_proposal_policy(
                root=runtime.paths.exports_directory,
                relative_directory=_required(args.policy_artifact, "--policy-artifact"),
            )
            result = run_football_product_json(
                path_text=_required(args.input, "--input"),
                exports_root=runtime.paths.exports_directory,
                published_policy=policy,
                published_policy_artifact_id=policy_artifact.artifact_id,
            )
            _print(result)
            return SUCCESS
        if args.verify_product_artifact:
            artifact = load_analytical_artifact(
                root=runtime.paths.exports_directory,
                relative_directory=_required(args.artifact, "--artifact"),
                expected_artifact_type=FOOTBALL_PRODUCT_READ_MODEL_TYPE,
                expected_schema_version=FOOTBALL_PRODUCT_READ_MODEL_SCHEMA,
            )
            return _verified(artifact)
        if args.run_offline_closed_loop_proof:
            return _run_offline_proof(runtime)
        if args.verify_offline_closed_loop_proof:
            artifact = load_offline_closed_loop_proof(
                root=runtime.paths.exports_directory,
                relative_directory=_required(args.artifact, "--artifact"),
            )
            return _verified(artifact)
    except (SportsAnalyticsError, OSError, ValueError, TypeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return INVALID
    parser.error("unhandled mode")
    return INVALID


def _audit(runtime: RuntimeContext) -> int:
    paths = runtime.paths
    with connect_database(runtime.database_path, read_only=True) as connection:
        records = SnapshotRepository(connection).list_snapshots(snapshot_type="football-ingestion")
    manifests = tuple(
        item.relative_path
        for item in records
        if item.status.value == "ready" and item.source_name == "football-data-co-uk"
    )
    audits = audit_verified_real_snapshots(
        snapshots_directory=paths.snapshots_directory,
        relative_manifest_paths=manifests,
    )
    identity = content_addressed_id(
        identity_type="real-football-audit-run-v1",
        payload={"snapshot_ids": [item.snapshot_id for item in audits]},
    )
    artifact = write_real_data_audit(
        root=paths.exports_directory,
        relative_directory=f"real-football-audits/{identity}",
        audits=audits,
    )
    return _verified(artifact)


def _register_results(runtime: RuntimeContext, snapshot_id: str) -> int:
    with connect_database(runtime.database_path, read_only=True) as connection:
        record = SnapshotRepository(connection).get_snapshot(snapshot_id)
    if record is None or record.checksum_sha256 is None:
        raise ValueError("snapshot is not registered READY evidence")
    report = register_completed_results_from_snapshot(
        database_path=runtime.database_path,
        snapshots_directory=runtime.paths.snapshots_directory,
        relative_manifest_path=record.relative_path,
        output_relative_root="canonical-results/from-football-snapshot",
        registered_at=runtime.started_at,
        actor="operator-cli",
    )
    _print(
        {
            "state": "registered",
            "source_snapshot_id": report.source_snapshot_id,
            "completed_events": report.completed_events,
            "skipped_events": report.skipped_events,
        }
    )
    return SUCCESS


def _list_results(runtime: RuntimeContext) -> int:
    with connect_database(runtime.database_path, read_only=True) as connection:
        total = int(connection.execute("SELECT COUNT(*) FROM result_snapshots").fetchone()[0])
    _print(
        {
            "registered_results": total,
            "eligibility_state": (
                "inspect-latest-training-eligibility-artifact" if total else "result-unverified"
            ),
        }
    )
    return SUCCESS


def _inspect_trigger(runtime: RuntimeContext, ledger_relative: str) -> int:
    policy = RetrainingPolicy()
    artifact = load_training_eligibility_ledger(
        root=runtime.paths.exports_directory,
        relative_directory=ledger_relative,
    )
    assert isinstance(artifact.payload, dict)
    records = artifact.payload["records"]
    assert isinstance(records, list)
    result_count = sum(
        isinstance(item, dict) and item.get("state") == "eligible" for item in records
    )
    competitions = {
        str(item["competition_id"])
        for item in records
        if isinstance(item, dict) and item.get("state") == "eligible"
    }
    now = runtime.started_at
    decision = evaluate_retraining_trigger(
        policy=policy,
        evaluated_at_utc=now,
        eligible_new_matches=result_count,
        champion_created_at_utc=now,
        last_successful_tournament_at_utc=now,
        last_failed_cycle_at_utc=None,
        season_transition_detected=False,
        data_coverage=(result_count / len(records)) if records else 0.0,
        competition_count=len(competitions),
        active_jobs_for_scope=0,
    )
    _print(
        {
            "state": decision.state,
            "policy_id": decision.policy_id,
            "trigger_codes": list(decision.trigger_codes),
            "blocker_codes": list(decision.blocker_codes),
        }
    )
    return SUCCESS


def _run_cycle(runtime: RuntimeContext, competition: str, ledger_relative: str) -> int:
    with connect_database(runtime.database_path, read_only=True) as connection:
        records = [
            item
            for item in SnapshotRepository(connection).list_snapshots(
                snapshot_type="football-ingestion"
            )
            if item.status.value == "ready" and f"/{competition}/" in item.relative_path
        ]
    policy = RetrainingPolicy()
    ledger = load_training_eligibility_ledger(
        root=runtime.paths.exports_directory,
        relative_directory=ledger_relative,
    )
    cutoff = runtime.started_at
    context = JobExecutionContext(
        job_id="11111111-1111-4111-8111-111111111111",
        worker_id="22222222-2222-4222-8222-222222222222",
        attempt=1,
        maximum_attempts=1,
        claimed_at=cutoff,
        lease_expires_at=cutoff + timedelta(minutes=30),
        logger=logging.getLogger("sports_analytics.lifecycle_cli"),
    )
    context.bind_runtime(runtime)
    payload: JsonValue = {
        "snapshot_refs": [
            {"snapshot_id": item.id, "checksum_sha256": item.checksum_sha256}
            for item in sorted(records, key=lambda item: item.relative_path)
        ],
        "artifact_refs": [
            {
                "artifact_id": ledger.artifact_id,
                "checksum_sha256": ledger.checksum_sha256,
            }
        ],
        "scope": {"sport_code": "football", "competition_id": competition},
        "policy_id": policy.policy_id,
        "cutoff_utc": cutoff.isoformat().replace("+00:00", "Z"),
    }
    _print(run_challenger_cycle_handler(context, payload))
    return SUCCESS


def _parse_player_file(path_text: str) -> PlayerEvidenceBundle:
    path = Path(path_text)
    text = path.read_text(encoding="utf-8")
    return (
        parse_player_import_bundle_csv(text)
        if path.suffix.lower() == ".csv"
        else parse_player_import_bundle_json(text)
    )


def _import_players(runtime: RuntimeContext, path_text: str) -> int:
    bundle = _parse_player_file(path_text)
    identity = content_addressed_id(
        identity_type="operator-player-import-v1",
        payload={"observation_ids": [item.observation_id for item in bundle.observations]},
    )
    artifact = publish_player_evidence_artifact(
        root=runtime.paths.exports_directory,
        relative_directory=f"player-evidence/{identity}",
        bundle=bundle,
    )
    return _verified(artifact)


def _read_policy(path_text: str) -> PublishedProposalPolicy:
    return parse_proposal_policy(json.loads(Path(path_text).read_text(encoding="utf-8")))


def _run_offline_proof(runtime: RuntimeContext) -> int:
    def identity(stage: str) -> str:
        return content_addressed_id(
            identity_type="closed-loop-synthetic-stage-v1",
            payload={"stage": stage, "provenance": "synthetic-contract"},
        )

    model_a = identity("model-artifact-a")
    proof = OfflineClosedLoopProof(
        model_artifact_a=model_a,
        prediction_artifact_a=identity("prediction-artifact-a"),
        result_snapshot_id=identity("verified-result-snapshot"),
        settlement_artifact_id=identity("analytical-settlement"),
        monitoring_artifact_id=identity("monitoring"),
        training_ledger_artifact_id=identity("training-eligibility"),
        challenger_artifact_b=identity("challenger-artifact-b"),
        tournament_artifact_id=identity("challenger-evaluation"),
        prediction_artifact_b=identity("prediction-artifact-b"),
        final_champion_artifact_id=model_a,
    )
    run_id = content_addressed_id(
        identity_type="closed-loop-offline-proof-run-v1",
        payload={"stages": list(proof.stages), "provenance": proof.provenance},
    )
    artifact = write_offline_closed_loop_proof(
        root=runtime.paths.exports_directory,
        relative_directory=f"closed-loop-proofs/{run_id}",
        proof=proof,
    )
    load_offline_closed_loop_proof(
        root=runtime.paths.exports_directory,
        relative_directory=f"closed-loop-proofs/{run_id}",
        expected_checksum=artifact.checksum_sha256,
    )
    return _verified(artifact)


def _apply_promotion(runtime: RuntimeContext, decision_id: str) -> int:
    with connect_database(runtime.database_path) as connection:
        with transaction(connection, immediate=True):
            transition_id = ModelGovernanceRepository(connection).apply_promotion(
                decision_id=decision_id,
                actor="operator-cli",
                occurred_at=runtime.started_at,
            )
    _print(
        {
            "state": "explicit-promotion-applied",
            "transition_id": transition_id,
            "automatic_promotion": False,
        }
    )
    return SUCCESS


def _verify_champions(runtime: RuntimeContext) -> int:
    with connect_database(runtime.database_path, read_only=True) as connection:
        champions = tuple(
            item
            for item in ModelGovernanceRepository(connection).list_models()
            if item.role is ModelRole.CHAMPION
        )
    if not champions:
        _print({"state": "no-production-champion", "champions": []})
        return INVALID
    verified: list[dict[str, JsonValue]] = []
    for item in champions:
        artifact = verify_model_artifact(
            paths=runtime.paths,
            relative_path=item.model_relative_path,
            expected_checksum=item.model_checksum_sha256,
        )
        verified.append(
            {
                "model_artifact_id": item.model_artifact_id,
                "checksum_sha256": artifact.checksum_sha256,
                "sport_code": item.sport_code,
                "market_key": item.market_key,
                "version": item.version,
            }
        )
    _print({"state": "verified", "champions": verified})
    return SUCCESS


def _rollback_promotion(runtime: RuntimeContext, transition_id: str) -> int:
    with connect_database(runtime.database_path) as connection:
        with transaction(connection, immediate=True):
            rollback_id = ModelGovernanceRepository(connection).rollback_transition(
                transition_id=transition_id,
                actor="operator-cli",
                occurred_at=runtime.started_at,
            )
    _print(
        {
            "state": "rollback-applied",
            "transition_id": rollback_id,
            "rollback_of_transition_id": transition_id,
        }
    )
    return SUCCESS


def _verified(artifact: AnalyticalArtifact) -> int:
    _print(
        {
            "state": "verified",
            "artifact_id": artifact.artifact_id,
            "checksum_sha256": artifact.checksum_sha256,
        }
    )
    return SUCCESS


def _required(value: str | None, flag: str) -> str:
    if value is None:
        raise ValueError(f"{flag} is required")
    return value


def _print(value: object) -> None:
    print(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str))


if __name__ == "__main__":
    raise SystemExit(main())
