"""Persisted end-to-end coherent football analytics product workflow."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Final

from sports_analytics.artifacts import AnalyticalArtifact, write_analytical_artifact
from sports_analytics.bookmakers.operator_quotes import (
    OperatorEventReference,
    OperatorQuoteCatalogue,
    OperatorQuoteInput,
    OperatorQuotePolicy,
    validate_operator_quotes,
    write_operator_quote_artifact,
)
from sports_analytics.core.exceptions import EvaluationError
from sports_analytics.data.types import JsonValue
from sports_analytics.markets.capabilities import market_capability_matrix
from sports_analytics.markets.football_score_markets import (
    FootballMarketProbability,
    derive_full_time_markets,
)
from sports_analytics.models.football_evaluation import EvaluationProvenance
from sports_analytics.models.football_scores import (
    DIXON_COLES,
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
    TournamentSplitConfiguration,
    run_score_tournament,
    write_tournament_artifact,
)
from sports_analytics.models.football_unified_tournament import (
    UnifiedTournament,
    run_unified_tournament,
    write_unified_tournament_artifact,
)
from sports_analytics.players.evidence import PlayerEvidenceBundle, player_capability_matrix
from sports_analytics.policies.proposal import PublishedProposalPolicy
from sports_analytics.predictions.football_scores import (
    write_football_probability_artifact,
)
from sports_analytics.proposals.football import (
    FootballOpportunityPolicy,
    ProposalRun,
    evaluate_catalogue_proposals,
    write_proposal_artifact,
)

FOOTBALL_PRODUCT_READ_MODEL_TYPE: Final[str] = "football-product-read-model"
FOOTBALL_PRODUCT_READ_MODEL_SCHEMA: Final[str] = "football-product-read-model-v1"


@dataclass(frozen=True, slots=True)
class UpcomingFootballEvent:
    canonical_event_id: str
    competition_id: str
    home_team_id: str
    away_team_id: str
    event_start_utc: datetime
    prediction_cutoff: date


@dataclass(frozen=True, slots=True)
class FootballProductRequest:
    historical_matches: tuple[ScoreTrainingMatch, ...]
    upcoming_events: tuple[UpcomingFootballEvent, ...]
    operator_quotes: tuple[OperatorQuoteInput, ...]
    registered_provider_ids: frozenset[str]
    evaluated_at_utc: datetime
    relative_root: str
    score_configuration: ScoreModelConfiguration
    split_configuration: TournamentSplitConfiguration
    opportunity_policy: FootballOpportunityPolicy
    quote_policy: OperatorQuotePolicy = OperatorQuotePolicy()
    evaluation_provenance: EvaluationProvenance = EvaluationProvenance.SYNTHETIC_CONTRACT
    published_proposal_policy: PublishedProposalPolicy | None = None
    published_proposal_policy_artifact_id: str | None = None
    player_context: PlayerEvidenceBundle | None = None
    player_context_artifact_id: str | None = None


@dataclass(frozen=True, slots=True)
class PublishedFootballProduct:
    tournament: FootballScoreTournament
    unified_tournament: UnifiedTournament
    champion_model: FootballScoreModel
    quote_catalogue: OperatorQuoteCatalogue | None
    proposals: ProposalRun
    tournament_artifact: AnalyticalArtifact
    unified_tournament_artifact: AnalyticalArtifact
    model_artifact: AnalyticalArtifact
    probability_artifacts: tuple[AnalyticalArtifact, ...]
    quote_artifact: AnalyticalArtifact | None
    proposal_artifact: AnalyticalArtifact
    read_model_artifact: AnalyticalArtifact


def run_and_publish_football_product(
    *,
    exports_root: Path,
    request: FootballProductRequest,
) -> PublishedFootballProduct:
    """Run the bounded local workflow; no network or bookmaker access exists here."""
    if not request.upcoming_events:
        raise EvaluationError("football product requires at least one upcoming event")
    if (
        request.player_context is not None
        and request.player_context.historical_equivalence_state != "player-context-not-trainable"
    ):
        raise EvaluationError("football product accepts display-only player context only")
    candidates = (
        ScoreTournamentCandidate(
            candidate_id="dynamic-independent-poisson-v1",
            model_family="independent-poisson",
            configuration=request.score_configuration,
        ),
        ScoreTournamentCandidate(
            candidate_id="dynamic-dixon-coles-v1",
            model_family=DIXON_COLES,
            configuration=request.score_configuration,
        ),
    )
    tournament = run_score_tournament(
        request.historical_matches,
        candidates=candidates,
        split_configuration=request.split_configuration,
        evaluation_provenance=request.evaluation_provenance,
    )
    unified_tournament = run_unified_tournament(
        request.historical_matches,
        split_configuration=request.split_configuration,
        score_configuration=request.score_configuration,
        provenance=request.evaluation_provenance,
    )
    winner_id = tournament.provisional_winner_candidate_id
    winner_candidate = next(
        (item for item in candidates if item.candidate_id == winner_id),
        None,
    )
    if winner_candidate is None:
        raise EvaluationError("football tournament produced no contract-proof winner")
    if winner_candidate.model_family == DIXON_COLES:
        model = fit_dixon_coles(
            request.historical_matches,
            configuration=winner_candidate.configuration,
        )
    else:
        model = fit_independent_poisson(
            request.historical_matches,
            configuration=winner_candidate.configuration,
        )
    base = request.relative_root.strip("/")
    if not base or "\\" in base or ".." in base.split("/"):
        raise EvaluationError("football product relative root is unsafe")
    tournament_artifact = write_tournament_artifact(
        root=exports_root,
        relative_directory=f"{base}/tournament",
        tournament=tournament,
    )
    unified_tournament_artifact = write_unified_tournament_artifact(
        root=exports_root,
        relative_directory=f"{base}/unified-tournament",
        tournament=unified_tournament,
    )
    model_artifact = write_score_model_artifact(
        root=exports_root,
        relative_directory=f"{base}/model",
        model=model,
    )
    _, reloaded_model = load_score_model_artifact(
        root=exports_root,
        relative_directory=f"{base}/model",
        expected_checksum=model_artifact.checksum_sha256,
        expected_artifact_id=model_artifact.artifact_id,
    )
    temperatures = tuple(
        item.temperature
        for item in tournament.fold_metrics
        if item.candidate_id == winner_candidate.candidate_id
    )
    final_temperature = sorted(temperatures)[len(temperatures) // 2]
    event_markets: dict[str, tuple[FootballMarketProbability, ...]] = {}
    probability_artifacts: list[AnalyticalArtifact] = []
    for event in sorted(request.upcoming_events, key=lambda item: item.canonical_event_id):
        if event.competition_id != reloaded_model.competition_id:
            raise EvaluationError("upcoming event competition differs from champion model")
        distribution = temperature_scale_distribution(
            predict_joint_score(
                reloaded_model,
                home_team_id=event.home_team_id,
                away_team_id=event.away_team_id,
                prediction_cutoff=event.prediction_cutoff,
            ),
            temperature=final_temperature,
        )
        event_markets[event.canonical_event_id] = derive_full_time_markets(distribution)
        probability_artifacts.append(
            write_football_probability_artifact(
                root=exports_root,
                relative_directory=(f"{base}/probabilities/{event.canonical_event_id}"),
                canonical_event_id=event.canonical_event_id,
                model_artifact_id=model_artifact.artifact_id,
                distribution=distribution,
            )
        )
    quote_catalogue: OperatorQuoteCatalogue | None = None
    quote_artifact: AnalyticalArtifact | None = None
    if request.operator_quotes:
        quote_catalogue = validate_operator_quotes(
            request.operator_quotes,
            registered_provider_ids=request.registered_provider_ids,
            events=tuple(
                OperatorEventReference(
                    canonical_event_id=item.canonical_event_id,
                    sport_code="football",
                    event_start_utc=item.event_start_utc,
                )
                for item in request.upcoming_events
            ),
            evaluated_at_utc=request.evaluated_at_utc,
            policy=request.quote_policy,
        )
        quote_artifact = write_operator_quote_artifact(
            root=exports_root,
            relative_directory=f"{base}/current-quotes",
            catalogue=quote_catalogue,
        )
    player_context_payload, player_context_state = _player_context_payload(
        request.player_context,
        upcoming_events=request.upcoming_events,
        evaluated_at_utc=request.evaluated_at_utc,
    )
    proposals = evaluate_catalogue_proposals(
        event_markets=event_markets,
        catalogue=quote_catalogue,
        model_artifact_ids={
            item.canonical_event_id: model_artifact.artifact_id for item in request.upcoming_events
        },
        decision_as_of_utc=request.evaluated_at_utc,
        policy=request.opportunity_policy,
        player_context_state=player_context_state,
    )
    proposal_artifact = write_proposal_artifact(
        root=exports_root,
        relative_directory=f"{base}/proposals",
        run=proposals,
    )
    read_model_payload: dict[str, JsonValue] = {
        "model_status": {
            "contract_proof_winner_candidate_id": winner_candidate.candidate_id,
            "model_artifact_id": model_artifact.artifact_id,
            "model_family": model.model_family,
            "training_cutoff": model.training_end.isoformat(),
            "calibration_state": "coherent-global-temperature",
            "promotion_state": tournament.promotion_state,
            "evaluation_provenance": tournament.evaluation_provenance.value,
            "production_eligibility_state": tournament.production_eligibility_state,
            "production_ineligibility_reasons": list(tournament.production_ineligibility_reasons),
            "unified_contract_winner_candidate_id": (
                unified_tournament.provisional_winner_candidate_id
            ),
            "unified_candidate_states": [
                {"candidate_id": candidate, "state": state}
                for candidate, state in unified_tournament.candidate_states
            ],
            "calibration_diagnostics_state": unified_tournament.calibration_state,
            "temporal_uncertainty_state": unified_tournament.uncertainty_state,
            "rho_warning_codes": list(unified_tournament.rho_warning_codes),
        },
        "artifact_lineage": {
            "tournament_artifact_id": tournament_artifact.artifact_id,
            "unified_tournament_artifact_id": (unified_tournament_artifact.artifact_id),
            "probability_artifact_ids": [item.artifact_id for item in probability_artifacts],
            "quote_artifact_id": (None if quote_artifact is None else quote_artifact.artifact_id),
            "proposal_artifact_id": proposal_artifact.artifact_id,
            "player_context_artifact_id": request.player_context_artifact_id,
            "published_proposal_policy_artifact_id": (
                request.published_proposal_policy_artifact_id
            ),
        },
        "product_state": {
            "mode": ("fair-odds-only" if quote_catalogue is None else "current-offered-prices"),
            "proposal_count": sum(item.accepted for item in proposals.decisions),
            "accumulator_count": len(proposals.accumulators),
            "placement_state": "manual-only",
            "automatic_bookmaker_access": False,
            "sport_policy": {
                "allowed_sports": list(proposals.sport_policy.allowed_sports),
                "mode": proposals.sport_policy.mode.value,
                "policy_id": proposals.sport_policy.policy_id,
                "published_configuration_id": (
                    None
                    if request.published_proposal_policy is None
                    else request.published_proposal_policy.configuration_id
                ),
            },
            "proposal_limits": (
                None
                if request.published_proposal_policy is None
                else request.published_proposal_policy.to_json()
            ),
            "sport_statuses": [
                {"sport_code": sport, "status": status}
                for sport, status in proposals.sport_statuses
            ],
            "player_context": {
                **player_context_payload,
                "model_use_state": "player-context-not-trainable",
                "historical_equivalence_state": "unavailable",
                "capabilities": [
                    {"capability": capability, "state": state}
                    for capability, state in player_capability_matrix()
                ],
            },
        },
        "market_capabilities": [
            {
                "sport_code": item.sport_code,
                "market_family": item.market_family,
                "required_data": item.required_data,
                "model_family": item.model_family,
                "probability_state": item.probability_state.value,
                "fair_odds_state": item.fair_odds_state.value,
                "offered_price_state": item.offered_price_state.value,
                "opportunity_state": item.opportunity_state.value,
                "combination_state": item.combination_state.value,
                "limitation": item.limitation,
            }
            for item in market_capability_matrix()
        ],
    }
    read_model_artifact = write_analytical_artifact(
        root=exports_root,
        relative_directory=f"{base}/read-model",
        artifact_type=FOOTBALL_PRODUCT_READ_MODEL_TYPE,
        schema_version=FOOTBALL_PRODUCT_READ_MODEL_SCHEMA,
        payload=read_model_payload,
    )
    return PublishedFootballProduct(
        tournament=tournament,
        unified_tournament=unified_tournament,
        champion_model=reloaded_model,
        quote_catalogue=quote_catalogue,
        proposals=proposals,
        tournament_artifact=tournament_artifact,
        unified_tournament_artifact=unified_tournament_artifact,
        model_artifact=model_artifact,
        probability_artifacts=tuple(probability_artifacts),
        quote_artifact=quote_artifact,
        proposal_artifact=proposal_artifact,
        read_model_artifact=read_model_artifact,
    )


def _player_context_payload(
    bundle: PlayerEvidenceBundle | None,
    *,
    upcoming_events: tuple[UpcomingFootballEvent, ...],
    evaluated_at_utc: datetime,
) -> tuple[dict[str, JsonValue], str]:
    """Project current player evidence for display without entering model inputs."""
    if bundle is None:
        return {"display_state": "context-unavailable", "observations": []}, "not-requested"
    event_ids = {item.canonical_event_id for item in upcoming_events}
    observations = [
        item
        for item in bundle.observations
        if item.event_id in event_ids and not item.is_stale(evaluated_at_utc)
    ]
    rows: list[JsonValue] = [
        {
            "observation_id": item.observation_id,
            "canonical_player_id": item.canonical_player_id,
            "canonical_team_id": item.canonical_team_id,
            "event_id": item.event_id,
            "status": item.status.value,
            "evidence_type": item.evidence_type.value,
            "confidence": item.confidence,
        }
        for item in sorted(observations, key=lambda item: item.observation_id)
    ]
    return (
        {
            "display_state": "display-only-current-context",
            "observations": rows,
        },
        "train-serve-equivalence-unavailable",
    )
