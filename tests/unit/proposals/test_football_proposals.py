from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from sports_analytics.bookmakers.operator_quotes import (
    FOOTBALL_RULES_SCOPE,
    REGULATION_SCOPE,
    OperatorEventReference,
    OperatorQuoteInput,
    OperatorQuoteSourceKind,
    validate_operator_quotes,
)
from sports_analytics.markets.football_score_markets import (
    derive_full_time_markets,
    find_market_probability,
)
from sports_analytics.models.football_scores import joint_score_from_intensities
from sports_analytics.proposals.football import (
    FootballOpportunityPolicy,
    ProposalSportPolicy,
    SportCombinationMode,
    analyse_same_event_conjunction,
    build_same_bookmaker_accumulators,
    evaluate_proposed_single,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _catalogue(event_id: str, odds: tuple[str, str, str]):
    inputs = tuple(
        OperatorQuoteInput(
            provider_id="local-book",
            provider_display_name="Local Book",
            sport_code="football",
            canonical_event_id=event_id,
            market_family="match-result",
            outcome_key=outcome,
            line_value=None,
            market_period="full-match",
            participant_scope="event",
            canonical_participant_id=None,
            overtime_scope=REGULATION_SCOPE,
            rules_scope=FOOTBALL_RULES_SCOPE,
            offered_decimal_odds=Decimal(price),
            observed_at_utc=NOW,
            valid_until_utc=None,
            source_kind=OperatorQuoteSourceKind.MANUAL,
        )
        for outcome, price in zip(("home", "draw", "away"), odds, strict=True)
    )
    return validate_operator_quotes(
        inputs,
        registered_provider_ids=frozenset({"local-book"}),
        events=(
            OperatorEventReference(
                event_id,
                "football",
                NOW + timedelta(days=1),
            ),
        ),
        evaluated_at_utc=NOW,
    )


def test_no_offered_price_means_no_ev_and_no_proposal() -> None:
    distribution = joint_score_from_intensities(
        home_intensity=1.8,
        away_intensity=0.8,
        prediction_cutoff=date(2026, 1, 1),
    )
    home = find_market_probability(
        derive_full_time_markets(distribution),
        market_family="match-result",
        outcome_key="home",
    )
    decision = evaluate_proposed_single(
        canonical_event_id="event-1",
        market=home,
        quote=None,
        model_artifact_id="model-1",
        decision_as_of_utc=NOW,
    )
    assert decision.fair_decimal_odds is not None
    assert decision.offered_decimal_odds is None
    assert decision.expected_value is None
    assert not decision.accepted
    assert "quote-unavailable" in decision.reason_codes


def test_proposed_single_and_same_bookmaker_accumulator() -> None:
    distribution = joint_score_from_intensities(
        home_intensity=2.2,
        away_intensity=0.7,
        prediction_cutoff=date(2026, 1, 1),
    )
    home = find_market_probability(
        derive_full_time_markets(distribution),
        market_family="match-result",
        outcome_key="home",
    )
    policy = FootballOpportunityPolicy(
        minimum_edge=0.0,
        minimum_expected_value=0.0,
        safety_margin=0.0,
    )
    decisions = []
    for event_id in ("event-1", "event-2"):
        catalogue = _catalogue(event_id, ("2.20", "4.00", "5.00"))
        quote = next(item for item in catalogue.quotes if item.input.outcome_key == "home")
        decisions.append(
            evaluate_proposed_single(
                canonical_event_id=event_id,
                market=home,
                quote=quote,
                model_artifact_id=f"model-{event_id}",
                decision_as_of_utc=NOW,
                policy=policy,
            )
        )
    assert all(item.accepted for item in decisions)
    accumulators, rejections, statuses = build_same_bookmaker_accumulators(
        tuple(decisions),
        policy=policy,
    )
    assert len(accumulators) == 1
    assert accumulators[0].provider_id == "local-book"
    assert accumulators[0].total_offered_odds == Decimal("4.8400")
    assert not rejections
    assert statuses == (("football", "operational"),)


def test_same_event_conjunction_is_analytical_until_combined_price_exists() -> None:
    distribution = joint_score_from_intensities(
        home_intensity=1.7,
        away_intensity=1.0,
        prediction_cutoff=date(2026, 1, 1),
    )
    markets = derive_full_time_markets(distribution)
    home = find_market_probability(
        markets,
        market_family="match-result",
        outcome_key="home",
    )
    over = find_market_probability(
        markets,
        market_family="total-goals",
        outcome_key="over",
        line_value=Decimal("2.5"),
    )
    conjunction = analyse_same_event_conjunction(
        canonical_event_id="event-1",
        distribution=distribution,
        left=home,
        right=over,
    )
    assert not conjunction.placeable
    assert conjunction.offered_combined_odds is None
    assert conjunction.expected_value is None
    assert conjunction.conjunction_probability != pytest.approx(home.probability * over.probability)


def test_sport_policy_combines_or_partitions_without_silent_fallback() -> None:
    distribution = joint_score_from_intensities(
        home_intensity=1.8,
        away_intensity=0.9,
        prediction_cutoff=date(2026, 1, 1),
    )
    home = find_market_probability(
        derive_full_time_markets(distribution),
        market_family="match-result",
        outcome_key="home",
    )
    base_policy = FootballOpportunityPolicy(
        minimum_edge=0.0,
        minimum_expected_value=0.0,
        safety_margin=0.0,
    )
    decisions = []
    for index, sport in enumerate(("football", "football", "basketball", "basketball")):
        event_id = f"sport-event-{index}"
        quote = next(
            item
            for item in _catalogue(event_id, ("3.00", "2.00", "2.00")).quotes
            if item.input.outcome_key == "home"
        )
        decision = evaluate_proposed_single(
            canonical_event_id=event_id,
            market=home,
            quote=quote,
            model_artifact_id=f"model-{sport}",
            decision_as_of_utc=NOW,
            policy=base_policy,
        )
        decisions.append(replace(decision, sport_code=sport))

    combined, _, combined_statuses = build_same_bookmaker_accumulators(
        tuple(decisions),
        policy=replace(
            base_policy,
            sport_policy=ProposalSportPolicy(
                ("basketball", "football", "tennis"),
                SportCombinationMode.COMBINE_SELECTED_SPORTS,
            ),
        ),
    )
    assert any(len(item.sport_codes) == 2 for item in combined)
    assert combined_statuses[-1] == ("tennis", "sport-model-unavailable")

    separated, _, _ = build_same_bookmaker_accumulators(
        tuple(decisions),
        policy=replace(
            base_policy,
            sport_policy=ProposalSportPolicy(
                ("basketball", "football"),
                SportCombinationMode.SEPARATE_BY_SPORT,
            ),
        ),
    )
    assert separated
    assert all(len(item.sport_codes) == 1 for item in separated)
