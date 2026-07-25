"""Focused tests for prediction identity, quote value, and opportunities."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from sports_analytics.core.exceptions import (
    OpportunityError,
    PredictionError,
    ValueEvaluationError,
)
from sports_analytics.markets.contracts import (
    LineType,
    MarketDefinition,
    MarketSelection,
    ParticipantScope,
)
from sports_analytics.opportunities.contracts import (
    OpportunityFilter,
    OpportunityRankingMode,
    RejectionCode,
    filter_and_rank_opportunities,
    opportunities_from_evaluation,
)
from sports_analytics.predictions.contracts import (
    CanonicalSelectionIdentity,
    PredictionLineage,
    PredictionQualityFlags,
    SelectionProbability,
    build_market_prediction,
)
from sports_analytics.predictions.football import VerifiedFeatureRow
from sports_analytics.value.contracts import (
    CompleteMarketQuote,
    PricedSelection,
    QuoteEvaluationMode,
    evaluate_complete_market,
)

START = datetime(2024, 2, 10, 15, tzinfo=UTC)
PREDICTED = START - timedelta(hours=3)
QUOTED = START - timedelta(hours=2)


def _selection(outcome: str) -> CanonicalSelectionIdentity:
    return CanonicalSelectionIdentity.from_selection(
        MarketSelection(
            definition=MarketDefinition(
                sport_code="tennis",
                market_family="set-winner",
                market_key="tennis.set-winner.match.full-match",
                market_period="full-match",
                participant_scope=ParticipantScope.EVENT.value,
                line_type=LineType.NONE.value,
                line_value=None,
                canonical_participant_id=None,
            ),
            outcome_key=outcome,
        )
    )


def _prediction(
    *,
    event_id: str = "event-1",
    start: datetime = START,
    probabilities: tuple[float, float] = (0.6, 0.4),
):
    lineage = PredictionLineage(
        model_artifact_id="model-id",
        model_checksum_sha256="a" * 64,
        model_specification_version="tennis-logistic-v1",
        feature_artifact_id="feature-id",
        feature_manifest_checksum_sha256="b" * 64,
        feature_specification_version="tennis-features-v1",
        feature_row_id=event_id,
        trained_through_date=date(2024, 2, 1),
        calibrated_through_date=date(2024, 2, 2),
    )
    return build_market_prediction(
        canonical_event_id=event_id,
        event_start_utc=start,
        predicted_at_utc=start - timedelta(hours=3),
        feature_available_at_utc=start - timedelta(hours=4),
        lineage=lineage,
        probabilities=(
            SelectionProbability(_selection("player-a"), probabilities[0]),
            SelectionProbability(_selection("player-b"), probabilities[1]),
        ),
        quality=PredictionQualityFlags(
            calibrated=True,
            model_artifact_verified=True,
            feature_artifact_verified=True,
            sufficient_history=True,
            data_quality_passed=True,
        ),
    )


def _quote(prediction=None, *, odds=(Decimal("2.00"), Decimal("2.20")), closing=False):
    prediction = prediction or _prediction()
    prices = tuple(
        PricedSelection(
            selection=item.selection,
            decimal_odds=odds[index],
            quote_series_id=f"series-{index}",
            quote_observation_id=f"observation-{index}",
        )
        for index, item in enumerate(prediction.probabilities)
    )
    return CompleteMarketQuote(
        canonical_event_id=prediction.canonical_event_id,
        source_name="licensed-feed",
        provider_type="bookmaker",
        provider_id="provider-a",
        quote_phase="closing" if closing else "current",
        source_observed_at_utc=prediction.event_start_utc - timedelta(hours=1),
        quoted_at_utc=None if closing else prediction.event_start_utc - timedelta(hours=2),
        quote_timestamp_precision=("snapshot-observation-only" if closing else "exact"),
        quote_valid_from_utc=None,
        quote_valid_to_utc=None,
        selections=prices,
    )


def test_prediction_identity_is_order_independent_and_complete() -> None:
    first = _prediction()
    reversed_prediction = build_market_prediction(
        canonical_event_id=first.canonical_event_id,
        event_start_utc=first.event_start_utc,
        predicted_at_utc=first.predicted_at_utc,
        feature_available_at_utc=first.feature_available_at_utc,
        lineage=first.lineage,
        probabilities=tuple(reversed(first.probabilities)),
        quality=first.quality,
    )
    assert first.prediction_id == reversed_prediction.prediction_id
    assert len(first.probabilities) == 2
    assert first.probability_for(_selection("player-a")) == pytest.approx(0.6)


def test_prediction_rejects_incomplete_probability_mass() -> None:
    with pytest.raises(PredictionError, match="sum to one"):
        _prediction(probabilities=(0.6, 0.3))


@pytest.mark.parametrize(
    ("labels", "values"),
    [
        (("home", "draw", "away"), (0.5, 0.2, 0.3)),
        (("one", "two", "three", "four"), (0.1, 0.2, 0.3, 0.4)),
    ],
)
def test_prediction_preserves_declared_three_and_four_outcome_order(
    labels: tuple[str, ...],
    values: tuple[float, ...],
) -> None:
    base = _prediction()
    probabilities = tuple(
        SelectionProbability(_selection(label), value)
        for label, value in zip(labels, values, strict=True)
    )
    prediction = build_market_prediction(
        canonical_event_id=base.canonical_event_id,
        event_start_utc=base.event_start_utc,
        predicted_at_utc=base.predicted_at_utc,
        feature_available_at_utc=base.feature_available_at_utc,
        lineage=base.lineage,
        probabilities=tuple(reversed(probabilities)),
        ordered_selection_space=tuple(item.selection for item in probabilities),
    )
    assert [item.selection.outcome_key for item in prediction.probabilities] == list(labels)
    assert prediction.ordered_selection_ids == tuple(
        item.selection.selection_id for item in probabilities
    )


def test_production_feature_input_rejects_target_or_post_event_fields() -> None:
    with pytest.raises(PredictionError, match="forbidden target/post-event"):
        VerifiedFeatureRow(
            canonical_event_id="event",
            feature_artifact_id="feature",
            feature_manifest_checksum_sha256="a" * 64,
            feature_specification_version="spec",
            feature_names=("final_score",),
            feature_values=(1.0,),
            available_at_utc=PREDICTED,
        )


def test_prediction_rejects_future_features_and_same_day_model_history() -> None:
    prediction = _prediction()
    production_quality = PredictionQualityFlags(
        calibrated=True,
        model_artifact_verified=True,
        feature_artifact_verified=True,
        sufficient_history=True,
        data_quality_passed=True,
    )
    with pytest.raises(PredictionError, match="not available"):
        build_market_prediction(
            canonical_event_id=prediction.canonical_event_id,
            event_start_utc=prediction.event_start_utc,
            predicted_at_utc=prediction.predicted_at_utc,
            feature_available_at_utc=prediction.predicted_at_utc + timedelta(seconds=1),
            lineage=prediction.lineage,
            probabilities=prediction.probabilities,
            quality=production_quality,
        )
    bad_lineage = PredictionLineage(
        model_artifact_id="model-id",
        model_checksum_sha256="a" * 64,
        model_specification_version="tennis-logistic-v1",
        feature_artifact_id="feature-id",
        feature_manifest_checksum_sha256="b" * 64,
        feature_specification_version="tennis-features-v1",
        feature_row_id="event-1",
        trained_through_date=START.date(),
        calibrated_through_date=START.date(),
    )
    with pytest.raises(PredictionError, match="training history"):
        build_market_prediction(
            canonical_event_id="event-1",
            event_start_utc=START,
            predicted_at_utc=PREDICTED,
            feature_available_at_utc=PREDICTED,
            lineage=bad_lineage,
            probabilities=prediction.probabilities,
            quality=production_quality,
        )


def test_complete_market_value_exposes_raw_normalized_edge_and_ev() -> None:
    prediction = _prediction()
    evaluation = evaluate_complete_market(
        prediction=prediction,
        quote=_quote(prediction),
        mode=QuoteEvaluationMode.LIVE_SAFE,
    )
    assert evaluation.quote.source_name == "licensed-feed"
    assert evaluation.quote.provider_id == "provider-a"
    assert evaluation.overround == pytest.approx((1 / 2.0) + (1 / 2.2) - 1)
    assert sum(item.normalized_implied_probability for item in evaluation.selections) == (
        pytest.approx(1.0)
    )
    by_outcome = {item.selection.outcome_key: item for item in evaluation.selections}
    assert by_outcome["player-a"].expected_value == pytest.approx(0.2)
    assert by_outcome["player-a"].raw_implied_probability == pytest.approx(0.5)


def test_quote_requires_exact_complete_outcome_set() -> None:
    prediction = _prediction()
    quote = _quote(prediction)
    incomplete = CompleteMarketQuote(
        canonical_event_id=quote.canonical_event_id,
        source_name=quote.source_name,
        provider_type=quote.provider_type,
        provider_id=quote.provider_id,
        quote_phase=quote.quote_phase,
        source_observed_at_utc=quote.source_observed_at_utc,
        quoted_at_utc=quote.quoted_at_utc,
        quote_timestamp_precision=quote.quote_timestamp_precision,
        quote_valid_from_utc=None,
        quote_valid_to_utc=None,
        selections=quote.selections[:1],
    )
    with pytest.raises(ValueEvaluationError, match="exactly cover"):
        evaluate_complete_market(
            prediction=prediction,
            quote=incomplete,
            mode=QuoteEvaluationMode.LIVE_SAFE,
        )


def test_live_quote_observed_at_or_after_start_is_rejected() -> None:
    prediction = _prediction()
    quote = _quote(prediction)
    late = CompleteMarketQuote(
        canonical_event_id=quote.canonical_event_id,
        source_name=quote.source_name,
        provider_type=quote.provider_type,
        provider_id=quote.provider_id,
        quote_phase=quote.quote_phase,
        source_observed_at_utc=quote.source_observed_at_utc,
        quoted_at_utc=prediction.event_start_utc,
        quote_timestamp_precision=quote.quote_timestamp_precision,
        quote_valid_from_utc=None,
        quote_valid_to_utc=None,
        selections=quote.selections,
    )
    with pytest.raises(ValueEvaluationError, match="strictly before"):
        evaluate_complete_market(
            prediction=prediction,
            quote=late,
            mode=QuoteEvaluationMode.LIVE_SAFE,
        )


def test_closing_line_is_benchmark_only_and_not_live_safe() -> None:
    prediction = _prediction()
    quote = _quote(prediction, closing=True)
    with pytest.raises(ValueEvaluationError, match="closing quotes"):
        evaluate_complete_market(
            prediction=prediction,
            quote=quote,
            mode=QuoteEvaluationMode.LIVE_SAFE,
        )
    benchmark = evaluate_complete_market(
        prediction=prediction,
        quote=quote,
        mode=QuoteEvaluationMode.CLOSING_LINE_HISTORICAL_BENCHMARK,
    )
    assert not benchmark.is_actionable
    assert "benchmark only" in str(benchmark.warning).lower()


def test_filter_rejections_are_audited_and_ranking_is_deterministic() -> None:
    prediction = _prediction()
    evaluation = evaluate_complete_market(
        prediction=prediction,
        quote=_quote(prediction),
        mode=QuoteEvaluationMode.LIVE_SAFE,
    )
    opportunities = opportunities_from_evaluation(evaluation)
    result = filter_and_rank_opportunities(
        tuple(reversed(opportunities)),
        filters=OpportunityFilter(
            minimum_probability=0.5,
            minimum_edge=0.0,
            minimum_expected_value=0.0,
            provider_ids=frozenset({"provider-a"}),
        ),
    )
    assert [item.selection.outcome_key for item in result.accepted] == ["player-a"]
    assert result.rejected[0].codes == (
        RejectionCode.PROBABILITY,
        RejectionCode.EDGE,
        RejectionCode.EXPECTED_VALUE,
    )
    again = filter_and_rank_opportunities(
        opportunities,
        filters=OpportunityFilter(minimum_expected_value=-1.0, minimum_edge=-1.0),
    )
    assert [item.opportunity_id for item in again.accepted] == [
        item.opportunity_id
        for item in sorted(
            again.accepted,
            key=lambda value: (
                -value.expected_value,
                -value.model_probability,
                value.canonical_event_id,
                value.selection.selection_id,
            ),
        )
    ]
    capped = filter_and_rank_opportunities(
        opportunities,
        filters=OpportunityFilter(
            minimum_expected_value=-1.0,
            minimum_edge=-1.0,
            ranking_mode=OpportunityRankingMode.MODEL_PROBABILITY,
            max_accepted_count=1,
        ),
    )
    assert len(capped.accepted) == 1
    assert capped.accepted[0].model_probability == 0.6
    assert capped.rejected[-1].codes == (RejectionCode.RANK_CAP,)
    assert capped.decisions[0].filter_config_id


def test_filter_rejects_invalid_separate_odds_range() -> None:
    with pytest.raises(OpportunityError, match="odds range"):
        OpportunityFilter(
            selection_minimum_odds=Decimal("3"),
            selection_maximum_odds=Decimal("2"),
        )
