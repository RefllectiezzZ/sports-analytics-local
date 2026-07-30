"""Safe JSON adapter for the complete offline football product workflow."""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import cast

from sports_analytics.bookmakers.operator_quotes import parse_operator_quote_json
from sports_analytics.core.exceptions import ConfigurationError
from sports_analytics.data.codec import dumps_canonical_json
from sports_analytics.data.types import JsonValue
from sports_analytics.markets.capabilities import market_capability_matrix
from sports_analytics.models.football_scores import (
    ScoreModelConfiguration,
    ScoreTrainingMatch,
)
from sports_analytics.models.football_tournament import TournamentSplitConfiguration
from sports_analytics.policies.proposal import PublishedProposalPolicy
from sports_analytics.proposals.football import (
    FootballOpportunityPolicy,
    ProposalSportPolicy,
)
from sports_analytics.services.football_product import (
    FootballProductRequest,
    UpcomingFootballEvent,
    run_and_publish_football_product,
)


def run_football_product_json(
    *,
    path_text: str,
    exports_root: Path,
    published_policy: PublishedProposalPolicy | None = None,
    published_policy_artifact_id: str | None = None,
) -> dict[str, JsonValue]:
    """Parse one bounded static payload and publish the complete persisted workflow."""
    return run_football_product_document(
        document=_read_json(path_text),
        exports_root=exports_root,
        published_policy=published_policy,
        published_policy_artifact_id=published_policy_artifact_id,
    )


def run_football_product_document(
    *,
    document: object,
    exports_root: Path,
    published_policy: PublishedProposalPolicy | None = None,
    published_policy_artifact_id: str | None = None,
) -> dict[str, JsonValue]:
    """Validate an already decoded static payload for CLI and frozen jobs."""
    if not isinstance(document, dict) or set(document) != {
        "relative_root",
        "evaluated_at_utc",
        "registered_provider_ids",
        "historical_matches",
        "upcoming_events",
        "operator_quotes",
        "split_configuration",
        "opportunity_policy",
    }:
        raise ConfigurationError("football product JSON fields are not exact")
    matches_raw = _array(document["historical_matches"], "historical_matches", maximum=20_000)
    matches = tuple(_score_match(item, index) for index, item in enumerate(matches_raw))
    events_raw = _array(document["upcoming_events"], "upcoming_events", maximum=500)
    events = tuple(_upcoming_event(item, index) for index, item in enumerate(events_raw))
    provider_values = _array(
        document["registered_provider_ids"],
        "registered_provider_ids",
        maximum=100,
    )
    requested_providers = frozenset(
        _string(value, f"registered_provider_ids[{index}]")
        for index, value in enumerate(provider_values)
    )
    quotes_raw = _array(document["operator_quotes"], "operator_quotes", maximum=5_000)
    quote_bytes = dumps_canonical_json(cast(JsonValue, quotes_raw)).encode("utf-8")
    quotes = () if not quotes_raw else parse_operator_quote_json(quote_bytes)
    providers = requested_providers
    if published_policy is not None and published_policy.provider_policy:
        allowed_providers = frozenset(published_policy.provider_policy)
        excluded = sorted(
            item.provider_id for item in quotes if item.provider_id not in allowed_providers
        )
        if excluded:
            raise ConfigurationError("operator quote provider is excluded by published policy")
        providers = requested_providers & allowed_providers
    split = _split_configuration(document["split_configuration"])
    input_policy = _opportunity_policy(document["opportunity_policy"])
    policy = (
        input_policy
        if published_policy is None
        else FootballOpportunityPolicy(
            minimum_offered_odds=input_policy.minimum_offered_odds,
            maximum_offered_odds=input_policy.maximum_offered_odds,
            minimum_edge=published_policy.minimum_edge,
            minimum_expected_value=published_policy.minimum_expected_value,
            safety_margin=input_policy.safety_margin,
            minimum_total_odds=published_policy.minimum_total_odds,
            maximum_total_odds=published_policy.maximum_total_odds,
            maximum_uncertainty=published_policy.maximum_uncertainty,
            minimum_legs=published_policy.minimum_legs,
            maximum_legs=published_policy.maximum_legs,
            sport_policy=ProposalSportPolicy(
                allowed_sports=published_policy.allowed_sports,
                mode=published_policy.combination_mode,
            ),
        )
    )
    evaluated = _timestamp(document["evaluated_at_utc"], "evaluated_at_utc")
    published = run_and_publish_football_product(
        exports_root=exports_root,
        request=FootballProductRequest(
            historical_matches=matches,
            upcoming_events=events,
            operator_quotes=quotes,
            registered_provider_ids=providers,
            evaluated_at_utc=evaluated,
            relative_root=_string(document["relative_root"], "relative_root"),
            score_configuration=ScoreModelConfiguration(),
            split_configuration=split,
            opportunity_policy=policy,
            published_proposal_policy=published_policy,
            published_proposal_policy_artifact_id=published_policy_artifact_id,
        ),
    )
    return {
        "tournament_artifact_id": published.tournament_artifact.artifact_id,
        "unified_tournament_artifact_id": (published.unified_tournament_artifact.artifact_id),
        "model_artifact_id": published.model_artifact.artifact_id,
        "probability_artifact_ids": cast(
            list[JsonValue],
            [item.artifact_id for item in published.probability_artifacts],
        ),
        "quote_artifact_id": (
            None if published.quote_artifact is None else published.quote_artifact.artifact_id
        ),
        "proposal_artifact_id": published.proposal_artifact.artifact_id,
        "read_model_artifact_id": published.read_model_artifact.artifact_id,
        "provisional_winner_candidate_id": (published.tournament.provisional_winner_candidate_id),
        "evaluation_provenance": published.tournament.evaluation_provenance.value,
        "production_eligibility_state": (published.tournament.production_eligibility_state),
        "promotion_state": published.tournament.promotion_state,
        "accepted_single_count": 0,
        "research_only_proposal_count": sum(
            item.accepted for item in published.proposals.decisions
        ),
        "placeable_manual_proposal_count": 0,
        "accumulator_count": 0,
        "placement_state": "manual-only",
        "execution_mode": "synthetic-contract-research-only",
        "authorization_state": "not-authorized-for-placement",
        "production_champion_state": "not-claimed",
        "bookmaker_network_access": False,
    }


