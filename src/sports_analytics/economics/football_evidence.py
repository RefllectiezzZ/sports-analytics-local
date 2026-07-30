"""Typed, derived economic eligibility evidence for football proposals."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final, cast

from sports_analytics.artifacts import (
    AnalyticalArtifact,
    load_analytical_artifact,
    write_analytical_artifact,
)
from sports_analytics.bookmakers.operator_quotes import (
    OPERATOR_QUOTE_ARTIFACT_SCHEMA,
    OPERATOR_QUOTE_ARTIFACT_TYPE,
    load_operator_quote_artifact,
)
from sports_analytics.core.exceptions import ArtifactError, EvaluationError
from sports_analytics.data.codec import format_utc_timestamp, parse_utc_timestamp
from sports_analytics.data.types import JsonValue, validate_sha256_checksum
from sports_analytics.models.identity import content_addressed_id
from sports_analytics.monitoring.artifacts import (
    MONITORING_ARTIFACT_TYPE,
    load_monitoring_report,
)
from sports_analytics.monitoring.contracts import MONITORING_REPORT_SCHEMA_VERSION
from sports_analytics.predictions.football_scores import (
    FOOTBALL_PROBABILITY_ARTIFACT_TYPE,
    FOOTBALL_PRODUCTION_PROBABILITY_ARTIFACT_SCHEMA,
    FootballProductionPredictionLineage,
    load_production_football_probability_artifact,
)
from sports_analytics.results.snapshots import (
    RESULT_SNAPSHOT_ARTIFACT_TYPE,
    RESULT_SNAPSHOT_SCHEMA_VERSION,
    load_result_snapshot,
)
from sports_analytics.settlement.service import (
    SETTLEMENT_REPORT_SCHEMA_VERSION,
    SETTLEMENT_REPORT_TYPE,
    load_settlement_report,
)
from sports_analytics.sports.contracts import require_utc

FOOTBALL_ECONOMIC_EVIDENCE_TYPE: Final[str] = "football-economic-evidence"
FOOTBALL_ECONOMIC_EVIDENCE_SCHEMA: Final[str] = "football-economic-evidence-v2"
FOOTBALL_ECONOMIC_POLICY_VERSION: Final[str] = "football-economic-eligibility-policy-v1"
FOOTBALL_ECONOMIC_DERIVATION_VERSION: Final[str] = "football-economic-derivation-v2"
FOOTBALL_ECONOMIC_REQUEST_SCHEMA: Final[str] = "football-economic-evaluation-request-v1"

PREDICTIONS_ROLE: Final[str] = "predictions"
QUOTES_ROLE: Final[str] = "quotes"
RESULTS_ROLE: Final[str] = "results"
SETTLEMENTS_ROLE: Final[str] = "settlements"
MONITORING_ROLE: Final[str] = "monitoring"
ECONOMIC_ROLES: Final[tuple[str, ...]] = (
    PREDICTIONS_ROLE,
    QUOTES_ROLE,
    RESULTS_ROLE,
    SETTLEMENTS_ROLE,
    MONITORING_ROLE,
)
_ROLE_CONTRACTS: Final[dict[str, tuple[str, str]]] = {
    PREDICTIONS_ROLE: (
        FOOTBALL_PROBABILITY_ARTIFACT_TYPE,
        FOOTBALL_PRODUCTION_PROBABILITY_ARTIFACT_SCHEMA,
    ),
    QUOTES_ROLE: (OPERATOR_QUOTE_ARTIFACT_TYPE, OPERATOR_QUOTE_ARTIFACT_SCHEMA),
    RESULTS_ROLE: (RESULT_SNAPSHOT_ARTIFACT_TYPE, RESULT_SNAPSHOT_SCHEMA_VERSION),
    SETTLEMENTS_ROLE: (SETTLEMENT_REPORT_TYPE, SETTLEMENT_REPORT_SCHEMA_VERSION),
    MONITORING_ROLE: (MONITORING_ARTIFACT_TYPE, MONITORING_REPORT_SCHEMA_VERSION),
}
_REQUEST_REFERENCE_FIELDS: Final[frozenset[str]] = frozenset(
    {"relative_directory", "artifact_id", "checksum_sha256"}
)
_PUBLISHED_REFERENCE_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "role",
        "relative_directory",
        "artifact_id",
        "checksum_sha256",
        "artifact_type",
        "schema_version",
    }
)
_EVIDENCE_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "sport_code",
        "competition_id",
        "market_key",
        "model_artifact_id",
        "model_checksum_sha256",
        "champion_role_revision",
        "champion_transition_id",
        "evaluation_mode",
        "evidence_window_start_utc",
        "evidence_window_end_utc",
        "evaluated_at_utc",
        "prediction_population_id",
        "quote_population_id",
        "result_population_id",
        "settlement_population_id",
        "monitoring_population_id",
        "policy_id",
        "policy_version",
        "policy_configuration_id",
        "prospective_prediction_count",
        "timestamped_quote_count",
        "completed_settlement_count",
        "settlement_coverage",
        "log_loss",
        "multiclass_brier_score",
        "ranked_probability_score",
        "calibration_error",
        "market_baseline_log_loss",
        "market_baseline_brier_score",
        "market_baseline_rps",
        "realised_turnover",
        "realised_profit_loss",
        "realised_roi",
        "maximum_drawdown",
        "unresolved_settlement_count",
        "stale_or_invalid_quote_count",
        "source_classification",
        "evidence_derivation_version",
        "upstream_artifacts",
    }
)
_OUTCOMES: Final[tuple[str, ...]] = ("home", "draw", "away")
_FINAL_SETTLEMENTS: Final[frozenset[str]] = frozenset({"win", "loss", "push", "void"})


@dataclass(frozen=True, slots=True, order=True)
class EconomicArtifactReference:
    role: str
    relative_directory: str
    artifact_id: str
    checksum_sha256: str
    artifact_type: str
    schema_version: str

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "role": self.role,
            "relative_directory": self.relative_directory,
            "artifact_id": self.artifact_id,
            "checksum_sha256": self.checksum_sha256,
            "artifact_type": self.artifact_type,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True, slots=True, order=True)
class EconomicArtifactRequestReference:
    relative_directory: str
    artifact_id: str
    checksum_sha256: str


@dataclass(frozen=True, slots=True)
class EconomicEvaluationRequest:
    output_relative_directory: str
    references: tuple[EconomicArtifactReference, ...]


@dataclass(frozen=True, slots=True)
class ChampionEconomicIdentity:
    model_artifact_id: str
    model_checksum_sha256: str
    champion_role_revision: int
    champion_transition_id: str | None
    market_key: str = "football.score.full-match"


@dataclass(frozen=True, slots=True)
class FootballEconomicEligibilityPolicy:
    """Conservative code policy; a caller never supplies an eligibility result."""

    minimum_prospective_prediction_count: int = 200
    minimum_timestamped_quote_count: int = 200
    minimum_completed_settlement_count: int = 150
    minimum_settlement_coverage: float = 0.90
    maximum_calibration_error: float = 0.05
    maximum_log_loss: float = 1.10
    maximum_brier_score: float = 0.70
    maximum_rps: float = 0.35
    maximum_market_baseline_degradation: float = 0.01
    require_positive_roi: bool = True
    minimum_realised_roi: float = 0.02
    maximum_drawdown: float = 0.20
    maximum_unresolved_settlement_count: int = 0
    maximum_stale_or_invalid_quote_count: int = 0
    maximum_evidence_age: timedelta = timedelta(days=7)
    policy_id: str = "football-production-economic-eligibility"
    policy_version: str = FOOTBALL_ECONOMIC_POLICY_VERSION

    def __post_init__(self) -> None:
        if any(
            type(getattr(self, name)) is not int or getattr(self, name) < 0
            for name in (
                "minimum_prospective_prediction_count",
                "minimum_timestamped_quote_count",
                "minimum_completed_settlement_count",
                "maximum_unresolved_settlement_count",
                "maximum_stale_or_invalid_quote_count",
            )
        ):
            raise EvaluationError("economic policy counts are invalid")
        for name in (
            "minimum_settlement_coverage",
            "maximum_calibration_error",
            "maximum_log_loss",
            "maximum_brier_score",
            "maximum_rps",
            "maximum_market_baseline_degradation",
            "minimum_realised_roi",
            "maximum_drawdown",
        ):
            value = getattr(self, name)
            if (
                type(value) not in (int, float)
                or not math.isfinite(float(value))
                or float(value) < 0
            ):
                raise EvaluationError("economic policy metrics are invalid")
        if (
            self.maximum_evidence_age <= timedelta(0)
            or not self.policy_id
            or self.policy_version != FOOTBALL_ECONOMIC_POLICY_VERSION
        ):
            raise EvaluationError("economic policy identity is invalid")

    @property
    def configuration_id(self) -> str:
        return content_addressed_id(
            identity_type=FOOTBALL_ECONOMIC_POLICY_VERSION, payload=self.to_json()
        )

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "minimum_prospective_prediction_count": self.minimum_prospective_prediction_count,
            "minimum_timestamped_quote_count": self.minimum_timestamped_quote_count,
            "minimum_completed_settlement_count": self.minimum_completed_settlement_count,
            "minimum_settlement_coverage": self.minimum_settlement_coverage,
            "maximum_calibration_error": self.maximum_calibration_error,
            "maximum_log_loss": self.maximum_log_loss,
            "maximum_brier_score": self.maximum_brier_score,
            "maximum_rps": self.maximum_rps,
            "maximum_market_baseline_degradation": self.maximum_market_baseline_degradation,
            "require_positive_roi": self.require_positive_roi,
            "minimum_realised_roi": self.minimum_realised_roi,
            "maximum_drawdown": self.maximum_drawdown,
            "maximum_unresolved_settlement_count": self.maximum_unresolved_settlement_count,
            "maximum_stale_or_invalid_quote_count": self.maximum_stale_or_invalid_quote_count,
            "maximum_evidence_age_seconds": int(self.maximum_evidence_age.total_seconds()),
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
        }


@dataclass(frozen=True, slots=True)
class FootballEconomicEvidence:
    artifact: AnalyticalArtifact
    payload: dict[str, JsonValue]


@dataclass(frozen=True, slots=True)
class EconomicEligibilityDecision:
    opportunity_analysis_eligible: bool
    bet_proposal_eligible: bool
    promotion_eligible: bool
    hold_reasons: tuple[str, ...]


def parse_economic_evaluation_request(raw: bytes) -> EconomicEvaluationRequest:
    """Parse a role-bound request that contains no caller-authored metrics."""
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise EvaluationError("economic evaluation request JSON is malformed") from exc
    if not isinstance(document, dict) or set(document) != {
        "schema_version",
        "output_relative_directory",
        "upstream_artifacts",
    }:
        raise EvaluationError("economic evaluation request fields are not exact")
    if document["schema_version"] != FOOTBALL_ECONOMIC_REQUEST_SCHEMA:
        raise EvaluationError("economic evaluation request schema is unsupported")
    output = _safe_relative(document["output_relative_directory"])
    grouped = document["upstream_artifacts"]
    if not isinstance(grouped, dict) or set(grouped) != set(ECONOMIC_ROLES):
        raise EvaluationError("economic evaluation roles are not exact")
    references: list[EconomicArtifactReference] = []
    for role in ECONOMIC_ROLES:
        raw_items = grouped[role]
        if not isinstance(raw_items, list) or not raw_items:
            raise EvaluationError(f"economic role {role} must contain references")
        if role in {QUOTES_ROLE, SETTLEMENTS_ROLE, MONITORING_ROLE} and len(raw_items) != 1:
            raise EvaluationError(f"economic role {role} must contain exactly one reference")
        artifact_type, schema = _ROLE_CONTRACTS[role]
        for raw_item in raw_items:
            request_reference = _request_reference(raw_item)
            references.append(
                EconomicArtifactReference(
                    role,
                    request_reference.relative_directory,
                    request_reference.artifact_id,
                    request_reference.checksum_sha256,
                    artifact_type,
                    schema,
                )
            )
    normalized = tuple(sorted(references))
    if len(set(normalized)) != len(normalized):
        raise EvaluationError("economic evaluation references are duplicated")
    return EconomicEvaluationRequest(output, normalized)


def derive_football_economic_evidence(
    *,
    root: Path,
    relative_directory: str,
    references: tuple[EconomicArtifactReference, ...],
    champion: ChampionEconomicIdentity,
    policy: FootballEconomicEligibilityPolicy,
) -> AnalyticalArtifact:
    """Load exact typed sources, recompute all metrics, and publish their lineage."""
    payload = _derive_payload(
        root=root,
        references=references,
        champion=champion,
        policy_identity=(
            policy.policy_id,
            policy.policy_version,
            policy.configuration_id,
        ),
    )
    return _write_football_economic_evidence(
        root=root,
        relative_directory=relative_directory,
        payload=payload,
    )


def inspect_economic_prediction_scope(
    *,
    root: Path,
    references: tuple[EconomicArtifactReference, ...],
) -> tuple[str, str]:
    """Strictly load prediction references and return their one competition/model scope."""
    _validate_reference_set(references)
    competitions: set[str] = set()
    models: set[str] = set()
    for reference in references:
        if reference.role != PREDICTIONS_ROLE:
            continue
        artifact, distribution, _ = load_production_football_probability_artifact(
            root=root,
            relative_directory=reference.relative_directory,
            expected_checksum=reference.checksum_sha256,
            expected_artifact_id=reference.artifact_id,
        )
        _require_artifact_identity(artifact, reference)
        competitions.add(distribution.competition_id)
        models.add(cast(str, cast(dict[str, Any], artifact.payload)["model_artifact_id"]))
    if len(competitions) != 1 or len(models) != 1:
        raise EvaluationError("economic prediction scope is inconsistent")
    return next(iter(competitions)), next(iter(models))


def _write_football_economic_evidence(
    *, root: Path, relative_directory: str, payload: dict[str, JsonValue]
) -> AnalyticalArtifact:
    checked = _validate_payload(payload)
    return write_analytical_artifact(
        root=root,
        relative_directory=relative_directory,
        artifact_type=FOOTBALL_ECONOMIC_EVIDENCE_TYPE,
        schema_version=FOOTBALL_ECONOMIC_EVIDENCE_SCHEMA,
        payload=checked,
    )


def load_football_economic_evidence(
    *,
    root: Path,
    relative_directory: str,
    expected_artifact_id: str | None = None,
    expected_checksum: str | None = None,
) -> FootballEconomicEvidence:
    """Load evidence and independently re-derive every metric from typed sources."""
    artifact = load_analytical_artifact(
        root=root,
        relative_directory=relative_directory,
        expected_artifact_type=FOOTBALL_ECONOMIC_EVIDENCE_TYPE,
        expected_schema_version=FOOTBALL_ECONOMIC_EVIDENCE_SCHEMA,
        expected_artifact_id=expected_artifact_id,
        expected_checksum=expected_checksum,
    )
    try:
        payload = _validate_payload(cast(dict[str, JsonValue], artifact.payload))
        references = tuple(
            _published_reference(item) for item in cast(list[object], payload["upstream_artifacts"])
        )
        expected = _derive_payload(
            root=root,
            references=references,
            champion=ChampionEconomicIdentity(
                cast(str, payload["model_artifact_id"]),
                cast(str, payload["model_checksum_sha256"]),
                cast(int, payload["champion_role_revision"]),
                cast(str | None, payload["champion_transition_id"]),
                cast(str, payload["market_key"]),
            ),
            policy_identity=(
                cast(str, payload["policy_id"]),
                cast(str, payload["policy_version"]),
                cast(str, payload["policy_configuration_id"]),
            ),
        )
    except (EvaluationError, TypeError, ValueError, OSError) as exc:
        raise ArtifactError(str(exc)) from exc
    if expected != payload:
        raise ArtifactError("economic evidence metrics do not match verified upstream artifacts")
    return FootballEconomicEvidence(artifact=artifact, payload=payload)


def evaluate_football_economic_evidence(
    *,
    evidence: FootballEconomicEvidence,
    policy: FootballEconomicEligibilityPolicy,
    model_artifact_id: str,
    model_checksum_sha256: str,
    competition_id: str,
    market_key: str,
    champion_role_revision: int,
    champion_transition_id: str | None,
    evaluated_at_utc: datetime,
) -> EconomicEligibilityDecision:
    p = cast(dict[str, Any], evidence.payload)
    common: list[str] = []
    proposal: list[str] = []
    promotion: list[str] = []
    if p["evaluation_mode"] != "prospective-operator":
        common.extend(
            ("invalid-economic-evidence-provenance", "no-prospective-timestamped-evidence")
        )
    if (
        p["model_artifact_id"] != model_artifact_id
        or p["model_checksum_sha256"] != model_checksum_sha256
        or p["champion_role_revision"] != champion_role_revision
        or p["champion_transition_id"] != champion_transition_id
    ):
        common.append("economic-evidence-model-lineage-mismatch")
    if p["competition_id"] != competition_id:
        common.append("economic-evidence-competition-mismatch")
    if p["market_key"] != market_key or p["sport_code"] != "football":
        common.append("economic-evidence-market-mismatch")
    cutoff = require_utc(evaluated_at_utc, field_name="evaluated_at_utc")
    end = _utc(cast(str, p["evidence_window_end_utc"]), "evidence_window_end_utc")
    if cutoff - end > policy.maximum_evidence_age:
        common.append("economic-evidence-stale")
    if (
        p["policy_id"] != policy.policy_id
        or p["policy_version"] != policy.policy_version
        or p["policy_configuration_id"] != policy.configuration_id
    ):
        common.append("invalid-economic-evidence-provenance")
    if p["prospective_prediction_count"] < policy.minimum_prospective_prediction_count:
        promotion.append("insufficient-prospective-sample")
    if p["timestamped_quote_count"] < policy.minimum_timestamped_quote_count:
        proposal.append("insufficient-timestamped-quote-sample")
    if p["completed_settlement_count"] < policy.minimum_completed_settlement_count:
        proposal.extend(
            ("insufficient-completed-settlement-sample", "no-prospective-settlement-cycle")
        )
    if p["settlement_coverage"] < policy.minimum_settlement_coverage:
        proposal.append("insufficient-settlement-coverage")
    if p["unresolved_settlement_count"] > policy.maximum_unresolved_settlement_count:
        proposal.append("unresolved-settlements-present")
    if p["stale_or_invalid_quote_count"] > policy.maximum_stale_or_invalid_quote_count:
        proposal.append("stale-or-invalid-quotes-present")
    if p["calibration_error"] > policy.maximum_calibration_error:
        promotion.append("calibration-threshold-failed")
    if (
        p["log_loss"] > policy.maximum_log_loss
        or p["multiclass_brier_score"] > policy.maximum_brier_score
        or p["ranked_probability_score"] > policy.maximum_rps
    ):
        promotion.append("proper-score-threshold-failed")
    baseline_pairs = (
        ("log_loss", "market_baseline_log_loss"),
        ("multiclass_brier_score", "market_baseline_brier_score"),
        ("ranked_probability_score", "market_baseline_rps"),
    )
    if any(
        p[baseline] is None or p[metric] > p[baseline] + policy.maximum_market_baseline_degradation
        for metric, baseline in baseline_pairs
    ):
        promotion.append("market-baseline-threshold-failed")
    if policy.require_positive_roi and p["realised_roi"] < policy.minimum_realised_roi:
        proposal.append("economic-return-threshold-failed")
    if p["maximum_drawdown"] > policy.maximum_drawdown:
        proposal.append("drawdown-threshold-failed")
    proposal_holds = tuple(sorted(set(common + proposal + promotion)))
    promotion_holds = tuple(sorted(set(common + promotion)))
    all_holds = tuple(sorted(set(proposal_holds + promotion_holds)))
    return EconomicEligibilityDecision(
        opportunity_analysis_eligible=not common,
        bet_proposal_eligible=not proposal_holds,
        promotion_eligible=not promotion_holds,
        hold_reasons=all_holds,
    )


def _derive_payload(
    *,
    root: Path,
    references: tuple[EconomicArtifactReference, ...],
    champion: ChampionEconomicIdentity,
    policy_identity: tuple[str, str, str],
) -> dict[str, JsonValue]:
    _validate_reference_set(references)
    predictions: dict[
        str,
        tuple[tuple[float, float, float], FootballProductionPredictionLineage],
    ] = {}
    competition_ids: set[str] = set()
    model_ids: set[str] = set()
    model_checksums: set[str] = set()
    champion_revisions: set[int] = set()
    champion_transitions: set[str | None] = set()
    decision_times: set[datetime] = set()
    upcoming_event_lineage: set[tuple[str, str]] = set()
    participant_registry_lineage: set[tuple[str, str]] = set()
    quote_probabilities: dict[str, tuple[float, float, float]] = {}
    quote_times: list[datetime] = []
    quote_times_by_event: dict[str, list[datetime]] = {}
    quote_observed_as_of: datetime | None = None
    result_outcomes: dict[str, int] = {}
    result_times: dict[str, datetime] = {}
    result_event_starts: dict[str, datetime] = {}
    result_snapshot_evidence: dict[str, tuple[str, str]] = {}
    settlement_rows: list[dict[str, JsonValue]] = []
    settlement_as_of: datetime | None = None
    monitoring_start: datetime | None = None
    monitoring_end: datetime | None = None
    monitoring_as_of: datetime | None = None
    monitoring_evidence: set[tuple[str, str]] = set()

    for reference in references:
        if reference.role == PREDICTIONS_ROLE:
            artifact, distribution, lineage = load_production_football_probability_artifact(
                root=root,
                relative_directory=reference.relative_directory,
                expected_checksum=reference.checksum_sha256,
                expected_artifact_id=reference.artifact_id,
            )
            _require_artifact_identity(artifact, reference)
            payload = cast(dict[str, Any], artifact.payload)
            event_id = cast(str, payload["canonical_event_id"])
            if event_id in predictions:
                raise EvaluationError("economic prediction event is duplicated")
            predictions[event_id] = (
                _market_probabilities(payload["markets"]),
                lineage,
            )
            competition_ids.add(distribution.competition_id)
            model_ids.add(lineage.model_artifact_id)
            model_checksums.add(lineage.model_checksum_sha256)
            champion_revisions.add(lineage.active_champion_role_revision)
            champion_transitions.add(lineage.active_champion_transition_id)
            decision_times.add(lineage.decision_as_of_utc)
            upcoming_event_lineage.add(
                (
                    lineage.upcoming_event_artifact_id,
                    lineage.upcoming_event_checksum_sha256,
                )
            )
            participant_registry_lineage.add(
                (
                    lineage.participant_registry_artifact_id,
                    lineage.participant_registry_checksum_sha256,
                )
            )
        elif reference.role == QUOTES_ROLE:
            artifact = load_operator_quote_artifact(
                root=root,
                relative_directory=reference.relative_directory,
                expected_checksum=reference.checksum_sha256,
            )
            _require_artifact_identity(artifact, reference)
            quote_payload = cast(dict[str, Any], artifact.payload)
            quote_observed_as_of = _utc(
                cast(str, quote_payload["observed_as_of_utc"]),
                "observed_as_of_utc",
            )
            rows = cast(list[dict[str, Any]], quote_payload["quotes"])
            grouped: dict[str, dict[str, float]] = {}
            for row in rows:
                if (
                    row["sport_code"] != "football"
                    or row["market_family"] != "match-result"
                    or row["market_period"] != "full-match"
                ):
                    continue
                event_id = cast(str, row["canonical_event_id"])
                outcome = cast(str, row["outcome_key"])
                if outcome not in _OUTCOMES or outcome in grouped.setdefault(event_id, {}):
                    raise EvaluationError("economic quote population is ambiguous")
                grouped[event_id][outcome] = 1.0 / float(cast(str, row["offered_decimal_odds"]))
                observed = _utc(cast(str, row["observed_at_utc"]), "observed_at_utc")
                quote_times.append(observed)
                quote_times_by_event.setdefault(event_id, []).append(observed)
            for event_id, values in grouped.items():
                if set(values) != set(_OUTCOMES):
                    raise EvaluationError("economic quote market is incomplete")
                total = sum(values.values())
                quote_probabilities[event_id] = cast(
                    tuple[float, float, float],
                    tuple(values[outcome] / total for outcome in _OUTCOMES),
                )
        elif reference.role == RESULTS_ROLE:
            generic = load_analytical_artifact(
                root=root,
                relative_directory=reference.relative_directory,
                expected_artifact_type=RESULT_SNAPSHOT_ARTIFACT_TYPE,
                expected_schema_version=RESULT_SNAPSHOT_SCHEMA_VERSION,
                expected_artifact_id=reference.artifact_id,
                expected_checksum=reference.checksum_sha256,
            )
            _require_artifact_identity(generic, reference)
            snapshot = load_result_snapshot(
                root=root,
                relative_directory=reference.relative_directory,
                expected_checksum=reference.checksum_sha256,
            )
            result = snapshot.result
            result_snapshot_evidence[snapshot.snapshot_id] = (
                snapshot.checksum_sha256,
                result.canonical_result_id,
            )
            if result.sport_code != "football" or result.result_timestamp_utc is None:
                raise EvaluationError("economic result is not a completed football result")
            roles = {item.role: item.score for item in result.participant_results}
            if set(roles) != {"home", "away"}:
                raise EvaluationError("economic result participant roles are incomplete")
            result_index = (
                0 if roles["home"] > roles["away"] else 2 if roles["away"] > roles["home"] else 1
            )
            if result.canonical_event_id in result_outcomes:
                raise EvaluationError("economic result event is duplicated")
            result_outcomes[result.canonical_event_id] = result_index
            result_times[result.canonical_event_id] = result.result_timestamp_utc
            result_event_starts[result.canonical_event_id] = result.scheduled_start_utc
        elif reference.role == SETTLEMENTS_ROLE:
            report = load_settlement_report(
                root=root,
                relative_directory=reference.relative_directory,
                expected_checksum=reference.checksum_sha256,
            )
            if report.artifact is None:
                raise EvaluationError("economic settlement artifact is unavailable")
            _require_artifact_identity(report.artifact, reference)
            settlement_rows = cast(
                list[dict[str, JsonValue]],
                cast(dict[str, JsonValue], report.artifact.payload)["settlements"],
            )
            settlement_as_of = report.as_of_utc
        elif reference.role == MONITORING_ROLE:
            verified = load_monitoring_report(
                root=root,
                relative_directory=reference.relative_directory,
                expected_checksum=reference.checksum_sha256,
            )
            _require_artifact_identity(verified.artifact, reference)
            monitoring_start = verified.report.window_start_utc
            monitoring_end = verified.report.window_end_utc
            monitoring_as_of = verified.report.as_of_utc
            monitoring_evidence = {
                (item.evidence_id, item.checksum_sha256) for item in verified.report.evidence
            }

    if (
        len(competition_ids) != 1
        or model_ids != {champion.model_artifact_id}
        or model_checksums != {champion.model_checksum_sha256}
        or champion_revisions != {champion.champion_role_revision}
        or champion_transitions != {champion.champion_transition_id}
    ):
        raise EvaluationError("economic prediction model or competition lineage is inconsistent")
    if (
        len(decision_times) != 1
        or len(upcoming_event_lineage) != 1
        or len(participant_registry_lineage) != 1
    ):
        raise EvaluationError("economic prediction prospective lineage is inconsistent")
    prediction_events = set(predictions)
    if (
        not prediction_events
        or set(quote_probabilities) != prediction_events
        or set(result_outcomes) != prediction_events
    ):
        raise EvaluationError("economic event populations are inconsistent")
    settlement_events: set[str] = set()
    completed = unresolved = 0
    turnover = profit = 0.0
    cumulative = peak = maximum_drawdown_units = 0.0
    for row in settlement_rows:
        events = cast(list[str], row["canonical_event_ids"])
        if len(events) != 1:
            raise EvaluationError("economic settlement is not an exact single-event population")
        event_id = events[0]
        if event_id in settlement_events:
            raise EvaluationError("economic settlement event is duplicated")
        settlement_events.add(event_id)
        evidence_rows = cast(list[dict[str, JsonValue]], row["evidence"])
        if len(evidence_rows) != 1:
            raise EvaluationError("economic settlement result evidence is incomplete")
        for evidence in evidence_rows:
            snapshot_evidence = result_snapshot_evidence.get(
                cast(str, evidence["result_snapshot_id"])
            )
            if evidence["canonical_event_id"] != event_id or snapshot_evidence != (
                evidence["result_checksum_sha256"],
                evidence["canonical_result_id"],
            ):
                raise EvaluationError("economic settlement result lineage is inconsistent")
        status = cast(str, row["status"])
        if status in _FINAL_SETTLEMENTS:
            completed += 1
            stake = float(cast(str, row["stake_units"]))
            row_profit = float(cast(str, row["profit_units"]))
            turnover += stake
            profit += row_profit
            cumulative += row_profit
            peak = max(peak, cumulative)
            maximum_drawdown_units = max(maximum_drawdown_units, peak - cumulative)
        else:
            unresolved += 1
    if settlement_events != prediction_events:
        raise EvaluationError("economic settlement population is inconsistent")
    settlement_reference = next(item for item in references if item.role == SETTLEMENTS_ROLE)
    if (
        settlement_reference.artifact_id,
        settlement_reference.checksum_sha256,
    ) not in monitoring_evidence:
        raise EvaluationError("economic monitoring lineage omits settlement evidence")
    if not quote_times or settlement_as_of is None or monitoring_start is None:
        raise EvaluationError("economic evidence is incomplete")
    assert monitoring_end is not None and monitoring_as_of is not None
    decision_as_of = next(iter(decision_times))
    if quote_observed_as_of != decision_as_of:
        raise EvaluationError("economic quote batch decision cutoff is inconsistent")
    for event_id, (_, lineage) in predictions.items():
        if result_event_starts[event_id] != lineage.event_start_utc:
            raise EvaluationError("economic result event start lineage is inconsistent")
        if any(
            quote_time >= lineage.event_start_utc for quote_time in quote_times_by_event[event_id]
        ):
            raise EvaluationError("economic quote is not prospective to event start")
        if any(quote_time > decision_as_of for quote_time in quote_times_by_event[event_id]):
            raise EvaluationError("economic quote follows the prediction decision cutoff")
    if any(
        quote_time >= result_times[event_id]
        for event_id in prediction_events
        for quote_time in quote_times_by_event[event_id]
    ):
        raise EvaluationError("economic quote timing is incompatible with results")
    if settlement_as_of < max(result_times.values()) or monitoring_as_of < settlement_as_of:
        raise EvaluationError("economic settlement or monitoring timing is incompatible")
    if monitoring_start > min(quote_times) or monitoring_end < max(result_times.values()):
        raise EvaluationError("economic monitoring window excludes the evidence population")

    actual_vectors = [predictions[event][0] for event in sorted(prediction_events)]
    baseline_vectors = [quote_probabilities[event] for event in sorted(prediction_events)]
    outcomes = [result_outcomes[event] for event in sorted(prediction_events)]
    log_loss, brier, rps = _proper_scores(actual_vectors, outcomes)
    baseline_log_loss, baseline_brier, baseline_rps = _proper_scores(baseline_vectors, outcomes)
    calibration = sum(
        abs(max(vector) - (1.0 if vector.index(max(vector)) == outcome else 0.0))
        for vector, outcome in zip(actual_vectors, outcomes, strict=True)
    ) / len(outcomes)
    coverage = completed / len(prediction_events)
    maximum_drawdown = 0.0 if turnover == 0 else maximum_drawdown_units / turnover
    evaluated = monitoring_as_of
    competition_id = next(iter(competition_ids))
    policy_id, policy_version, policy_configuration_id = policy_identity
    return {
        "sport_code": "football",
        "competition_id": competition_id,
        "market_key": champion.market_key,
        "model_artifact_id": next(iter(model_ids)),
        "model_checksum_sha256": next(iter(model_checksums)),
        "champion_role_revision": next(iter(champion_revisions)),
        "champion_transition_id": next(iter(champion_transitions)),
        "evaluation_mode": "prospective-operator",
        "evidence_window_start_utc": format_utc_timestamp(monitoring_start),
        "evidence_window_end_utc": format_utc_timestamp(monitoring_end),
        "evaluated_at_utc": format_utc_timestamp(evaluated),
        "prediction_population_id": _population_id(PREDICTIONS_ROLE, references),
        "quote_population_id": _population_id(QUOTES_ROLE, references),
        "result_population_id": _population_id(RESULTS_ROLE, references),
        "settlement_population_id": _population_id(SETTLEMENTS_ROLE, references),
        "monitoring_population_id": _population_id(MONITORING_ROLE, references),
        "policy_id": policy_id,
        "policy_version": policy_version,
        "policy_configuration_id": policy_configuration_id,
        "prospective_prediction_count": len(predictions),
        "timestamped_quote_count": len(quote_times),
        "completed_settlement_count": completed,
        "settlement_coverage": coverage,
        "log_loss": log_loss,
        "multiclass_brier_score": brier,
        "ranked_probability_score": rps,
        "calibration_error": calibration,
        "market_baseline_log_loss": baseline_log_loss,
        "market_baseline_brier_score": baseline_brier,
        "market_baseline_rps": baseline_rps,
        "realised_turnover": turnover,
        "realised_profit_loss": profit,
        "realised_roi": 0.0 if turnover == 0 else profit / turnover,
        "maximum_drawdown": maximum_drawdown,
        "unresolved_settlement_count": unresolved,
        "stale_or_invalid_quote_count": 0,
        "source_classification": "verified-prospective-operator-artifacts",
        "evidence_derivation_version": FOOTBALL_ECONOMIC_DERIVATION_VERSION,
        "upstream_artifacts": [item.to_json() for item in references],
    }


def _market_probabilities(raw: object) -> tuple[float, float, float]:
    if not isinstance(raw, list):
        raise EvaluationError("football probability markets are invalid")
    values: dict[str, float] = {}
    for item in raw:
        if (
            isinstance(item, dict)
            and item.get("market_family") == "match-result"
            and item.get("market_key") == "football.match-result.1x2.full-match"
        ):
            outcome = item.get("outcome_key")
            if outcome in values or outcome not in _OUTCOMES:
                raise EvaluationError("football 1X2 probability outcomes are ambiguous")
            values[cast(str, outcome)] = float(cast(float, item.get("probability")))
    if set(values) != set(_OUTCOMES):
        raise EvaluationError("football 1X2 probability outcomes are incomplete")
    return cast(tuple[float, float, float], tuple(values[item] for item in _OUTCOMES))


def _proper_scores(
    vectors: list[tuple[float, float, float]], outcomes: list[int]
) -> tuple[float, float, float]:
    count = len(outcomes)
    log_loss = (
        sum(
            -math.log(max(vector[outcome], 1e-15))
            for vector, outcome in zip(vectors, outcomes, strict=True)
        )
        / count
    )
    brier = (
        sum(
            sum(
                (probability - (1.0 if index == outcome else 0.0)) ** 2
                for index, probability in enumerate(vector)
            )
            for vector, outcome in zip(vectors, outcomes, strict=True)
        )
        / count
    )
    rps = (
        sum(
            sum(
                (sum(vector[: index + 1]) - (1.0 if outcome <= index else 0.0)) ** 2
                for index in range(2)
            )
            / 2.0
            for vector, outcome in zip(vectors, outcomes, strict=True)
        )
        / count
    )
    return log_loss, brier, rps


def _population_id(role: str, references: tuple[EconomicArtifactReference, ...]) -> str:
    return content_addressed_id(
        identity_type=f"football-economic-{role}-population-v1",
        payload={"artifacts": [item.to_json() for item in references if item.role == role]},
    )


def _require_artifact_identity(
    artifact: AnalyticalArtifact, reference: EconomicArtifactReference
) -> None:
    expected_type, expected_schema = _ROLE_CONTRACTS[reference.role]
    if (
        artifact.artifact_id != reference.artifact_id
        or artifact.checksum_sha256 != reference.checksum_sha256
        or artifact.artifact_type != expected_type
        or artifact.schema_version != expected_schema
    ):
        raise EvaluationError("economic upstream artifact identity is inconsistent")


def _validate_reference_set(
    references: tuple[EconomicArtifactReference, ...],
) -> None:
    if not references or references != tuple(sorted(references)):
        raise EvaluationError("economic upstream references are not canonical")
    roles = {item.role for item in references}
    if roles != set(ECONOMIC_ROLES):
        raise EvaluationError("economic upstream roles are incomplete")
    for role in (QUOTES_ROLE, SETTLEMENTS_ROLE, MONITORING_ROLE):
        if sum(item.role == role for item in references) != 1:
            raise EvaluationError(f"economic role {role} must be unique")
    for reference in references:
        expected = _ROLE_CONTRACTS.get(reference.role)
        if expected != (reference.artifact_type, reference.schema_version):
            raise EvaluationError("economic upstream role contract is unsupported")


def _validate_payload(value: object) -> dict[str, JsonValue]:
    if not isinstance(value, dict) or set(value) != _EVIDENCE_FIELDS:
        raise EvaluationError("economic evidence fields are not exact")
    p = cast(dict[str, Any], value)
    if (
        p["sport_code"] != "football"
        or p["evaluation_mode"] != "prospective-operator"
        or p["source_classification"] != "verified-prospective-operator-artifacts"
        or p["evidence_derivation_version"] != FOOTBALL_ECONOMIC_DERIVATION_VERSION
    ):
        raise EvaluationError("economic evidence provenance is invalid")
    try:
        validate_sha256_checksum(cast(str, p["model_checksum_sha256"]))
    except Exception as exc:
        raise EvaluationError("economic evidence checksum is invalid") from exc
    start, end, evaluated = (
        _utc(cast(str, p[name]), name)
        for name in ("evidence_window_start_utc", "evidence_window_end_utc", "evaluated_at_utc")
    )
    if not start < end <= evaluated:
        raise EvaluationError("economic evidence window is invalid")
    counts = (
        "prospective_prediction_count",
        "timestamped_quote_count",
        "completed_settlement_count",
        "unresolved_settlement_count",
        "stale_or_invalid_quote_count",
    )
    if any(type(p[name]) is not int or p[name] < 0 for name in counts):
        raise EvaluationError("economic evidence counts are invalid")
    for name in (
        "settlement_coverage",
        "log_loss",
        "multiclass_brier_score",
        "ranked_probability_score",
        "calibration_error",
        "market_baseline_log_loss",
        "market_baseline_brier_score",
        "market_baseline_rps",
        "realised_turnover",
        "realised_profit_loss",
        "realised_roi",
        "maximum_drawdown",
    ):
        if type(p[name]) not in (int, float) or not math.isfinite(float(p[name])):
            raise EvaluationError("economic evidence metric is invalid")
    if not 0 <= p["settlement_coverage"] <= 1:
        raise EvaluationError("economic evidence coverage is invalid")
    refs = p["upstream_artifacts"]
    if not isinstance(refs, list):
        raise EvaluationError("economic evidence upstream references are invalid")
    normalized = tuple(_published_reference(item) for item in refs)
    _validate_reference_set(normalized)
    return cast(dict[str, JsonValue], p)


def _request_reference(value: object) -> EconomicArtifactRequestReference:
    if not isinstance(value, dict) or set(value) != _REQUEST_REFERENCE_FIELDS:
        raise EvaluationError("economic request reference fields are not exact")
    relative = _safe_relative(value["relative_directory"])
    artifact_id = _safe_text(value["artifact_id"], "artifact_id")
    checksum = _safe_text(value["checksum_sha256"], "checksum_sha256")
    try:
        validate_sha256_checksum(checksum)
    except Exception as exc:
        raise EvaluationError("economic request reference checksum is invalid") from exc
    return EconomicArtifactRequestReference(relative, artifact_id, checksum)


def _published_reference(value: object) -> EconomicArtifactReference:
    if not isinstance(value, dict) or set(value) != _PUBLISHED_REFERENCE_FIELDS:
        raise EvaluationError("economic evidence reference fields are not exact")
    role = _safe_text(value["role"], "role")
    relative = _safe_relative(value["relative_directory"])
    artifact_id = _safe_text(value["artifact_id"], "artifact_id")
    checksum = _safe_text(value["checksum_sha256"], "checksum_sha256")
    artifact_type = _safe_text(value["artifact_type"], "artifact_type")
    schema = _safe_text(value["schema_version"], "schema_version")
    try:
        validate_sha256_checksum(checksum)
    except Exception as exc:
        raise EvaluationError("economic evidence reference checksum is invalid") from exc
    reference = EconomicArtifactReference(
        role, relative, artifact_id, checksum, artifact_type, schema
    )
    expected = _ROLE_CONTRACTS.get(role)
    if expected != (artifact_type, schema):
        raise EvaluationError("economic evidence reference role contract is unsupported")
    return reference


def _safe_relative(value: object) -> str:
    text = _safe_text(value, "relative_directory")
    if text.startswith("/") or "\\" in text or ".." in text.split("/"):
        raise EvaluationError("economic evidence reference is unsafe")
    return text


def _safe_text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise EvaluationError(f"economic {field} is invalid")
    return value


def _utc(value: str, field: str) -> datetime:
    if not value.endswith("Z"):
        raise EvaluationError(f"{field} must be canonical UTC")
    try:
        parsed = parse_utc_timestamp(value)
    except Exception as exc:
        raise EvaluationError(f"{field} is invalid") from exc
    if format_utc_timestamp(parsed) != value:
        raise EvaluationError(f"{field} must be canonical UTC")
    return parsed
