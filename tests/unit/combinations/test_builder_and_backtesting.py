"""Focused combination dependency, timing, and settlement tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from sports_analytics.backtesting.contracts import (
    BacktestFold,
    BacktestMode,
    FoldBacktestInput,
    SettledOpportunity,
    SettlementResult,
    StrategyConfiguration,
)
from sports_analytics.backtesting.engine import (
    run_backtest,
    settle_combination_flat_unit,
    settle_single_flat_unit,
)
from sports_analytics.combinations.builder import BuilderBounds, build_combinations
from sports_analytics.combinations.contracts import (
    CombinationRules,
    DependencyClass,
    classify_dependency,
    validate_combination,
)
from sports_analytics.core.exceptions import BacktestError, CombinationError
from sports_analytics.markets.contracts import (
    LineType,
    MarketDefinition,
    MarketSelection,
    ParticipantScope,
)
from sports_analytics.opportunities.contracts import Opportunity, OpportunityFilter
from sports_analytics.predictions.contracts import CanonicalSelectionIdentity
from sports_analytics.value.contracts import QuoteEvaluationMode

START = datetime(2024, 3, 10, 15, tzinfo=UTC)


def _selection(*, market_key: str = "basketball.winner.moneyline.full-match", outcome="a"):
    return CanonicalSelectionIdentity.from_selection(
        MarketSelection(
            definition=MarketDefinition(
                sport_code="basketball",
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


def _opportunity(
    suffix: str,
    *,
    event_id: str,
    start: datetime,
    odds: str = "2.0",
    selection=None,
    mode=QuoteEvaluationMode.LIVE_SAFE,
    quoted: datetime | None = None,
) -> Opportunity:
    selection = selection or _selection()
    quoted = quoted if quoted is not None else start - timedelta(hours=2)
    return Opportunity(
        opportunity_id=f"opportunity-{suffix}",
        canonical_event_id=event_id,
        event_start_utc=start,
        selection=selection,
        prediction_id=f"prediction-{suffix}",
        predicted_at_utc=start - timedelta(hours=3),
        model_trained_through_date=date(2024, 3, 1),
        model_calibrated_through_date=date(2024, 3, 2),
        quote_observation_id=f"quote-{suffix}",
        quoted_at_utc=quoted,
        source_observed_at_utc=start - timedelta(hours=1),
        source_name="feed",
        provider_type="bookmaker",
        provider_id="book-a",
        evaluation_mode=mode,
        decimal_odds=Decimal(odds),
        model_probability=0.6,
        raw_implied_probability=0.5,
        normalized_implied_probability=0.5,
        overround=0.05,
        edge=0.1,
        expected_value=0.2,
        dependency_keys=frozenset({f"event:{event_id}"}),
        participant_ids=frozenset({f"participant:{event_id}"}),
        dependency_metadata_complete=True,
    )


def test_dependency_classifier_is_conservative_and_deterministic() -> None:
    first = _opportunity("1", event_id="event-1", start=START)
    separate = _opportunity("2", event_id="event-2", start=START + timedelta(days=1))
    conflict = _opportunity(
        "3",
        event_id="event-1",
        start=START,
        selection=_selection(outcome="b"),
    )
    unknown = _opportunity(
        "4",
        event_id="event-1",
        start=START,
        selection=_selection(market_key="basketball.totals.points.full-match"),
    )
    assert classify_dependency(first, separate).classification is (
        DependencyClass.STRUCTURALLY_SEPARATE
    )
    assert classify_dependency(first, conflict).classification is DependencyClass.CONFLICT
    assert classify_dependency(first, unknown).classification is DependencyClass.UNKNOWN
    assert classify_dependency(unknown, first) == classify_dependency(first, unknown)


def test_dependency_metadata_missing_or_shared_is_unknown_and_builder_rejects() -> None:
    first = _opportunity("1", event_id="event-1", start=START)
    missing = replace(
        _opportunity("2", event_id="event-2", start=START + timedelta(days=1)),
        dependency_keys=frozenset(),
        participant_ids=frozenset(),
        dependency_metadata_complete=False,
    )
    assert classify_dependency(first, missing).classification is DependencyClass.UNKNOWN
    shared = replace(
        _opportunity("3", event_id="event-3", start=START + timedelta(days=2)),
        dependency_keys=first.dependency_keys,
    )
    assert classify_dependency(first, shared).classification is DependencyClass.UNKNOWN
    build = build_combinations(
        (first, missing),
        rules=CombinationRules(allow_unknown_dependencies=True),
    )
    assert not build.combinations
    assert "unknown dependency rejected" in build.rejections[0].reason


def test_combination_enforces_per_leg_total_and_common_timing_separately() -> None:
    first = _opportunity("1", event_id="event-1", start=START, odds="2.0")
    second = _opportunity(
        "2",
        event_id="event-2",
        start=START + timedelta(days=1),
        odds="3.0",
    )
    second = replace(
        second,
        predicted_at_utc=START - timedelta(hours=3),
        quoted_at_utc=START - timedelta(hours=2),
        source_observed_at_utc=START - timedelta(hours=1),
    )
    rules = CombinationRules(
        selection_minimum_odds=Decimal("1.5"),
        selection_maximum_odds=Decimal("3.5"),
        combined_minimum_odds=Decimal("5.0"),
        combined_maximum_odds=Decimal("7.0"),
    )
    combination = validate_combination((second, first), rules=rules)
    assert combination.combined_decimal_odds == Decimal("6.00")
    assert combination.leg_count == 2
    assert combination.joint_probability == pytest.approx(0.36)
    assert combination.expected_value == pytest.approx(1.16)
    assert combination.earliest_event_start_utc == START
    late = replace(second, quoted_at_utc=START)
    with pytest.raises(CombinationError, match="strictly before"):
        validate_combination((first, late), rules=rules)
    with pytest.raises(CombinationError, match="combined_odds_range"):
        validate_combination(
            (first, second),
            rules=replace(rules, combined_maximum_odds=Decimal("5.5")),
        )


def test_cross_sport_multi_date_combination_requires_explicit_separation() -> None:
    basketball = _opportunity("1", event_id="event-1", start=START)
    football_selection = replace(
        _selection(market_key="football.winner.moneyline.full-match"),
        sport_code="football",
    )
    football = _opportunity(
        "2",
        event_id="event-2",
        start=START + timedelta(days=2),
        selection=football_selection,
    )
    football = replace(
        football,
        predicted_at_utc=START - timedelta(hours=3),
        quoted_at_utc=START - timedelta(hours=2),
        source_observed_at_utc=START - timedelta(hours=1),
    )
    combination = validate_combination(
        (basketball, football),
        rules=CombinationRules(allow_multiple_sports=True, allow_multiple_dates=True),
    )
    assert {leg.selection.sport_code for leg in combination.legs} == {
        "basketball",
        "football",
    }


def test_closing_benchmark_leg_is_never_a_production_combination() -> None:
    first = _opportunity("1", event_id="event-1", start=START)
    closing = _opportunity(
        "2",
        event_id="event-2",
        start=START + timedelta(days=1),
        mode=QuoteEvaluationMode.CLOSING_LINE_HISTORICAL_BENCHMARK,
        quoted=None,
    )
    closing = replace(closing, quoted_at_utc=None)
    with pytest.raises(CombinationError, match="refuse"):
        validate_combination((first, closing), rules=CombinationRules())


def test_builder_is_bounded_and_input_order_independent() -> None:
    opportunities = tuple(
        _opportunity(
            str(index),
            event_id=f"event-{index}",
            start=START + timedelta(days=index),
        )
        for index in range(5)
    )
    bounds = BuilderBounds(
        maximum_candidates=4,
        maximum_evaluated_combinations=3,
        maximum_results=2,
    )
    first = build_combinations(
        opportunities,
        rules=CombinationRules(minimum_legs=2, maximum_legs=3),
        bounds=bounds,
    )
    second = build_combinations(
        tuple(reversed(opportunities)),
        rules=CombinationRules(minimum_legs=2, maximum_legs=3),
        bounds=bounds,
    )
    assert first.truncated
    assert first.combinations_evaluated == 3
    assert [item.combination_id for item in first.combinations] == [
        item.combination_id for item in second.combinations
    ]


def test_flat_unit_settlement_and_required_metrics_are_pure() -> None:
    assert settle_single_flat_unit(
        decimal_odds=Decimal("2.5"),
        result=SettlementResult.WIN,
    ) == (Decimal("2.5"), Decimal("1.5"))
    with pytest.raises(BacktestError, match="rejects push"):
        settle_combination_flat_unit(
            legs=(
                (Decimal("2"), SettlementResult.WIN),
                (Decimal("3"), SettlementResult.PUSH),
            )
        )
    with pytest.raises(BacktestError, match="rejects push"):
        settle_single_flat_unit(
            decimal_odds=Decimal("2"),
            result=SettlementResult.PUSH,
        )
    loss = settle_combination_flat_unit(
        legs=(
            (Decimal("2"), SettlementResult.WIN),
            (Decimal("3"), SettlementResult.LOSS),
        )
    )
    assert loss[-1] == Decimal("-1")


def test_timestamped_synthetic_backtest_uses_fixed_strategy() -> None:
    first = _opportunity(
        "1",
        event_id="event-1",
        start=datetime(2024, 3, 10, 15, tzinfo=UTC),
    )
    second = _opportunity(
        "2",
        event_id="event-2",
        start=datetime(2024, 3, 11, 15, tzinfo=UTC),
    )
    second = replace(
        second,
        predicted_at_utc=first.predicted_at_utc,
        quoted_at_utc=first.quoted_at_utc,
        source_observed_at_utc=first.source_observed_at_utc,
    )
    fold = BacktestFold(
        fold_id="fold-001",
        train_start_date=date(2024, 1, 1),
        train_end_date=date(2024, 2, 1),
        calibration_start_date=date(2024, 2, 2),
        calibration_end_date=date(2024, 3, 2),
        test_start_date=date(2024, 3, 10),
        test_end_date=date(2024, 3, 11),
    )
    strategy = StrategyConfiguration(
        strategy_version="synthetic-v1",
        mode=BacktestMode.TIMESTAMPED_SYNTHETIC,
        opportunity_filter=OpportunityFilter(
            minimum_edge=-1,
            minimum_expected_value=-1,
        ),
        include_singles=True,
        include_combinations=True,
        combination_rules=CombinationRules(minimum_legs=2, maximum_legs=2),
    )
    result = run_backtest(
        (
            FoldBacktestInput(
                fold=fold,
                candidates=(
                    SettledOpportunity(first, SettlementResult.WIN),
                    SettledOpportunity(second, SettlementResult.LOSS),
                ),
            ),
        ),
        strategy=strategy,
    )
    assert result.metrics.bet_count == 3
    assert result.metrics.win_count == 1
    assert result.metrics.loss_count == 2
    assert result.metrics.net_profit_units == Decimal("-1.0")
    assert result.metrics.candidate_count == 2
    assert result.metrics.accepted_single_count == 2
    assert result.metrics.accepted_combination_count == 1
    assert result.metrics.gross_return_units == Decimal("2.0")
    assert len(result.metrics.cumulative_profit_units) == 3
    assert {item.dimension for item in result.metrics.aggregations} >= {
        "fold",
        "sport",
        "market",
        "provider",
        "ev_bucket",
    }
    assert [item.event_start_utc for item in result.bets] == sorted(
        item.event_start_utc for item in result.bets
    )


def test_backtest_reports_multiclass_scores_when_complete_distribution_exists() -> None:
    rows = tuple(
        replace(
            _opportunity(
                str(index),
                event_id="event-1",
                start=START,
                selection=_selection(outcome=outcome),
            ),
            prediction_id="prediction-complete",
            model_probability=probability,
        )
        for index, (outcome, probability) in enumerate(
            (("a", 0.6), ("b", 0.3), ("c", 0.1)),
            start=1,
        )
    )
    fold = BacktestFold(
        fold_id="fold-score",
        train_start_date=date(2024, 1, 1),
        train_end_date=date(2024, 2, 1),
        calibration_start_date=date(2024, 2, 2),
        calibration_end_date=date(2024, 3, 2),
        test_start_date=START.date(),
        test_end_date=START.date(),
    )
    result = run_backtest(
        (
            FoldBacktestInput(
                fold=fold,
                candidates=tuple(
                    SettledOpportunity(
                        row,
                        SettlementResult.WIN if index == 0 else SettlementResult.LOSS,
                    )
                    for index, row in enumerate(rows)
                ),
            ),
        ),
        strategy=StrategyConfiguration(
            strategy_version="scores-v1",
            mode=BacktestMode.TIMESTAMPED_SYNTHETIC,
            opportunity_filter=OpportunityFilter(
                minimum_edge=-1,
                minimum_expected_value=-1,
            ),
        ),
    )
    assert result.metrics.all_prediction_count == 1
    assert result.metrics.selected_prediction_count == 1
    assert result.metrics.all_log_loss == pytest.approx(0.5108256238)
    assert result.metrics.all_multiclass_brier_score == pytest.approx(0.26)


def test_closing_strategy_contract_refuses_combinations() -> None:
    with pytest.raises(BacktestError, match="closing-line accumulators"):
        StrategyConfiguration(
            strategy_version="bad-v1",
            mode=BacktestMode.CLOSING_LINE_HISTORICAL_BENCHMARK,
            opportunity_filter=OpportunityFilter(include_historical_benchmarks=True),
            include_singles=True,
            include_combinations=True,
            combination_rules=CombinationRules(),
        )
