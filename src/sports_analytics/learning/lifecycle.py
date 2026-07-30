"""Deterministic training eligibility, retraining, and promotion governance."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Final

from sports_analytics.artifacts import (
    AnalyticalArtifact,
    load_analytical_artifact,
    write_analytical_artifact,
)
from sports_analytics.core.exceptions import ArtifactError, ModelError
from sports_analytics.data.codec import format_utc_timestamp
from sports_analytics.data.types import JsonValue, validate_sha256_checksum
from sports_analytics.models.identity import content_addressed_id
from sports_analytics.results.snapshots import VerifiedResultSnapshot
from sports_analytics.sports.contracts import require_utc

TRAINING_LEDGER_TYPE: Final[str] = "training-eligibility-ledger"
TRAINING_LEDGER_SCHEMA: Final[str] = "training-eligibility-ledger-v1"
RETRAINING_POLICY_SCHEMA: Final[str] = "football-retraining-policy-v1"
CHAMPION_HISTORY_TYPE: Final[str] = "model-champion-history"
CHAMPION_HISTORY_SCHEMA: Final[str] = "model-champion-history-v1"


class TrainingEligibilityState(StrEnum):
    ELIGIBLE = "eligible"
    RESULT_UNVERIFIED = "result-unverified"
    DUPLICATE_CONFLICT = "duplicate-conflict"
    MISSING_REQUIRED_FEATURE_HISTORY = "missing-required-feature-history"
    FEATURE_CUTOFF_INVALID = "feature-cutoff-invalid"
    EVENT_IDENTITY_UNRESOLVED = "event-identity-unresolved"
    EXCLUDED_COMPETITION = "excluded-competition"
    INSUFFICIENT_SEASON_CONTEXT = "insufficient-season-context"
    ALREADY_IN_TRAINING_SNAPSHOT = "already-in-training-snapshot"


@dataclass(frozen=True, slots=True, order=True)
class TrainingEligibilityRecord:
    canonical_event_id: str
    competition_id: str
    result_snapshot_id: str
    result_checksum_sha256: str
    pre_match_feature_artifact_id: str | None
    state: TrainingEligibilityState
    reason_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        for value, field in (
            (self.canonical_event_id, "canonical_event_id"),
            (self.competition_id, "competition_id"),
            (self.result_snapshot_id, "result_snapshot_id"),
        ):
            _text(value, field)
        _checksum(self.result_checksum_sha256, "result_checksum_sha256")
        if self.state is TrainingEligibilityState.ELIGIBLE:
            if self.pre_match_feature_artifact_id is None or self.reason_codes:
                raise ModelError("eligible training evidence requires features and no rejection")
        elif not self.reason_codes:
            raise ModelError("ineligible training evidence requires exact reason codes")

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "canonical_event_id": self.canonical_event_id,
            "competition_id": self.competition_id,
            "result_snapshot_id": self.result_snapshot_id,
            "result_checksum_sha256": self.result_checksum_sha256,
            "pre_match_feature_artifact_id": self.pre_match_feature_artifact_id,
            "state": self.state.value,
            "reason_codes": list(self.reason_codes),
        }


@dataclass(frozen=True, slots=True)
class TrainingEligibilityLedger:
    cutoff_utc: datetime
    source_snapshot_ids: tuple[str, ...]
    records: tuple[TrainingEligibilityRecord, ...]

    @property
    def eligible_records(self) -> tuple[TrainingEligibilityRecord, ...]:
        return tuple(
            item for item in self.records if item.state is TrainingEligibilityState.ELIGIBLE
        )


def build_training_eligibility_ledger(
    *,
    result_snapshots: tuple[VerifiedResultSnapshot, ...],
    event_competitions: dict[str, str],
    pre_match_feature_artifact_ids: dict[str, str],
    allowed_competitions: frozenset[str],
    already_trained_event_ids: frozenset[str],
    cutoff_utc: datetime,
) -> TrainingEligibilityLedger:
    """Classify only verified completed results with pre-match evidence."""
    cutoff = require_utc(cutoff_utc, field_name="cutoff_utc")
    records: list[TrainingEligibilityRecord] = []
    seen: dict[str, VerifiedResultSnapshot] = {}
    for snapshot in sorted(result_snapshots, key=lambda item: item.result.canonical_event_id):
        result = snapshot.result
        event_id = result.canonical_event_id
        previous = seen.get(event_id)
        if previous is not None and previous.snapshot_id != snapshot.snapshot_id:
            records.append(
                _record(
                    snapshot,
                    event_competitions.get(event_id, "unresolved"),
                    pre_match_feature_artifact_ids.get(event_id),
                    TrainingEligibilityState.DUPLICATE_CONFLICT,
                    ("duplicate-conflicting-final-evidence",),
                )
            )
            continue
        seen[event_id] = snapshot
        competition = event_competitions.get(event_id)
        feature_id = pre_match_feature_artifact_ids.get(event_id)
        reasons: tuple[str, ...]
        if competition is None:
            state = TrainingEligibilityState.EVENT_IDENTITY_UNRESOLVED
            reasons = ("event-competition-unresolved",)
            competition = "unresolved"
        elif competition not in allowed_competitions:
            state = TrainingEligibilityState.EXCLUDED_COMPETITION
            reasons = ("competition-not-in-training-scope",)
        elif event_id in already_trained_event_ids:
            state = TrainingEligibilityState.ALREADY_IN_TRAINING_SNAPSHOT
            reasons = ("event-already-consumed-by-training-snapshot",)
        elif result.result_timestamp_utc is None or result.result_timestamp_utc > cutoff:
            state = TrainingEligibilityState.RESULT_UNVERIFIED
            reasons = ("result-not-verified-by-ledger-cutoff",)
        elif feature_id is None:
            state = TrainingEligibilityState.MISSING_REQUIRED_FEATURE_HISTORY
            reasons = ("pre-match-feature-artifact-missing",)
        else:
            state = TrainingEligibilityState.ELIGIBLE
            reasons = ()
        records.append(_record(snapshot, competition, feature_id, state, reasons))
    return TrainingEligibilityLedger(
        cutoff_utc=cutoff,
        source_snapshot_ids=tuple(sorted(item.snapshot_id for item in result_snapshots)),
        records=tuple(sorted(records)),
    )


def write_training_eligibility_ledger(
    *,
    root: Path,
    relative_directory: str,
    ledger: TrainingEligibilityLedger,
) -> AnalyticalArtifact:
    return write_analytical_artifact(
        root=root,
        relative_directory=relative_directory,
        artifact_type=TRAINING_LEDGER_TYPE,
        schema_version=TRAINING_LEDGER_SCHEMA,
        payload={
            "cutoff_utc": format_utc_timestamp(ledger.cutoff_utc),
            "source_snapshot_ids": list(ledger.source_snapshot_ids),
            "records": [item.to_json() for item in ledger.records],
        },
    )


def load_training_eligibility_ledger(
    *,
    root: Path,
    relative_directory: str,
    expected_checksum: str | None = None,
) -> AnalyticalArtifact:
    artifact = load_analytical_artifact(
        root=root,
        relative_directory=relative_directory,
        expected_artifact_type=TRAINING_LEDGER_TYPE,
        expected_schema_version=TRAINING_LEDGER_SCHEMA,
        expected_checksum=expected_checksum,
    )
    payload = artifact.payload
    if not isinstance(payload, dict) or set(payload) != {
        "cutoff_utc",
        "source_snapshot_ids",
        "records",
    }:
        raise ArtifactError("training eligibility ledger fields are not exact")
    if not isinstance(payload["records"], list):
        raise ArtifactError("training eligibility records are invalid")
    for value in payload["records"]:
        if not isinstance(value, dict) or set(value) != {
            "canonical_event_id",
            "competition_id",
            "result_snapshot_id",
            "result_checksum_sha256",
            "pre_match_feature_artifact_id",
            "state",
            "reason_codes",
        }:
            raise ArtifactError("training eligibility record fields are not exact")
        try:
            TrainingEligibilityState(str(value["state"]))
        except ValueError as exc:
            raise ArtifactError("training eligibility state is invalid") from exc
    return artifact


@dataclass(frozen=True, slots=True)
class RetrainingPolicy:
    """Conservative immutable periodic retraining policy."""

    minimum_newly_completed_eligible_matches: int = 100
    maximum_champion_age_days: int = 180
    maximum_days_since_successful_tournament: int = 45
    season_transition_trigger: bool = True
    minimum_data_coverage: float = 0.95
    minimum_competitions: int = 1
    failed_retraining_cooldown_days: int = 7
    maximum_active_jobs_per_scope: int = 1
    strict_policy_auto_promotion: bool = False
    schema_version: str = RETRAINING_POLICY_SCHEMA

    def __post_init__(self) -> None:
        for field in (
            "minimum_newly_completed_eligible_matches",
            "maximum_champion_age_days",
            "maximum_days_since_successful_tournament",
            "minimum_competitions",
            "failed_retraining_cooldown_days",
            "maximum_active_jobs_per_scope",
        ):
            value = getattr(self, field)
            if type(value) is not int or value < 1:
                raise ModelError(f"{field} must be a positive integer")
        if not 0.0 <= self.minimum_data_coverage <= 1.0:
            raise ModelError("minimum_data_coverage must lie in [0, 1]")
        if self.maximum_active_jobs_per_scope != 1:
            raise ModelError("at most one active retraining job is permitted per scope")
        if self.strict_policy_auto_promotion:
            raise ModelError("automatic promotion is not enabled by the default reviewed policy")

    @property
    def policy_id(self) -> str:
        return content_addressed_id(
            identity_type=RETRAINING_POLICY_SCHEMA,
            payload=self.to_json(),
        )

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "minimum_newly_completed_eligible_matches": (
                self.minimum_newly_completed_eligible_matches
            ),
            "maximum_champion_age_days": self.maximum_champion_age_days,
            "maximum_days_since_successful_tournament": (
                self.maximum_days_since_successful_tournament
            ),
            "season_transition_trigger": self.season_transition_trigger,
            "minimum_data_coverage": self.minimum_data_coverage,
            "minimum_competitions": self.minimum_competitions,
            "failed_retraining_cooldown_days": self.failed_retraining_cooldown_days,
            "maximum_active_jobs_per_scope": self.maximum_active_jobs_per_scope,
            "strict_policy_auto_promotion": self.strict_policy_auto_promotion,
        }


@dataclass(frozen=True, slots=True)
class RetrainingDecision:
    should_run: bool
    state: str
    trigger_codes: tuple[str, ...]
    blocker_codes: tuple[str, ...]
    policy_id: str


def evaluate_retraining_trigger(
    *,
    policy: RetrainingPolicy,
    evaluated_at_utc: datetime,
    eligible_new_matches: int,
    champion_created_at_utc: datetime,
    last_successful_tournament_at_utc: datetime,
    last_failed_cycle_at_utc: datetime | None,
    season_transition_detected: bool,
    data_coverage: float,
    competition_count: int,
    active_jobs_for_scope: int,
) -> RetrainingDecision:
    now = require_utc(evaluated_at_utc, field_name="evaluated_at_utc")
    champion_created = require_utc(champion_created_at_utc, field_name="champion_created_at_utc")
    last_success = require_utc(
        last_successful_tournament_at_utc,
        field_name="last_successful_tournament_at_utc",
    )
    triggers: list[str] = []
    blockers: list[str] = []
    if eligible_new_matches >= policy.minimum_newly_completed_eligible_matches:
        triggers.append("minimum-newly-completed-eligible-matches")
    if now - champion_created >= timedelta(days=policy.maximum_champion_age_days):
        triggers.append("maximum-champion-age")
    if now - last_success >= timedelta(days=policy.maximum_days_since_successful_tournament):
        triggers.append("maximum-days-since-successful-tournament")
    if policy.season_transition_trigger and season_transition_detected:
        triggers.append("season-transition")
    if data_coverage < policy.minimum_data_coverage:
        blockers.append("minimum-data-coverage-not-met")
    if competition_count < policy.minimum_competitions:
        blockers.append("minimum-competitions-not-met")
    if active_jobs_for_scope >= policy.maximum_active_jobs_per_scope:
        blockers.append("retraining-job-already-active")
    if last_failed_cycle_at_utc is not None:
        last_failed = require_utc(
            last_failed_cycle_at_utc,
            field_name="last_failed_cycle_at_utc",
        )
        if now - last_failed < timedelta(days=policy.failed_retraining_cooldown_days):
            blockers.append("failed-retraining-cooldown-active")
    should_run = bool(triggers) and not blockers
    return RetrainingDecision(
        should_run=should_run,
        state="retraining-due" if should_run else "retraining-held",
        trigger_codes=tuple(sorted(triggers)),
        blocker_codes=tuple(sorted(blockers)),
        policy_id=policy.policy_id,
    )


@dataclass(frozen=True, slots=True, order=True)
class ChampionRevision:
    revision: int
    model_artifact_id: str
    model_checksum_sha256: str
    training_evidence_artifact_id: str
    tournament_artifact_id: str
    effective_at_utc: datetime
    action: str
    previous_model_artifact_id: str | None

    def __post_init__(self) -> None:
        if type(self.revision) is not int or self.revision < 1:
            raise ModelError("champion revision must be positive")
        for value, field in (
            (self.model_artifact_id, "model_artifact_id"),
            (self.training_evidence_artifact_id, "training_evidence_artifact_id"),
            (self.tournament_artifact_id, "tournament_artifact_id"),
        ):
            _text(value, field)
        _checksum(self.model_checksum_sha256, "model_checksum_sha256")
        require_utc(self.effective_at_utc, field_name="effective_at_utc")
        if self.action not in {"initial", "manual-promotion", "rollback"}:
            raise ModelError("champion revision action is invalid")


@dataclass(frozen=True, slots=True)
class ChampionHistory:
    scope_id: str
    revisions: tuple[ChampionRevision, ...]
    governance_mode: str = "manual-promotion"

    def __post_init__(self) -> None:
        _text(self.scope_id, "scope_id")
        if self.governance_mode != "manual-promotion" or not self.revisions:
            raise ModelError("champion history requires manual governance and a revision")
        expected = tuple(range(1, len(self.revisions) + 1))
        if tuple(item.revision for item in self.revisions) != expected:
            raise ModelError("champion history revisions must be contiguous")

    @property
    def champion(self) -> ChampionRevision:
        return self.revisions[-1]


def promote_challenger(
    history: ChampionHistory,
    *,
    challenger_model_artifact_id: str,
    challenger_checksum_sha256: str,
    training_evidence_artifact_id: str,
    tournament_artifact_id: str,
    promoted_at_utc: datetime,
    evidence_gate_state: str,
    compatible_scope: bool,
    verified_artifact: bool,
    confidence_intervals_valid: bool,
    proper_score_improved: bool,
    calibration_regressed: bool,
    coverage_regressed: bool,
    severe_competition_regression: bool,
    rho_stable: bool,
) -> ChampionHistory:
    """Apply an explicit manual promotion only when every strict gate passes."""
    failed = [
        code
        for code, passed in (
            ("production-evidence-gate-failed", evidence_gate_state == "production-eligible"),
            ("scope-incompatible", compatible_scope),
            ("artifact-unverified", verified_artifact),
            ("confidence-interval-invalid", confidence_intervals_valid),
            ("proper-score-not-improved", proper_score_improved),
            ("calibration-regressed", not calibration_regressed),
            ("coverage-regressed", not coverage_regressed),
            ("competition-regression-severe", not severe_competition_regression),
            ("rho-unstable", rho_stable),
        )
        if not passed
    ]
    if failed:
        raise ModelError(f"manual promotion rejected: {','.join(failed)}")
    revision = ChampionRevision(
        revision=len(history.revisions) + 1,
        model_artifact_id=challenger_model_artifact_id,
        model_checksum_sha256=challenger_checksum_sha256,
        training_evidence_artifact_id=training_evidence_artifact_id,
        tournament_artifact_id=tournament_artifact_id,
        effective_at_utc=require_utc(promoted_at_utc, field_name="promoted_at_utc"),
        action="manual-promotion",
        previous_model_artifact_id=history.champion.model_artifact_id,
    )
    return ChampionHistory(history.scope_id, (*history.revisions, revision))


def rollback_champion(
    history: ChampionHistory,
    *,
    target_model_artifact_id: str,
    rolled_back_at_utc: datetime,
) -> ChampionHistory:
    target = next(
        (item for item in history.revisions if item.model_artifact_id == target_model_artifact_id),
        None,
    )
    if target is None or target.model_artifact_id == history.champion.model_artifact_id:
        raise ModelError("rollback target must be a retained prior champion")
    revision = ChampionRevision(
        revision=len(history.revisions) + 1,
        model_artifact_id=target.model_artifact_id,
        model_checksum_sha256=target.model_checksum_sha256,
        training_evidence_artifact_id=target.training_evidence_artifact_id,
        tournament_artifact_id=target.tournament_artifact_id,
        effective_at_utc=require_utc(rolled_back_at_utc, field_name="rolled_back_at_utc"),
        action="rollback",
        previous_model_artifact_id=history.champion.model_artifact_id,
    )
    return ChampionHistory(history.scope_id, (*history.revisions, revision))


def write_champion_history(
    *,
    root: Path,
    relative_directory: str,
    history: ChampionHistory,
) -> AnalyticalArtifact:
    return write_analytical_artifact(
        root=root,
        relative_directory=relative_directory,
        artifact_type=CHAMPION_HISTORY_TYPE,
        schema_version=CHAMPION_HISTORY_SCHEMA,
        payload={
            "scope_id": history.scope_id,
            "governance_mode": history.governance_mode,
            "active_model_artifact_id": history.champion.model_artifact_id,
            "revisions": [
                {
                    "revision": item.revision,
                    "model_artifact_id": item.model_artifact_id,
                    "model_checksum_sha256": item.model_checksum_sha256,
                    "training_evidence_artifact_id": item.training_evidence_artifact_id,
                    "tournament_artifact_id": item.tournament_artifact_id,
                    "effective_at_utc": format_utc_timestamp(item.effective_at_utc),
                    "action": item.action,
                    "previous_model_artifact_id": item.previous_model_artifact_id,
                }
                for item in history.revisions
            ],
        },
    )


def _record(
    snapshot: VerifiedResultSnapshot,
    competition: str,
    feature_id: str | None,
    state: TrainingEligibilityState,
    reasons: tuple[str, ...],
) -> TrainingEligibilityRecord:
    return TrainingEligibilityRecord(
        canonical_event_id=snapshot.result.canonical_event_id,
        competition_id=competition,
        result_snapshot_id=snapshot.snapshot_id,
        result_checksum_sha256=snapshot.checksum_sha256,
        pre_match_feature_artifact_id=feature_id,
        state=state,
        reason_codes=tuple(sorted(reasons)),
    )


def _text(value: str, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ModelError(f"{field} must be non-empty canonical text")
    return value


def _checksum(value: str, field: str) -> str:
    try:
        return validate_sha256_checksum(value)
    except Exception as exc:
        raise ModelError(f"{field} must be a SHA-256 checksum") from exc