def capability_payload() -> dict[str, JsonValue]:
    return {
        "capabilities": [
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
        ]
    }


def _read_json(path_text: str) -> object:
    normalized = path_text.replace("\\", "/")
    try:
        raw = Path(normalized).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ConfigurationError(f"cannot read football product JSON input: {normalized}") from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigurationError(
            f"football product JSON is malformed at line {exc.lineno}, column {exc.colno}"
        ) from exc


def _score_match(value: object, index: int) -> ScoreTrainingMatch:
    row = _exact_object(
        value,
        {
            "canonical_event_id",
            "competition_id",
            "event_date",
            "home_team_id",
            "away_team_id",
            "home_goals",
            "away_goals",
        },
        f"historical_matches[{index}]",
    )
    return ScoreTrainingMatch(
        canonical_event_id=_string(row["canonical_event_id"], "canonical_event_id"),
        competition_id=_string(row["competition_id"], "competition_id"),
        event_date=_date(row["event_date"], "event_date"),
        home_team_id=_string(row["home_team_id"], "home_team_id"),
        away_team_id=_string(row["away_team_id"], "away_team_id"),
        home_goals=_integer(row["home_goals"], "home_goals"),
        away_goals=_integer(row["away_goals"], "away_goals"),
    )


def _upcoming_event(value: object, index: int) -> UpcomingFootballEvent:
    row = _exact_object(
        value,
        {
            "canonical_event_id",
            "competition_id",
            "home_team_id",
            "away_team_id",
            "event_start_utc",
            "prediction_cutoff",
        },
        f"upcoming_events[{index}]",
    )
    return UpcomingFootballEvent(
        canonical_event_id=_string(row["canonical_event_id"], "canonical_event_id"),
        competition_id=_string(row["competition_id"], "competition_id"),
        home_team_id=_string(row["home_team_id"], "home_team_id"),
        away_team_id=_string(row["away_team_id"], "away_team_id"),
        event_start_utc=_timestamp(row["event_start_utc"], "event_start_utc"),
        prediction_cutoff=_date(row["prediction_cutoff"], "prediction_cutoff"),
    )


def _split_configuration(value: object) -> TournamentSplitConfiguration:
    row = _exact_object(
        value,
        {
            "minimum_training_rows",
            "calibration_rows",
            "test_rows",
            "maximum_folds",
        },
        "split_configuration",
    )
    return TournamentSplitConfiguration(
        minimum_training_rows=_integer(
            row["minimum_training_rows"],
            "minimum_training_rows",
        ),
        calibration_rows=_integer(row["calibration_rows"], "calibration_rows"),
        test_rows=_integer(row["test_rows"], "test_rows"),
        maximum_folds=_integer(row["maximum_folds"], "maximum_folds"),
    )


def _opportunity_policy(value: object) -> FootballOpportunityPolicy:
    row = _exact_object(
        value,
        {
            "minimum_offered_odds",
            "maximum_offered_odds",
            "minimum_edge",
            "minimum_expected_value",
            "safety_margin",
        },
        "opportunity_policy",
    )
    from decimal import InvalidOperation

    try:
        minimum_odds = Decimal(_string(row["minimum_offered_odds"], "minimum_offered_odds"))
        maximum_odds = Decimal(_string(row["maximum_offered_odds"], "maximum_offered_odds"))
    except InvalidOperation as exc:
        raise ConfigurationError("opportunity odds policy contains malformed Decimal") from exc
    return FootballOpportunityPolicy(
        minimum_offered_odds=minimum_odds,
        maximum_offered_odds=maximum_odds,
        minimum_edge=_number(row["minimum_edge"], "minimum_edge"),
        minimum_expected_value=_number(
            row["minimum_expected_value"],
            "minimum_expected_value",
        ),
        safety_margin=_number(row["safety_margin"], "safety_margin"),
    )


def _exact_object(
    value: object,
    fields: set[str],
    description: str,
) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != fields:
        raise ConfigurationError(f"{description} fields are not exact")
    return value


def _array(value: object, field: str, *, maximum: int) -> list[object]:
    if not isinstance(value, list) or len(value) > maximum:
        raise ConfigurationError(f"{field} must be a bounded JSON array")
    return value


def _string(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ConfigurationError(f"{field} must be non-empty text")
    return value


def _integer(value: object, field: str) -> int:
    if type(value) is not int or value < 0:
        raise ConfigurationError(f"{field} must be a non-negative integer")
    return value


def _number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigurationError(f"{field} must be numeric")
    return float(value)


def _date(value: object, field: str) -> date:
    try:
        return date.fromisoformat(_string(value, field))
    except ValueError as exc:
        raise ConfigurationError(f"{field} must be YYYY-MM-DD") from exc


def _timestamp(value: object, field: str) -> datetime:
    text = _string(value, field)
    if not (text.endswith("Z") or text.endswith("+00:00")):
        raise ConfigurationError(f"{field} must be canonical UTC")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ConfigurationError(f"{field} is invalid") from exc
    return parsed
