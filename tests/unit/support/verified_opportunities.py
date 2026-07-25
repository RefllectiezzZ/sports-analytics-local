"""Shared helpers for constructing verified opportunities in unit tests."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sports_analytics.markets.contracts import (
    LineType,
    MarketDefinition,
    MarketSelection,
    ParticipantScope,
)
from sports_analytics.opportunities.contracts import Opportunity, opportunities_from_evaluation
from sports_analytics.predictions.contracts import (
    CanonicalSelectionIdentity,
    PredictionLineage,
    PredictionQualityFlags,
    SelectionProbability,
    build_market_prediction,
)
from sports_analytics.value.contracts import (
    CompleteMarketQuote,
    PricedSelection,
    QuoteEvaluationMode,
    evaluate_complete_market,
)

DEFAULT_START = datetime(2024, 3, 10, 15, tzinfo=UTC)


def basketball_selection(
    *,
    market_key: str = "basketball.winner.moneyline.full-match",
    outcome: str = "a",
    sport_code: str = "basketball",
) -> CanonicalSelectionIdentity:
    return CanonicalSelectionIdentity.from_selection(
        MarketSelection(
            definition=MarketDefinition(
                sport_code=sport_code,
                market_family="winner",
                market_key=market_key,
                market_period="full-match",
                participant_scope=ParticipantScope.EVENT.value,
                line_type=LineType.NONE.value,
                line_value=None,
                canonical_participant_id=None,
            ),
            outcome_key=outcome,
        )
    )


def build_test_opportunity(
    suffix: str,
    *,
    event_id: str,
    start: datetime = DEFAULT_START,
    odds: str = "2.0",
    model_probability: float = 0.6,
    selection: CanonicalSelectionIdentity | None = None,
    mode: QuoteEvaluationMode = QuoteEvaluationMode.LIVE_SAFE,
    quoted: datetime | None = None,
    predicted_at_utc: datetime | None = None,
    source_observed_at_utc: datetime | None = None,
    opponent_odds: str = "2.0",
    opponent_probability: float | None = None,
    dependency_keys: frozenset[str] | None = None,
    participant_ids: frozenset[str] | None = None,
    dependency_metadata_complete: bool = True,
) -> Opportunity:
    """Build one verified opportunity through the canonical evaluation pipeline."""
    selection = selection or basketball_selection()
    opponent_outcome = "b" if selection.outcome_key != "b" else "c"
    opponent = basketball_selection(
        outcome=opponent_outcome,
        sport_code=selection.sport_code,
        market_key=selection.market_key,
    )
    quoted = quoted if quoted is not None else start - timedelta(hours=2)
    predicted_at = predicted_at_utc if predicted_at_utc is not None else start - timedelta(hours=3)
    observed_at = (
        source_observed_at_utc if source_observed_at_utc is not None else start - timedelta(hours=1)
    )
    opponent_probability = (
        1.0 - model_probability if opponent_probability is None else opponent_probability
    )
    lineage = PredictionLineage(
        model_artifact_id=f"model-{suffix}",
        model_checksum_sha256="a" * 64,
        model_specification_version="model-v1",
        feature_artifact_id=f"feature-{suffix}",
        feature_manifest_checksum_sha256="b" * 64,
        feature_specification_version="feature-v1",
        feature_row_id=event_id,
        trained_through_date=date(2024, 3, 1),
        calibrated_through_date=date(2024, 3, 2),
    )
    prediction = build_market_prediction(
        canonical_event_id=event_id,
        event_start_utc=start,
        predicted_at_utc=predicted_at,
        feature_available_at_utc=predicted_at - timedelta(hours=1),
        lineage=lineage,
        probabilities=(
            SelectionProbability(selection, model_probability),
            SelectionProbability(opponent, opponent_probability),
        ),
        quality=PredictionQualityFlags(
            calibrated=True,
            model_artifact_verified=True,
            feature_artifact_verified=True,
            sufficient_history=True,
            data_quality_passed=True,
        ),
    )
    quote = CompleteMarketQuote(
        canonical_event_id=event_id,
        source_name="feed",
        provider_type="bookmaker",
        provider_id="book-a",
        quote_phase="current" if mode is QuoteEvaluationMode.LIVE_SAFE else "closing",
        source_observed_at_utc=observed_at,
        quoted_at_utc=(
            None if mode is QuoteEvaluationMode.CLOSING_LINE_HISTORICAL_BENCHMARK else quoted
        ),
        quote_timestamp_precision=(
            "snapshot-observation-only"
            if mode is QuoteEvaluationMode.CLOSING_LINE_HISTORICAL_BENCHMARK
            else "exact"
        ),
        quote_valid_from_utc=None,
        quote_valid_to_utc=None,
        selections=(
            PricedSelection(
                selection=selection,
                decimal_odds=Decimal(odds),
                quote_series_id=f"series-{suffix}",
                quote_observation_id=f"quote-{suffix}",
            ),
            PricedSelection(
                selection=opponent,
                decimal_odds=Decimal(opponent_odds),
                quote_series_id=f"series-{suffix}-b",
                quote_observation_id=f"quote-{suffix}-b",
            ),
        ),
    )
    evaluation = evaluate_complete_market(prediction=prediction, quote=quote, mode=mode)
    opportunity = next(
        item
        for item in opportunities_from_evaluation(evaluation)
        if item.selection.selection_id == selection.selection_id
    )
    needs_metadata_override = (
        dependency_keys is not None
        or participant_ids is not None
        or not dependency_metadata_complete
    )
    if needs_metadata_override:
        from dataclasses import replace

        return replace(
            opportunity,
            dependency_keys=dependency_keys or opportunity.dependency_keys,
            participant_ids=participant_ids or opportunity.participant_ids,
            dependency_metadata_complete=dependency_metadata_complete,
        )
    from dataclasses import replace

    return replace(
        opportunity,
        dependency_keys=frozenset({f"event:{event_id}"}),
        participant_ids=frozenset({f"participant:{event_id}"}),
        dependency_metadata_complete=True,
    )


def build_three_outcome_opportunities(
    *,
    event_id: str = "event-1",
    start: datetime = DEFAULT_START,
    probabilities: tuple[float, float, float] = (0.6, 0.3, 0.1),
    prediction_id: str = "prediction-complete",
) -> tuple[Opportunity, Opportunity, Opportunity]:
    """Build three verified opportunities sharing one complete three-outcome prediction."""
    outcomes = ("a", "b", "c")
    selections = tuple(basketball_selection(outcome=outcome) for outcome in outcomes)
    lineage = PredictionLineage(
        model_artifact_id="model-multiclass",
        model_checksum_sha256="a" * 64,
        model_specification_version="model-v1",
        feature_artifact_id="feature-multiclass",
        feature_manifest_checksum_sha256="b" * 64,
        feature_specification_version="feature-v1",
        feature_row_id=event_id,
        trained_through_date=date(2024, 3, 1),
        calibrated_through_date=date(2024, 3, 2),
    )
    prediction = build_market_prediction(
        canonical_event_id=event_id,
        event_start_utc=start,
        predicted_at_utc=start - timedelta(hours=3),
        feature_available_at_utc=start - timedelta(hours=4),
        lineage=lineage,
        probabilities=tuple(
            SelectionProbability(selection, probability)
            for selection, probability in zip(selections, probabilities, strict=True)
        ),
        quality=PredictionQualityFlags(
            calibrated=True,
            model_artifact_verified=True,
            feature_artifact_verified=True,
            sufficient_history=True,
            data_quality_passed=True,
        ),
    )
    odds = (Decimal("2.0"), Decimal("3.0"), Decimal("10.0"))
    quote = CompleteMarketQuote(
        canonical_event_id=event_id,
        source_name="feed",
        provider_type="bookmaker",
        provider_id="book-a",
        quote_phase="current",
        source_observed_at_utc=start - timedelta(hours=1),
        quoted_at_utc=start - timedelta(hours=2),
        quote_timestamp_precision="exact",
        quote_valid_from_utc=None,
        quote_valid_to_utc=None,
        selections=tuple(
            PricedSelection(
                selection=selection,
                decimal_odds=odd,
                quote_series_id=f"series-{index}",
                quote_observation_id=f"quote-{index}",
            )
            for index, (selection, odd) in enumerate(zip(selections, odds, strict=True))
        ),
    )
    evaluation = evaluate_complete_market(
        prediction=prediction,
        quote=quote,
        mode=QuoteEvaluationMode.LIVE_SAFE,
    )
    opportunities = opportunities_from_evaluation(evaluation)
    if len(opportunities) != 3:
        raise RuntimeError("expected three opportunities from complete evaluation")
    return (opportunities[0], opportunities[1], opportunities[2])
