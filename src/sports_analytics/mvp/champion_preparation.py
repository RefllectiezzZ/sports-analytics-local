"""Explicit, governed score-champion preparation for the local MVP."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import cast

import numpy as np

from sports_analytics.artifacts import AnalyticalArtifact
from sports_analytics.core.exceptions import ArtifactError, GovernanceError, SportsAnalyticsError
from sports_analytics.core.paths import RuntimePaths
from sports_analytics.data.database import connect_database, transaction
from sports_analytics.data.types import JsonValue
from sports_analytics.features.football.datasets import (
    load_finished_events_from_snapshots,
)
from sports_analytics.governance.contracts import (
    GovernanceDecisionKind,
    ModelEvaluationEvidence,
    ModelRole,
    PromotionPolicy,
    evaluate_challenger,
)
from sports_analytics.governance.repository import ModelGovernanceRepository
from sports_analytics.markets.football_score_markets import (
    ScorePredicateKind,
    predicate_probability,
    primitive,
)
from sports_analytics.models.football_evaluation import (
    EvaluationProvenance,
    multiclass_calibration_diagnostics,
)
from sports_analytics.models.football_scores import (
    DIXON_COLES,
    INDEPENDENT_POISSON,
    FootballScoreModel,
    ScoreModelConfiguration,
    ScoreTrainingMatch,
    fit_dixon_coles,
    fit_independent_poisson,
    load_score_model_artifact,
    predict_joint_score,
    temperature_scale_distribution,
    write_score_model_artifact,
)
from sports_analytics.models.football_tournament import (
    FootballScoreTournament,
    ScoreTournamentCandidate,
    TournamentCandidateSummary,
    TournamentFoldMetric,
    TournamentSplitConfiguration,
    build_tournament_folds,
    default_score_candidates,
    load_tournament_artifact,
    run_score_tournament,
    tournament_payload,
    write_tournament_artifact,
)
from sports_analytics.models.identity import content_addressed_id
from sports_analytics.services.champion_resolution import (
    FOOTBALL_PROBABILITY_GENERATOR_SCOPE,
    FOOTBALL_PRODUCT_MODEL_PURPOSE,
    FOOTBALL_PRODUCTION_EVALUATION_MODE,
    ResolvedScoreChampion,
    load_score_calibration_artifact,
    resolve_active_score_champion,
    write_score_calibration_artifact,
)
from sports_analytics.sports.football.markets import MARKET_KEY_MATCH_RESULT_1X2
from sports_analytics.sports.football.participant_registry import (
    ParticipantSourceReference,
)

_SPLIT = TournamentSplitConfiguration(
    minimum_training_rows=500,
    calibration_rows=100,
    test_rows=100,
    maximum_folds=3,
)
_SCORE_CONFIGURATION = ScoreModelConfiguration(maximum_grid_goals=24)
_CANDIDATES = default_score_candidates(configuration=_SCORE_CONFIGURATION)


@dataclass(frozen=True, slots=True)
class PreparedChampion:
    """One active champion created or reused through explicit preparation."""

    competition_id: str
    model_artifact_id: str
    tournament_artifact_id: str | None
    decision_id: str | None
    transition_id: str | None
    reused: bool


@dataclass(frozen=True, slots=True)
class ChampionPreparationReport:
    """Per-click governed outcomes and human-readable blockers."""

    champions: tuple[PreparedChampion, ...]
    blockers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _CompetitionHistory:
    competition_id: str
    references: tuple[ParticipantSourceReference, ...]
    matches: tuple[ScoreTrainingMatch, ...]


@dataclass(frozen=True, slots=True)
class _PublishedCandidate:
    candidate: ScoreTournamentCandidate
    artifact: AnalyticalArtifact
    model: FootballScoreModel
    relative_directory: str
    calibration_artifact: AnalyticalArtifact
    calibration_relative_directory: str
    calibration_temperature: float


def prepare_score_champions(
    *,
    paths: RuntimePaths,
    references: tuple[ParticipantSourceReference, ...],
    evaluated_at_utc: datetime,
) -> ChampionPreparationReport:
    """Prepare every eligible missing competition champion on one UI action."""
    try:
        histories = _load_competition_histories(paths, references)
    except (SportsAnalyticsError, OSError, ValueError) as exc:
        return ChampionPreparationReport(
            (),
            (f"verified historical preparation failed: {exc}",),
        )
    prepared: list[PreparedChampion] = []
    blockers: list[str] = []
    for history in histories:
        try:
            prepared.append(
                _prepare_competition(
                    paths=paths,
                    history=history,
                    evaluated_at_utc=evaluated_at_utc,
                )
            )
        except (SportsAnalyticsError, OSError, ValueError) as exc:
            blockers.append(f"{history.competition_id}: {exc}")
    return ChampionPreparationReport(tuple(prepared), tuple(blockers))


def _load_competition_histories(
    paths: RuntimePaths,
    references: tuple[ParticipantSourceReference, ...],
) -> tuple[_CompetitionHistory, ...]:
    grouped_references: dict[str, list[ParticipantSourceReference]] = {}
    grouped_matches: dict[str, dict[str, ScoreTrainingMatch]] = {}
    for reference in references:
        events, identities, _quotes = load_finished_events_from_snapshots(
            snapshots_directory=paths.snapshots_directory,
            relative_manifest_paths=(f"{reference.relative_directory}/manifest.json",),
        )
        if (
            len(identities) != 1
            or identities[0].snapshot_id != reference.artifact_id
            or identities[0].manifest_checksum_sha256 != reference.checksum_sha256
        ):
            raise GovernanceError("historical snapshot identity differs from verified registry")
        competition = identities[0].scope_id
        grouped_references.setdefault(competition, []).append(reference)
        destination = grouped_matches.setdefault(competition, {})
        for event in events:
            if event.competition_id != competition:
                raise GovernanceError("historical snapshot competition scope differs")
            match = ScoreTrainingMatch(
                canonical_event_id=event.canonical_event_id,
                competition_id=event.competition_id,
                event_date=event.event_date,
                home_team_id=event.home_canonical_participant_id,
                away_team_id=event.away_canonical_participant_id,
                home_goals=event.home_score,
                away_goals=event.away_score,
            )
            previous = destination.get(match.canonical_event_id)
            if previous is not None and previous != match:
                raise GovernanceError("historical event identity has contradictory results")
            destination[match.canonical_event_id] = match
    return tuple(
        _CompetitionHistory(
            competition,
            tuple(sorted(grouped_references[competition])),
            tuple(
                sorted(
                    grouped_matches[competition].values(),
                    key=lambda item: (item.event_date, item.canonical_event_id),
                )
            ),
        )
        for competition in sorted(grouped_matches)
    )


def _prepare_competition(
    *,
    paths: RuntimePaths,
    history: _CompetitionHistory,
    evaluated_at_utc: datetime,
) -> PreparedChampion:
    current = _active_champion(paths, history.competition_id)
    if current is not None:
        return PreparedChampion(
            history.competition_id,
            current.model_artifact_id,
            None,
            None,
            current.active_transition_id,
            True,
        )
    identity = content_addressed_id(
        identity_type="mvp-score-champion-preparation-v1",
        payload={
            "competition_id": history.competition_id,
            "snapshot_refs": [
                {
                    "artifact_id": item.artifact_id,
                    "checksum_sha256": item.checksum_sha256,
                }
                for item in history.references
            ],
            "candidate_ids": [item.candidate_id for item in _CANDIDATES],
            "split": {
                "minimum_training_rows": _SPLIT.minimum_training_rows,
                "calibration_rows": _SPLIT.calibration_rows,
                "test_rows": _SPLIT.test_rows,
                "maximum_folds": _SPLIT.maximum_folds,
            },
        },
    )
    base = f"mvp/champion-preparation/{history.competition_id}/{identity}"
    tournament_relative = f"{base}/tournament"
    existing = _load_existing_tournament(
        root=paths.exports_directory,
        relative_directory=tournament_relative,
        history=history,
    )
    if existing is None:
        tournament = run_score_tournament(
            history.matches,
            candidates=_CANDIDATES,
            split_configuration=_SPLIT,
            evaluation_provenance=EvaluationProvenance.VERIFIED_HISTORICAL,
        )
        tournament_artifact = _publish_tournament(
            root=paths.exports_directory,
            relative_directory=tournament_relative,
            tournament=tournament,
        )
    else:
        tournament_artifact, tournament = existing
    if tournament.evaluation_provenance is not EvaluationProvenance.VERIFIED_HISTORICAL:
        raise GovernanceError("synthetic or fixture tournament evidence is prohibited")
    if tournament.production_eligibility_state != "production-eligible":
        reasons = ",".join(tournament.production_ineligibility_reasons)
        raise GovernanceError(f"historical tournament is production-ineligible: {reasons}")
    winner_id = tournament.provisional_winner_candidate_id
    if winner_id is None:
        raise GovernanceError("historical tournament produced no converged winner")
    winner = next(item for item in _CANDIDATES if item.candidate_id == winner_id)
    incumbents = tuple(item for item in _CANDIDATES if item.candidate_id != winner_id)
    if len(incumbents) != 1:
        raise GovernanceError("historical tournament has no exact bootstrap incumbent")
    incumbent = incumbents[0]
    published = {
        candidate.candidate_id: _publish_candidate(
            paths=paths,
            base=base,
            history=history,
            tournament=tournament,
            tournament_artifact=tournament_artifact,
            candidate=candidate,
        )
        for candidate in (incumbent, winner)
    }
    incumbent_model = published[incumbent.candidate_id]
    winning_model = published[winner.candidate_id]
    incumbent_evidence = _governance_evidence(
        tournament=tournament,
        tournament_artifact=tournament_artifact,
        published=incumbent_model,
    )
    winner_evidence = _governance_evidence(
        tournament=tournament,
        tournament_artifact=tournament_artifact,
        published=winning_model,
    )
    policy = PromotionPolicy()
    with connect_database(paths.sqlite_path) as connection:
        with transaction(connection, immediate=True):
            repository = ModelGovernanceRepository(connection)
            active = tuple(
                item
                for item in repository.list_models()
                if item.role is ModelRole.CHAMPION
                and item.lifecycle_status.value == "promoted"
                and item.sport_code == "football"
                and item.market_key == MARKET_KEY_MATCH_RESULT_1X2
            )
            if active:
                raise GovernanceError("a conflicting active champion appeared during preparation")
            incumbent_entry = repository.register_verified_score_model(
                artifact=incumbent_model.artifact,
                model=incumbent_model.model,
                relative_path=incumbent_model.relative_directory,
                market_key=MARKET_KEY_MATCH_RESULT_1X2,
                registered_at=evaluated_at_utc,
                actor="operator-ui",
                role=ModelRole.CHALLENGER,
                provenance=_provenance(
                    history.competition_id,
                    tournament_artifact,
                    incumbent_model,
                ),
            )
            winner_entry = repository.register_verified_score_model(
                artifact=winning_model.artifact,
                model=winning_model.model,
                relative_path=winning_model.relative_directory,
                market_key=MARKET_KEY_MATCH_RESULT_1X2,
                registered_at=evaluated_at_utc,
                actor="operator-ui",
                role=ModelRole.CHALLENGER,
                provenance=_provenance(
                    history.competition_id,
                    tournament_artifact,
                    winning_model,
                ),
            )
            decision = evaluate_challenger(
                champion=incumbent_entry,
                challenger=winner_entry,
                champion_evidence=incumbent_evidence,
                challenger_evidence=winner_evidence,
                policy=policy,
                as_of_utc=winner_evidence.window_end_utc,
            )
            repository.record_decision(
                decision=decision,
                actor="operator-ui",
                created_at=evaluated_at_utc,
            )
            transition_id = (
                repository.apply_initial_promotion(
                    decision_id=decision.decision_id,
                    actor="operator-ui",
                    occurred_at=evaluated_at_utc,
                )
                if decision.decision is GovernanceDecisionKind.PROMOTE
                else None
            )
    if transition_id is None:
        reasons = ",".join(decision.reasons)
        raise GovernanceError(f"governance decision {decision.decision.value}: {reasons}")
    resolved = _active_champion(paths, history.competition_id)
    if resolved is None or resolved.model_artifact_id != winning_model.artifact.artifact_id:
        raise GovernanceError("authorized promotion did not resolve the expected champion")
    return PreparedChampion(
        history.competition_id,
        resolved.model_artifact_id,
        tournament_artifact.artifact_id,
        decision.decision_id,
        transition_id,
        False,
    )


def _active_champion(
    paths: RuntimePaths,
    competition_id: str,
) -> ResolvedScoreChampion | None:
    with connect_database(paths.sqlite_path, read_only=True) as connection:
        return resolve_active_score_champion(
            connection=connection,
            model_root=paths.models_directory,
            competition_id=competition_id,
            market_key=MARKET_KEY_MATCH_RESULT_1X2,
        )


def _publish_tournament(
    *,
    root: Path,
    relative_directory: str,
    tournament: FootballScoreTournament,
) -> AnalyticalArtifact:
    try:
        return write_tournament_artifact(
            root=root,
            relative_directory=relative_directory,
            tournament=tournament,
        )
    except ArtifactError:
        existing = load_tournament_artifact(
            root=root,
            relative_directory=relative_directory,
        )
        if existing.payload != tournament_payload(tournament):
            raise GovernanceError(
                "existing historical tournament conflicts with verified evidence"
            ) from None
        return existing


def _load_existing_tournament(
    *,
    root: Path,
    relative_directory: str,
    history: _CompetitionHistory,
) -> tuple[AnalyticalArtifact, FootballScoreTournament] | None:
    if not (root / relative_directory).exists():
        return None
    artifact = load_tournament_artifact(
        root=root,
        relative_directory=relative_directory,
    )
    try:
        payload = cast(dict[str, JsonValue], artifact.payload)
        metrics = tuple(
            _fold_metric(cast(dict[str, JsonValue], item))
            for item in cast(list[JsonValue], payload["fold_metrics"])
        )
        summaries = tuple(
            _candidate_summary(cast(dict[str, JsonValue], item))
            for item in cast(list[JsonValue], payload["candidate_summaries"])
        )
        winner_value = payload["provisional_winner_candidate_id"]
        winner = None if winner_value is None else str(winner_value)
        tournament = FootballScoreTournament(
            candidates=_CANDIDATES,
            folds=build_tournament_folds(history.matches, configuration=_SPLIT),
            fold_metrics=metrics,
            summaries=summaries,
            provisional_winner_candidate_id=winner,
            provisional_winner_reason=str(payload["provisional_winner_reason"]),
            evaluation_provenance=EvaluationProvenance(str(payload["evaluation_provenance"])),
            production_eligibility_state=str(payload["production_eligibility_state"]),
            production_ineligibility_reasons=tuple(
                str(item)
                for item in cast(
                    list[JsonValue],
                    payload["production_ineligibility_reasons"],
                )
            ),
            multinomial_baseline_state=str(payload["multinomial_baseline_state"]),
            market_baseline_state=str(payload["market_baseline_state"]),
            promotion_state=str(payload["promotion_state"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise GovernanceError("persisted historical tournament is malformed") from exc
    if tournament_payload(tournament) != artifact.payload:
        raise GovernanceError("persisted historical tournament is incompatible")
    return artifact, tournament


def _fold_metric(row: dict[str, JsonValue]) -> TournamentFoldMetric:
    return TournamentFoldMetric(
        candidate_id=_stored_str(row, "candidate_id"),
        fold_id=_stored_str(row, "fold_id"),
        training_end=date.fromisoformat(_stored_str(row, "training_end")),
        calibration_start=date.fromisoformat(_stored_str(row, "calibration_start")),
        calibration_end=date.fromisoformat(_stored_str(row, "calibration_end")),
        test_start=date.fromisoformat(_stored_str(row, "test_start")),
        test_end=date.fromisoformat(_stored_str(row, "test_end")),
        temperature=_stored_float(row, "temperature"),
        exact_score_negative_log_likelihood=_stored_float(
            row,
            "exact_score_negative_log_likelihood",
        ),
        result_log_loss=_stored_float(row, "result_log_loss"),
        result_brier=_stored_float(row, "result_brier"),
        ranked_probability_score=_stored_float(row, "ranked_probability_score"),
        mean_absolute_goal_error=_stored_float(row, "mean_absolute_goal_error"),
        test_rows=_stored_int(row, "test_rows"),
        converged=_stored_bool(row, "converged"),
    )


def _candidate_summary(row: dict[str, JsonValue]) -> TournamentCandidateSummary:
    return TournamentCandidateSummary(
        candidate_id=_stored_str(row, "candidate_id"),
        model_family=_stored_str(row, "model_family"),
        fold_count=_stored_int(row, "fold_count"),
        exact_score_negative_log_likelihood=_stored_float(
            row,
            "exact_score_negative_log_likelihood",
        ),
        result_log_loss=_stored_float(row, "result_log_loss"),
        result_brier=_stored_float(row, "result_brier"),
        ranked_probability_score=_stored_float(row, "ranked_probability_score"),
        mean_absolute_goal_error=_stored_float(row, "mean_absolute_goal_error"),
        all_folds_converged=_stored_bool(row, "all_folds_converged"),
    )


def _stored_str(row: dict[str, JsonValue], key: str) -> str:
    value = row[key]
    if type(value) is not str:
        raise ValueError(f"{key} must be a string")
    return value


def _stored_float(row: dict[str, JsonValue], key: str) -> float:
    value = row[key]
    if type(value) is not float or not math.isfinite(value):
        raise ValueError(f"{key} must be a finite float")
    return value


def _stored_int(row: dict[str, JsonValue], key: str) -> int:
    value = row[key]
    if type(value) is not int:
        raise ValueError(f"{key} must be an integer")
    return value


def _stored_bool(row: dict[str, JsonValue], key: str) -> bool:
    value = row[key]
    if type(value) is not bool:
        raise ValueError(f"{key} must be a boolean")
    return value


def _publish_candidate(
    *,
    paths: RuntimePaths,
    base: str,
    history: _CompetitionHistory,
    tournament: FootballScoreTournament,
    tournament_artifact: AnalyticalArtifact,
    candidate: ScoreTournamentCandidate,
) -> _PublishedCandidate:
    relative = f"{base}/models/{candidate.candidate_id}"
    if (paths.models_directory / relative).exists():
        artifact, model = load_score_model_artifact(
            root=paths.models_directory,
            relative_directory=relative,
        )
    else:
        model = _fit(candidate, history.matches)
        try:
            artifact = write_score_model_artifact(
                root=paths.models_directory,
                relative_directory=relative,
                model=model,
            )
        except ArtifactError:
            artifact, existing_model = load_score_model_artifact(
                root=paths.models_directory,
                relative_directory=relative,
            )
            if existing_model != model:
                raise GovernanceError(
                    "existing score model conflicts with verified tournament winner"
                ) from None
    artifact, model = load_score_model_artifact(
        root=paths.models_directory,
        relative_directory=relative,
        expected_artifact_id=artifact.artifact_id,
        expected_checksum=artifact.checksum_sha256,
    )
    if (
        not model.diagnostics.converged
        or model.competition_id != history.competition_id
        or model.model_family != candidate.model_family
        or model.configuration != candidate.configuration
    ):
        raise GovernanceError("strictly reloaded winner model is incompatible")
    temperatures = sorted(
        item.temperature
        for item in tournament.fold_metrics
        if item.candidate_id == candidate.candidate_id
    )
    if not temperatures:
        raise GovernanceError("candidate has no persisted calibration temperature")
    temperature = temperatures[len(temperatures) // 2]
    calibration_relative = f"{base}/calibration/{candidate.candidate_id}"
    if (paths.models_directory / calibration_relative).exists():
        calibration, loaded_temperature = load_score_calibration_artifact(
            root=paths.models_directory,
            relative_directory=calibration_relative,
            expected_model_artifact_id=artifact.artifact_id,
            expected_training_lineage=tournament_artifact.artifact_id,
        )
    else:
        try:
            calibration = write_score_calibration_artifact(
                root=paths.models_directory,
                relative_directory=calibration_relative,
                model_artifact_id=artifact.artifact_id,
                training_lineage=tournament_artifact.artifact_id,
                temperature=temperature,
            )
        except ArtifactError:
            calibration, loaded_temperature = load_score_calibration_artifact(
                root=paths.models_directory,
                relative_directory=calibration_relative,
                expected_model_artifact_id=artifact.artifact_id,
                expected_training_lineage=tournament_artifact.artifact_id,
            )
    calibration, loaded_temperature = load_score_calibration_artifact(
        root=paths.models_directory,
        relative_directory=calibration_relative,
        expected_artifact_id=calibration.artifact_id,
        expected_checksum=calibration.checksum_sha256,
        expected_model_artifact_id=artifact.artifact_id,
        expected_training_lineage=tournament_artifact.artifact_id,
    )
    if not math.isclose(loaded_temperature, temperature):
        raise GovernanceError("existing score calibration conflicts with tournament")
    return _PublishedCandidate(
        candidate,
        artifact,
        model,
        relative,
        calibration,
        calibration_relative,
        loaded_temperature,
    )


def _fit(
    candidate: ScoreTournamentCandidate,
    matches: tuple[ScoreTrainingMatch, ...],
) -> FootballScoreModel:
    if candidate.model_family == INDEPENDENT_POISSON:
        return fit_independent_poisson(matches, configuration=candidate.configuration)
    if candidate.model_family == DIXON_COLES:
        return fit_dixon_coles(matches, configuration=candidate.configuration)
    raise GovernanceError("tournament winner model family is unsupported")


def _governance_evidence(
    *,
    tournament: FootballScoreTournament,
    tournament_artifact: AnalyticalArtifact,
    published: _PublishedCandidate,
) -> ModelEvaluationEvidence:
    metrics = tuple(
        item
        for item in tournament.fold_metrics
        if item.candidate_id == published.candidate.candidate_id
    )
    if not metrics or not all(item.converged for item in metrics):
        raise GovernanceError("candidate convergence evidence is incomplete")
    sample_size = sum(item.test_rows for item in metrics)
    tests = tuple(match for fold in tournament.folds for match in fold.test)
    if sample_size != len(tests):
        raise GovernanceError("tournament metric population differs from test folds")
    calibration_error = _calibration_error(tournament, published.candidate)
    if calibration_error is None:
        raise GovernanceError("candidate calibration evidence is insufficient")
    return ModelEvaluationEvidence(
        evidence_artifact_id=tournament_artifact.artifact_id,
        evidence_checksum_sha256=tournament_artifact.checksum_sha256,
        model_artifact_id=published.artifact.artifact_id,
        sport_code="football",
        market_key=MARKET_KEY_MATCH_RESULT_1X2,
        evaluation_mode="verified-historical-rolling-origin",
        window_start_utc=datetime.combine(
            min(item.event_date for item in tests),
            datetime.min.time(),
            tzinfo=UTC,
        ),
        window_end_utc=datetime.combine(
            max(item.event_date for item in tests),
            datetime.max.time(),
            tzinfo=UTC,
        ),
        event_population_id=content_addressed_id(
            identity_type="score-governance-event-population-v1",
            payload={
                "event_ids": cast(
                    list[JsonValue],
                    sorted(item.canonical_event_id for item in tests),
                ),
            },
        ),
        sample_size=sample_size,
        completed_result_count=sample_size,
        coverage=1.0,
        log_loss=_weighted_metric(metrics, "result_log_loss"),
        multiclass_brier_score=_weighted_metric(metrics, "result_brier"),
        calibration_error=calibration_error,
    )


def _calibration_error(
    tournament: FootballScoreTournament,
    candidate: ScoreTournamentCandidate,
) -> float | None:
    probabilities: list[tuple[float, float, float]] = []
    targets: list[int] = []
    metric_by_fold = {
        item.fold_id: item
        for item in tournament.fold_metrics
        if item.candidate_id == candidate.candidate_id
    }
    for fold in tournament.folds:
        metric = metric_by_fold.get(fold.fold_id)
        if metric is None:
            raise GovernanceError("candidate calibration fold evidence is missing")
        model = _fit(candidate, fold.training)
        for match in fold.test:
            surface = temperature_scale_distribution(
                predict_joint_score(
                    model,
                    home_team_id=match.home_team_id,
                    away_team_id=match.away_team_id,
                    prediction_cutoff=match.event_date,
                ),
                temperature=metric.temperature,
            )
            probabilities.append(
                (
                    predicate_probability(
                        surface,
                        primitive(ScorePredicateKind.HOME_WIN),
                    ),
                    predicate_probability(surface, primitive(ScorePredicateKind.DRAW)),
                    predicate_probability(
                        surface,
                        primitive(ScorePredicateKind.AWAY_WIN),
                    ),
                )
            )
            targets.append(
                0
                if match.home_goals > match.away_goals
                else 1
                if match.home_goals == match.away_goals
                else 2
            )
    diagnostics = multiclass_calibration_diagnostics(
        np.asarray(probabilities, dtype=np.float64),
        np.asarray(targets, dtype=np.int64),
    )
    return diagnostics.expected_calibration_error


def _weighted_metric(
    metrics: tuple[TournamentFoldMetric, ...],
    field: str,
) -> float:
    rows = [(float(getattr(item, field)), item.test_rows) for item in metrics]
    total = sum(weight for _value, weight in rows)
    if total <= 0:
        raise GovernanceError("governance metric has no test population")
    return math.fsum(value * weight for value, weight in rows) / total


def _provenance(
    competition_id: str,
    tournament_artifact: AnalyticalArtifact,
    published: _PublishedCandidate,
) -> JsonValue:
    return {
        "competition_id": competition_id,
        "model_purpose": FOOTBALL_PRODUCT_MODEL_PURPOSE,
        "probability_generator_scope": FOOTBALL_PROBABILITY_GENERATOR_SCOPE,
        "evaluation_mode": FOOTBALL_PRODUCTION_EVALUATION_MODE,
        "artifact_type": published.artifact.artifact_type,
        "artifact_schema": published.artifact.schema_version,
        "model_family": published.model.model_family,
        "training_lineage": tournament_artifact.artifact_id,
        "calibration": {
            "method": "global-temperature",
            "relative_directory": published.calibration_relative_directory,
            "lineage_artifact_id": published.calibration_artifact.artifact_id,
            "lineage_checksum_sha256": published.calibration_artifact.checksum_sha256,
        },
    }
