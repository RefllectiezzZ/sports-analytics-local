"""Production/operator football inference from verified evidence only."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
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
from sports_analytics.models.football_scores import (
    predict_joint_score,
    temperature_scale_distribution,
)
from sports_analytics.players.evidence import (
    PlayerEvidenceBundle,
    load_player_evidence_artifact,
    player_capability_matrix,
)
from sports_analytics.policies.proposal import (
    PublishedProposalPolicy,
    load_published_proposal_policy,
)
from sports_analytics.predictions.football_scores import write_football_probability_artifact
from sports_analytics.proposals.football import (
    FootballOpportunityPolicy,
    ProposalRun,
    ProposalSportPolicy,
    evaluate_catalogue_proposals,
    write_proposal_artifact,
)
from sports_analytics.services.champion_resolution import resolve_active_score_champion
from sports_analytics.services.football_product import (
    FOOTBALL_PRODUCT_READ_MODEL_SCHEMA,
    FOOTBALL_PRODUCT_READ_MODEL_TYPE,
)
from sports_analytics.upcoming_events import UpcomingEvent, load_upcoming_event_artifact

PRODUCTION_ECONOMIC_HOLDS: Final[tuple[str, ...]] = (
    "historical-closing-only-evidence",
    "market-baseline-materially-better",
    "negative-historical-closing-backtest",
    "no-prospective-settlement-cycle",
    "no-prospective-timestamped-evidence",
)


@dataclass(frozen=True, slots=True)
class ProductionFootballProductRequest:
    upcoming_event_relative_directory: str
    upcoming_event_artifact_id: str
    upcoming_event_checksum_sha256: str
    competition_id: str
    market_key: str
    evaluated_at_utc: datetime
    relative_root: str
    proposal_policy_relative_directory: str
    proposal_policy_checksum_sha256: str
    operator_quotes: tuple[OperatorQuoteInput, ...] = ()
    registered_provider_ids: frozenset[str] = frozenset()
    quote_policy: OperatorQuotePolicy = OperatorQuotePolicy()
    player_context_relative_directory: str | None = None
    player_context_checksum_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class PublishedProductionFootballProduct:
    read_model_artifact: AnalyticalArtifact
    probability_artifacts: tuple[AnalyticalArtifact, ...]
    quote_artifact: AnalyticalArtifact | None
    proposal_artifact: AnalyticalArtifact | None
    quote_catalogue: OperatorQuoteCatalogue | None
    proposals: ProposalRun | None


def run_and_publish_production_football_product(
    *,
    connection: sqlite3.Connection,
    exports_root: Path,
    model_root: Path,
    request: ProductionFootballProductRequest,
) -> PublishedProductionFootballProduct:
    """Perform verified inference only; this function contains no training path."""
    base = _safe_relative(request.relative_root)
    event_artifact, events = load_upcoming_event_artifact(
        root=exports_root,
        relative_directory=request.upcoming_event_relative_directory,
        expected_checksum=request.upcoming_event_checksum_sha256,
        expected_artifact_id=request.upcoming_event_artifact_id,
    )
    if {item.competition_id for item in events} != {request.competition_id}:
        raise EvaluationError("verified upcoming events do not match product competition")
    policy_artifact, published_policy = load_published_proposal_policy(
        root=exports_root,
        relative_directory=request.proposal_policy_relative_directory,
        expected_checksum=request.proposal_policy_checksum_sha256,
    )
    player_artifact_id, player_payload, player_state = _load_player_context(
        exports_root=exports_root,
        request=request,
        events=events,
    )
    champion = resolve_active_score_champion(
        connection=connection,
        model_root=model_root,
        competition_id=request.competition_id,
        market_key=request.market_key,
    )
    if champion is None:
        read_model = _write_no_champion_read_model(
            exports_root=exports_root,
            base=base,
            events=events,
            event_artifact=event_artifact,
            policy_artifact=policy_artifact,
            published_policy=published_policy,
            player_artifact_id=player_artifact_id,
            player_payload=player_payload,
        )
        return PublishedProductionFootballProduct(read_model, (), None, None, None, None)

    event_markets: dict[str, tuple[FootballMarketProbability, ...]] = {}
    probability_artifacts: list[AnalyticalArtifact] = []
    for event in events:
        distribution = temperature_scale_distribution(
            predict_joint_score(
                champion.model,
                home_team_id=event.canonical_home_participant_id,
                away_team_id=event.canonical_away_participant_id,
                prediction_cutoff=request.evaluated_at_utc.date(),
            ),
            temperature=champion.calibration_temperature,
        )
        event_markets[event.canonical_event_id] = derive_full_time_markets(distribution)
        probability_artifacts.append(
            write_football_probability_artifact(
                root=exports_root,
                relative_directory=f"{base}/probabilities/{event.canonical_event_id}",
                canonical_event_id=event.canonical_event_id,
                model_artifact_id=champion.model_artifact_id,
                distribution=distribution,
            )
        )
    quote_catalogue, quote_artifact = _quotes(
        exports_root=exports_root,
        base=base,
        request=request,
        events=events,
    )
    holds = PRODUCTION_ECONOMIC_HOLDS + (
        ("player-train-serve-equivalence-unavailable",)
        if player_state == "train-serve-equivalence-unavailable"
        else ()
    )
    opportunity_eligible = quote_catalogue is not None
    proposals: ProposalRun | None = None
    proposal_artifact: AnalyticalArtifact | None = None
    if opportunity_eligible:
        proposals = evaluate_catalogue_proposals(
            event_markets=event_markets,
            catalogue=quote_catalogue,
            model_artifact_ids={
                item.canonical_event_id: champion.model_artifact_id for item in events
            },
            decision_as_of_utc=request.evaluated_at_utc,
            policy=_opportunity_policy(published_policy),
            player_context_state=player_state,
            evidence_hold_reasons=holds,
        )
        proposal_artifact = write_proposal_artifact(
            root=exports_root,
            relative_directory=f"{base}/proposals",
            run=proposals,
        )
    payload = _base_payload(
        events=events,
        player_payload=player_payload,
        published_policy=published_policy,
        eligibility={
            "model_artifact_valid": True,
            "fair_odds_eligible": True,
            "opportunity_analysis_eligible": opportunity_eligible,
            "bet_proposal_eligible": False,
            "promotion_eligible": False,
        },
        operational_state=("economic-evidence-hold" if opportunity_eligible else "fair-odds-only"),
        abstention_reasons=holds if opportunity_eligible else ("quote-unavailable",),
    )
    payload["model_status"] = {
        "state": "active-production-champion",
        "model_artifact_id": champion.model_artifact_id,
        "model_checksum_sha256": champion.model_checksum_sha256,
        "model_family": champion.model_family,
        "training_lineage": champion.training_lineage,
        "calibration_lineage": champion.calibration_lineage,
        "calibration_checksum_sha256": champion.calibration_checksum_sha256,
        "active_role_revision": champion.active_role_revision,
        "active_transition_id": champion.active_transition_id,
        "promotion_state": "explicitly-promoted",
        "player_context_consumption": "not-consumed",
    }
    analytical_candidates = (
        0
        if proposals is None
        else sum(item.offered_decimal_odds is not None for item in proposals.decisions)
    )
    product_state = payload["product_state"]
    if not isinstance(product_state, dict):
        raise EvaluationError("football product state assembly failed")
    product_state["analytical_candidate_count"] = analytical_candidates
    product_state["research_only_proposal_count"] = analytical_candidates
    product_state["placeable_manual_proposal_count"] = 0
    payload["artifact_lineage"] = {
        "upcoming_event_artifact_id": event_artifact.artifact_id,
        "upcoming_event_checksum_sha256": event_artifact.checksum_sha256,
        "model_artifact_id": champion.model_artifact_id,
        "probability_artifact_ids": [item.artifact_id for item in probability_artifacts],
        "quote_artifact_id": None if quote_artifact is None else quote_artifact.artifact_id,
        "proposal_artifact_id": (
            None if proposal_artifact is None else proposal_artifact.artifact_id
        ),
        "player_context_artifact_id": player_artifact_id,
        "published_proposal_policy_artifact_id": policy_artifact.artifact_id,
        "player_context_model_consumption": "not-consumed",
    }
    read_model = write_analytical_artifact(
        root=exports_root,
        relative_directory=f"{base}/read-model",
        artifact_type=FOOTBALL_PRODUCT_READ_MODEL_TYPE,
        schema_version=FOOTBALL_PRODUCT_READ_MODEL_SCHEMA,
        payload=payload,
    )
    return PublishedProductionFootballProduct(
        read_model,
        tuple(probability_artifacts),
        quote_artifact,
        proposal_artifact,
        quote_catalogue,
        proposals,
    )


def _write_no_champion_read_model(
    *,
    exports_root: Path,
    base: str,
    events: tuple[UpcomingEvent, ...],
    event_artifact: AnalyticalArtifact,
    policy_artifact: AnalyticalArtifact,
    published_policy: PublishedProposalPolicy,
    player_artifact_id: str | None,
    player_payload: dict[str, JsonValue],
) -> AnalyticalArtifact:
    payload = _base_payload(
        events=events,
        player_payload=player_payload,
        published_policy=published_policy,
        eligibility={
            "model_artifact_valid": False,
            "fair_odds_eligible": False,
            "opportunity_analysis_eligible": False,
            "bet_proposal_eligible": False,
            "promotion_eligible": False,
        },
        operational_state="no-production-champion",
        abstention_reasons=("no-production-champion",),
    )
    payload["model_status"] = {
        "state": "no-production-champion",
        "model_artifact_id": None,
        "probability_state": "unavailable",
        "fair_odds_state": "unavailable",
        "opportunity_analysis_state": "unavailable",
        "bet_proposal_state": "unavailable",
        "promotion_state": "explicit-governance-required",
        "player_context_consumption": "not-consumed",
    }
    payload["artifact_lineage"] = {
        "upcoming_event_artifact_id": event_artifact.artifact_id,
        "upcoming_event_checksum_sha256": event_artifact.checksum_sha256,
        "model_artifact_id": None,
        "probability_artifact_ids": [],
        "quote_artifact_id": None,
        "proposal_artifact_id": None,
        "player_context_artifact_id": player_artifact_id,
        "published_proposal_policy_artifact_id": policy_artifact.artifact_id,
        "player_context_model_consumption": "not-consumed",
    }
    return write_analytical_artifact(
        root=exports_root,
        relative_directory=f"{base}/read-model",
        artifact_type=FOOTBALL_PRODUCT_READ_MODEL_TYPE,
        schema_version=FOOTBALL_PRODUCT_READ_MODEL_SCHEMA,
        payload=payload,
    )


def _base_payload(
    *,
    events: tuple[UpcomingEvent, ...],
    player_payload: dict[str, JsonValue],
    published_policy: PublishedProposalPolicy,
    eligibility: dict[str, JsonValue],
    operational_state: str,
    abstention_reasons: tuple[str, ...],
) -> dict[str, JsonValue]:
    return {
        "model_status": {},
        "artifact_lineage": {},
        "product_state": {
            "mode": "production-operator-inference",
            "operational_state": operational_state,
            "eligibility": eligibility,
            "abstention_reasons": list(sorted(set(abstention_reasons))),
            "economic_evidence": {
                "historical_price_semantics": "closing-benchmark-diagnostic-only",
                "historical_backtest_state": "negative",
                "compatible_market_baseline_comparison": ("market-baseline-materially-better"),
                "prospective_timestamped_quote_result_cycle": "unavailable",
            },
            "proposal_count": 0,
            "analytical_candidate_count": 0,
            "research_only_proposal_count": 0,
            "placeable_manual_proposal_count": 0,
            "accumulator_count": 0,
            "placement_state": "manual-only",
            "automatic_bookmaker_access": False,
            "events": [item.to_json() for item in events],
            "sport_policy": {
                "allowed_sports": list(published_policy.allowed_sports),
                "mode": published_policy.combination_mode.value,
                "policy_id": _opportunity_policy(published_policy).sport_policy.policy_id,
                "published_configuration_id": published_policy.configuration_id,
            },
            "proposal_limits": published_policy.to_json(),
            "sport_statuses": [
                {
                    "sport_code": "football",
                    "status": operational_state,
                }
            ],
            "player_context": player_payload,
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


def _load_player_context(
    *,
    exports_root: Path,
    request: ProductionFootballProductRequest,
    events: tuple[UpcomingEvent, ...],
) -> tuple[str | None, dict[str, JsonValue], str]:
    if request.player_context_relative_directory is None:
        if request.player_context_checksum_sha256 is not None:
            raise EvaluationError("player context checksum requires an artifact path")
        return (
            None,
            {
                "display_state": "context-unavailable",
                "model_use_state": "player-context-not-trainable",
                "historical_equivalence_state": "unavailable",
                "observations": [],
                "capabilities": [
                    {"capability": capability, "state": state}
                    for capability, state in player_capability_matrix()
                ],
            },
            "not-requested",
        )
    artifact, bundle = load_player_evidence_artifact(
        root=exports_root,
        relative_directory=request.player_context_relative_directory,
        expected_checksum=request.player_context_checksum_sha256,
    )
    _reconcile_player_events(bundle, events=events)
    observations: list[JsonValue] = [
        item.to_json()
        for item in sorted(bundle.observations, key=lambda item: item.observation_id)
        if item.event_id is not None
    ]
    return (
        artifact.artifact_id,
        {
            "display_state": "display-only-current-context",
            "model_use_state": "player-context-not-trainable",
            "historical_equivalence_state": "unavailable",
            "observations": observations,
            "capabilities": [
                {"capability": capability, "state": state}
                for capability, state in player_capability_matrix()
            ],
        },
        "train-serve-equivalence-unavailable",
    )


def _reconcile_player_events(
    bundle: PlayerEvidenceBundle, *, events: tuple[UpcomingEvent, ...]
) -> None:
    by_id = {item.canonical_event_id: item for item in events}
    for observation in bundle.observations:
        if observation.event_id is None:
            continue
        event = by_id.get(observation.event_id)
        if event is None:
            raise EvaluationError("player context references an unverified upcoming event")
        if observation.event_start_utc != event.event_start_utc:
            raise EvaluationError("player context event schedule does not match verified event")
        if observation.canonical_team_id not in {
            event.canonical_home_participant_id,
            event.canonical_away_participant_id,
        }:
            raise EvaluationError("player context team is not an event participant")


def _quotes(
    *,
    exports_root: Path,
    base: str,
    request: ProductionFootballProductRequest,
    events: tuple[UpcomingEvent, ...],
) -> tuple[OperatorQuoteCatalogue | None, AnalyticalArtifact | None]:
    if not request.operator_quotes:
        return None, None
    catalogue = validate_operator_quotes(
        request.operator_quotes,
        registered_provider_ids=request.registered_provider_ids,
        events=tuple(
            OperatorEventReference(
                canonical_event_id=item.canonical_event_id,
                sport_code=item.sport_code,
                event_start_utc=item.event_start_utc,
            )
            for item in events
        ),
        evaluated_at_utc=request.evaluated_at_utc,
        policy=request.quote_policy,
    )
    artifact = write_operator_quote_artifact(
        root=exports_root,
        relative_directory=f"{base}/current-quotes",
        catalogue=catalogue,
    )
    return catalogue, artifact


def _opportunity_policy(policy: PublishedProposalPolicy) -> FootballOpportunityPolicy:
    return FootballOpportunityPolicy(
        minimum_edge=policy.minimum_edge,
        minimum_expected_value=policy.minimum_expected_value,
        minimum_total_odds=policy.minimum_total_odds,
        maximum_total_odds=policy.maximum_total_odds,
        maximum_uncertainty=policy.maximum_uncertainty,
        minimum_legs=policy.minimum_legs,
        maximum_legs=policy.maximum_legs,
        sport_policy=ProposalSportPolicy(
            allowed_sports=policy.allowed_sports,
            mode=policy.combination_mode,
        ),
    )


def _safe_relative(value: str) -> str:
    normalized = value.strip("/")
    if not normalized or "\\" in normalized or ".." in normalized.split("/"):
        raise EvaluationError("football product relative root is unsafe")
    return normalized
