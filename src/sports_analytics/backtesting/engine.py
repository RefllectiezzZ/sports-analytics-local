"""Deterministic rolling-origin selection and pure flat-unit settlement."""

from __future__ import annotations

import math
from collections import defaultdict
from decimal import Decimal
from typing import cast

from sports_analytics.backtesting.contracts import (
    BacktestMetricAggregation,
    BacktestMetrics,
    BacktestMode,
    BacktestResult,
    BetKind,
    FoldBacktestInput,
    SettledBet,
    SettledOpportunity,
    SettlementResult,
    StrategyConfiguration,
)
from sports_analytics.combinations.builder import (
    BuilderBounds,
    CombinationRejection,
    build_combinations,
)
from sports_analytics.core.exceptions import BacktestError
from sports_analytics.data.types import JsonValue
from sports_analytics.models.identity import content_addressed_id
from sports_analytics.opportunities.contracts import (
    Opportunity,
    OpportunityDecision,
    OpportunityRejection,
    filter_and_rank_opportunities,
)
from sports_analytics.value.contracts import QuoteEvaluationMode

FLAT_STAKE: Decimal = Decimal("1")


def settle_single_flat_unit(
    *,
    decimal_odds: Decimal,
    result: SettlementResult,
) -> tuple[Decimal, Decimal]:
    """Return ``(returned_units, profit_units)`` for a one-unit single."""
    if not decimal_odds.is_finite() or decimal_odds <= 1:
        raise BacktestError("settlement decimal odds must be finite and >1")
    if result is SettlementResult.WIN:
        returned = decimal_odds
    elif result is SettlementResult.LOSS:
        returned = Decimal("0")
    elif result in {SettlementResult.PUSH, SettlementResult.VOID}:
        raise BacktestError("v1 settlement rejects push and void results")
    else:
        raise BacktestError(f"unsupported settlement result: {result}")
    return returned, returned - FLAT_STAKE


def settle_combination_flat_unit(
    *,
    legs: tuple[tuple[Decimal, SettlementResult], ...],
) -> tuple[SettlementResult, Decimal, Decimal, Decimal]:
    """Settle a one-unit combination without stake sizing or operational state."""
    if not legs:
        raise BacktestError("combination settlement requires at least one leg")
    for odds, result in legs:
        if not odds.is_finite() or odds <= 1:
            raise BacktestError("combination leg odds must be finite and >1")
        if result in {SettlementResult.PUSH, SettlementResult.VOID}:
            raise BacktestError("v1 settlement rejects push and void results")
        if result not in {SettlementResult.WIN, SettlementResult.LOSS}:
            raise BacktestError(f"unsupported combination settlement: {result}")
    if any(result is SettlementResult.LOSS for _, result in legs):
        return SettlementResult.LOSS, Decimal("0"), Decimal("0"), Decimal("-1")
    active_odds = Decimal("1")
    for odds, result in legs:
        if result is SettlementResult.WIN:
            active_odds *= odds
    return SettlementResult.WIN, active_odds, active_odds, active_odds - Decimal("1")


def run_backtest(
    fold_inputs: tuple[FoldBacktestInput, ...],
    *,
    strategy: StrategyConfiguration,
    builder_bounds: BuilderBounds | None = None,
) -> BacktestResult:
    """Run one fixed strategy over strictly chronological untouched test folds."""
    if not fold_inputs:
        raise BacktestError("backtest requires at least one fold")
    ordered_inputs = tuple(
        sorted(fold_inputs, key=lambda item: (item.fold.test_start_date, item.fold.fold_id))
    )
    if len({item.fold.fold_id for item in ordered_inputs}) != len(ordered_inputs):
        raise BacktestError("backtest fold ids must be unique")
    for index in range(1, len(ordered_inputs)):
        if (
            ordered_inputs[index].fold.test_start_date
            <= ordered_inputs[index - 1].fold.test_start_date
        ):
            raise BacktestError("backtest folds must advance chronologically")
    _validate_non_overlapping_test_events(ordered_inputs)

    seen_opportunities: set[str] = set()
    bets: list[SettledBet] = []
    all_candidates: list[SettledOpportunity] = []
    accepted_opportunities: dict[str, Opportunity] = {}
    rejection_count = 0
    opportunity_decisions: list[OpportunityDecision] = []
    opportunity_rejections: list[OpportunityRejection] = []
    combination_rejections: list[CombinationRejection] = []
    for fold_input in ordered_inputs:
        fold = fold_input.fold
        settlements: dict[str, SettlementResult] = {}
        opportunities: list[Opportunity] = []
        for settled in fold_input.candidates:
            opportunity = settled.opportunity
            if opportunity.opportunity_id in seen_opportunities:
                raise BacktestError(
                    f"opportunity appears in multiple folds: {opportunity.opportunity_id}"
                )
            seen_opportunities.add(opportunity.opportunity_id)
            event_date = opportunity.event_start_utc.date()
            if not fold.test_start_date <= event_date <= fold.test_end_date:
                raise BacktestError("candidate event falls outside its fold test window")
            if opportunity.model_trained_through_date >= fold.test_start_date:
                raise BacktestError("candidate model training reaches the test window")
            if opportunity.model_calibrated_through_date >= fold.test_start_date:
                raise BacktestError("candidate model calibration reaches the test window")
            _validate_mode(opportunity, strategy.mode)
            settlements[opportunity.opportunity_id] = settled.result
            opportunities.append(opportunity)
            all_candidates.append(settled)

        search = filter_and_rank_opportunities(
            tuple(opportunities),
            filters=strategy.opportunity_filter,
        )
        rejection_count += len(search.rejected)
        opportunity_decisions.extend(search.decisions)
        opportunity_rejections.extend(search.rejected)
        accepted_opportunities.update((item.opportunity_id, item) for item in search.accepted)
        if strategy.include_singles:
            for opportunity in search.accepted:
                result = settlements[opportunity.opportunity_id]
                _returned, profit = settle_single_flat_unit(
                    decimal_odds=opportunity.decimal_odds,
                    result=result,
                )
                bets.append(
                    SettledBet(
                        bet_id=content_addressed_id(
                            identity_type="backtest-single-v1",
                            payload={
                                "strategy_id": strategy.strategy_id,
                                "fold_id": fold.fold_id,
                                "opportunity_id": opportunity.opportunity_id,
                            },
                        ),
                        fold_id=fold.fold_id,
                        kind=BetKind.SINGLE,
                        opportunity_ids=(opportunity.opportunity_id,),
                        decimal_odds=opportunity.decimal_odds,
                        result=result,
                        stake_units=FLAT_STAKE,
                        profit_units=profit,
                        event_start_utc=opportunity.event_start_utc,
                        sport_code=opportunity.selection.sport_code,
                        market_key=opportunity.selection.market_key,
                        provider_id=opportunity.provider_id,
                        model_probability=opportunity.model_probability,
                        edge=opportunity.edge,
                        expected_value=opportunity.expected_value,
                    )
                )
        if strategy.include_combinations:
            if strategy.mode is not BacktestMode.TIMESTAMPED_SYNTHETIC:
                raise BacktestError(
                    "production closing-line accumulators are explicitly unsupported"
                )
            rules = strategy.combination_rules
            if rules is None:
                raise BacktestError("combination strategy is missing rules")
            build = build_combinations(
                search.accepted,
                rules=rules,
                bounds=builder_bounds,
            )
            combination_rejections.extend(build.rejections)
            for combination in build.combinations:
                result, effective_odds, _returned, profit = settle_combination_flat_unit(
                    legs=tuple(
                        (leg.decimal_odds, settlements[leg.opportunity_id])
                        for leg in combination.legs
                    )
                )
                bets.append(
                    SettledBet(
                        bet_id=content_addressed_id(
                            identity_type="backtest-combination-v1",
                            payload={
                                "strategy_id": strategy.strategy_id,
                                "fold_id": fold.fold_id,
                                "combination_id": combination.combination_id,
                            },
                        ),
                        fold_id=fold.fold_id,
                        kind=BetKind.COMBINATION,
                        opportunity_ids=tuple(leg.opportunity_id for leg in combination.legs),
                        decimal_odds=effective_odds,
                        result=result,
                        stake_units=FLAT_STAKE,
                        profit_units=profit,
                        event_start_utc=combination.earliest_event_start_utc,
                        sport_code=_single_or_multiple(
                            tuple(leg.selection.sport_code for leg in combination.legs)
                        ),
                        market_key=_single_or_multiple(
                            tuple(leg.selection.market_key for leg in combination.legs)
                        ),
                        provider_id=_single_or_multiple(
                            tuple(leg.provider_id for leg in combination.legs)
                        ),
                        model_probability=combination.joint_probability,
                        edge=sum(leg.edge for leg in combination.legs) / combination.leg_count,
                        expected_value=combination.expected_value,
                    )
                )
    bets.sort(
        key=lambda item: (
            item.event_start_utc.isoformat() if item.event_start_utc is not None else "",
            item.bet_id,
        )
    )
    metrics = calculate_backtest_metrics(
        tuple(bets),
        candidates=tuple(all_candidates),
        selected_opportunity_ids=frozenset(accepted_opportunities),
        rejection_count=rejection_count,
        opportunity_rejections=tuple(opportunity_rejections),
        combination_rejections=tuple(combination_rejections),
    )
    decision_run_id = _derive_decision_run_id(
        strategy=strategy,
        ordered_inputs=ordered_inputs,
        candidates=tuple(all_candidates),
        opportunity_decisions=tuple(opportunity_decisions),
        opportunity_rejections=tuple(opportunity_rejections),
        combination_rejections=tuple(combination_rejections),
    )
    backtest_result_id = _derive_backtest_result_id(
        decision_run_id=decision_run_id,
        strategy=strategy,
        ordered_inputs=ordered_inputs,
        candidates=tuple(all_candidates),
        opportunity_decisions=tuple(opportunity_decisions),
        opportunity_rejections=tuple(opportunity_rejections),
        combination_rejections=tuple(combination_rejections),
        bets=tuple(bets),
        metrics=metrics,
    )
    disclaimer = (
        "Historical closing-line benchmark only; no pre-kickoff quote availability "
        "or production accumulator performance is claimed."
        if strategy.mode is BacktestMode.CLOSING_LINE_HISTORICAL_BENCHMARK
        else "Timestamped synthetic evaluation only; not operational settlement."
    )
    return BacktestResult(
        backtest_id=backtest_result_id,
        decision_run_id=decision_run_id,
        backtest_result_id=backtest_result_id,
        mode=strategy.mode,
        strategy_id=strategy.strategy_id,
        folds=tuple(item.fold for item in ordered_inputs),
        bets=tuple(bets),
        metrics=metrics,
        disclaimer=disclaimer,
        candidates=tuple(all_candidates),
        opportunity_decisions=tuple(opportunity_decisions),
        opportunity_rejections=tuple(opportunity_rejections),
        combination_rejections=tuple(combination_rejections),
    )


def calculate_backtest_metrics(
    bets: tuple[SettledBet, ...],
    *,
    candidates: tuple[SettledOpportunity, ...] = (),
    selected_opportunity_ids: frozenset[str] = frozenset(),
    rejection_count: int = 0,
    opportunity_rejections: tuple[OpportunityRejection, ...] = (),
    combination_rejections: tuple[CombinationRejection, ...] = (),
) -> BacktestMetrics:
    """Compute deterministic required metrics from immutable settled bets."""
    wins = sum(item.result is SettlementResult.WIN for item in bets)
    losses = sum(item.result is SettlementResult.LOSS for item in bets)
    pushes = sum(item.result is SettlementResult.PUSH for item in bets)
    voids = sum(item.result is SettlementResult.VOID for item in bets)
    stake = sum((item.stake_units for item in bets), start=Decimal("0"))
    net = sum((item.profit_units for item in bets), start=Decimal("0"))
    returned = stake + net
    decisions = wins + losses
    roi = float(net / stake) if stake else 0.0
    hit_rate = wins / decisions if decisions else 0.0
    average_odds = sum(float(item.decimal_odds) for item in bets) / len(bets) if bets else 0.0
    equity = Decimal("0")
    peak = Decimal("0")
    maximum_drawdown = Decimal("0")
    cumulative: list[Decimal] = []
    chronological_bets = tuple(
        sorted(
            bets,
            key=lambda item: (
                item.event_start_utc.isoformat() if item.event_start_utc is not None else "",
                item.bet_id,
            ),
        )
    )
    for item in chronological_bets:
        equity += item.profit_units
        cumulative.append(equity)
        peak = max(peak, equity)
        maximum_drawdown = max(maximum_drawdown, peak - equity)
    selected = [
        item.opportunity
        for item in candidates
        if item.opportunity.opportunity_id in selected_opportunity_ids
    ]
    score_all = _prediction_scores(candidates)
    score_selected = _prediction_scores(
        candidates,
        selected_opportunity_ids=selected_opportunity_ids,
    )
    return BacktestMetrics(
        bet_count=len(bets),
        settled_decision_count=decisions,
        win_count=wins,
        loss_count=losses,
        push_count=pushes,
        void_count=voids,
        staked_units=stake,
        returned_units=returned,
        net_profit_units=net,
        roi=roi,
        hit_rate=hit_rate,
        average_decimal_odds=average_odds,
        maximum_drawdown_units=maximum_drawdown,
        candidate_count=len(candidates),
        rejection_count=rejection_count,
        accepted_single_count=sum(item.kind is BetKind.SINGLE for item in bets),
        accepted_combination_count=sum(item.kind is BetKind.COMBINATION for item in bets),
        gross_return_units=returned,
        cumulative_profit_units=tuple(cumulative),
        average_model_probability=_average(tuple(item.model_probability for item in selected)),
        average_edge=_average(tuple(item.edge for item in selected)),
        average_expected_value=_average(tuple(item.expected_value for item in selected)),
        all_prediction_count=score_all[0],
        selected_prediction_count=score_selected[0],
        all_log_loss=score_all[1],
        all_multiclass_brier_score=score_all[2],
        selected_log_loss=score_selected[1],
        selected_multiclass_brier_score=score_selected[2],
        aggregations=_metric_aggregations(chronological_bets),
        rejection_counts_by_reason=_rejection_counts_by_reason(
            opportunity_rejections,
            combination_rejections,
        ),
    )


def _prediction_scores(
    candidates: tuple[SettledOpportunity, ...],
    *,
    selected_opportunity_ids: frozenset[str] | None = None,
) -> tuple[int, float | None, float | None]:
    by_prediction: dict[str, list[SettledOpportunity]] = defaultdict(list)
    for item in candidates:
        by_prediction[item.opportunity.prediction_id].append(item)
    losses: list[float] = []
    briers: list[float] = []
    for rows in by_prediction.values():
        if selected_opportunity_ids is not None and not any(
            row.opportunity.opportunity_id in selected_opportunity_ids for row in rows
        ):
            continue
        winners = [row for row in rows if row.result is SettlementResult.WIN]
        probability_total = math.fsum(row.opportunity.model_probability for row in rows)
        if (
            len(winners) != 1
            or len(rows) < 2
            or abs(probability_total - 1.0) > 1e-9
            or any(row.result not in {SettlementResult.WIN, SettlementResult.LOSS} for row in rows)
        ):
            continue
        true_probability = winners[0].opportunity.model_probability
        losses.append(-math.log(max(true_probability, 1e-15)))
        briers.append(
            sum(
                (
                    row.opportunity.model_probability
                    - (1.0 if row.result is SettlementResult.WIN else 0.0)
                )
                ** 2
                for row in rows
            )
        )
    count = len(losses)
    return (
        count,
        sum(losses) / count if count else None,
        sum(briers) / count if count else None,
    )


def _metric_aggregations(
    bets: tuple[SettledBet, ...],
) -> tuple[BacktestMetricAggregation, ...]:
    groups: dict[tuple[str, str], list[SettledBet]] = defaultdict(list)
    for bet in bets:
        values = {
            "fold": bet.fold_id,
            "sport": bet.sport_code or "unknown",
            "market": bet.market_key or "unknown",
            "provider": bet.provider_id or "unknown",
            "ev_bucket": _ev_bucket(bet.expected_value),
        }
        for dimension, key in values.items():
            groups[(dimension, key)].append(bet)
    return tuple(
        BacktestMetricAggregation(
            dimension=dimension,
            key=key,
            sample_size=len(rows),
            accepted_single_count=sum(item.kind is BetKind.SINGLE for item in rows),
            accepted_combination_count=sum(item.kind is BetKind.COMBINATION for item in rows),
            net_profit_units=sum(
                (item.profit_units for item in rows),
                start=Decimal("0"),
            ),
            average_model_probability=_average(tuple(item.model_probability for item in rows)),
            average_edge=_average(tuple(item.edge for item in rows)),
            average_expected_value=_average(tuple(item.expected_value for item in rows)),
        )
        for (dimension, key), rows in sorted(groups.items())
    )


def _average(values: tuple[float, ...]) -> float:
    return sum(values) / len(values) if values else 0.0


def _ev_bucket(value: float) -> str:
    if value < 0:
        return "negative"
    if value < 0.05:
        return "0.00-0.05"
    if value < 0.10:
        return "0.05-0.10"
    return "0.10+"


def _single_or_multiple(values: tuple[str, ...]) -> str:
    unique = set(values)
    return next(iter(unique)) if len(unique) == 1 else "multiple"


def _validate_non_overlapping_test_events(
    fold_inputs: tuple[FoldBacktestInput, ...],
) -> None:
    """Reject ambiguous overlapping test events used for aggregate betting metrics."""
    events_by_fold: dict[str, frozenset[str]] = {}
    for fold_input in fold_inputs:
        fold_events = frozenset(
            settled.opportunity.canonical_event_id for settled in fold_input.candidates
        )
        for other_events in events_by_fold.values():
            overlap = fold_events & other_events
            if overlap:
                raise BacktestError(f"overlapping test event across folds: {sorted(overlap)[0]}")
        events_by_fold[fold_input.fold.fold_id] = fold_events


def _fold_payload(fold_inputs: tuple[FoldBacktestInput, ...]) -> list[JsonValue]:
    return cast(
        list[JsonValue],
        [
            {
                "fold_id": item.fold.fold_id,
                "train_start_date": item.fold.train_start_date.isoformat(),
                "train_end_date": item.fold.train_end_date.isoformat(),
                "calibration_start_date": item.fold.calibration_start_date.isoformat(),
                "calibration_end_date": item.fold.calibration_end_date.isoformat(),
                "test_start_date": item.fold.test_start_date.isoformat(),
                "test_end_date": item.fold.test_end_date.isoformat(),
            }
            for item in fold_inputs
        ],
    )


def _derive_decision_run_id(
    *,
    strategy: StrategyConfiguration,
    ordered_inputs: tuple[FoldBacktestInput, ...],
    candidates: tuple[SettledOpportunity, ...],
    opportunity_decisions: tuple[OpportunityDecision, ...],
    opportunity_rejections: tuple[OpportunityRejection, ...],
    combination_rejections: tuple[CombinationRejection, ...],
) -> str:
    return content_addressed_id(
        identity_type="rolling-origin-backtest-decision-v1",
        payload={
            "mode": strategy.mode.value,
            "strategy_id": strategy.strategy_id,
            "folds": _fold_payload(ordered_inputs),
            "candidate_opportunity_ids": cast(
                list[JsonValue],
                sorted(item.opportunity.opportunity_id for item in candidates),
            ),
            "decisions": [
                {
                    "opportunity_id": item.opportunity_id,
                    "filter_config_id": item.filter_config_id,
                    "eligible": item.eligible,
                    "rejection_codes": [code.value for code in item.rejection_codes],
                    "accepted_rank": item.accepted_rank,
                }
                for item in sorted(opportunity_decisions, key=lambda row: row.opportunity_id)
            ],
            "opportunity_rejections": [
                {
                    "opportunity_id": item.opportunity.opportunity_id,
                    "codes": [code.value for code in item.codes],
                }
                for item in sorted(
                    opportunity_rejections,
                    key=lambda row: row.opportunity.opportunity_id,
                )
            ],
            "combination_rejections": [
                {
                    "opportunity_ids": list(item.opportunity_ids),
                    "reason": item.reason,
                }
                for item in combination_rejections
            ],
        },
    )


def _derive_backtest_result_id(
    *,
    decision_run_id: str,
    strategy: StrategyConfiguration,
    ordered_inputs: tuple[FoldBacktestInput, ...],
    candidates: tuple[SettledOpportunity, ...],
    opportunity_decisions: tuple[OpportunityDecision, ...],
    opportunity_rejections: tuple[OpportunityRejection, ...],
    combination_rejections: tuple[CombinationRejection, ...],
    bets: tuple[SettledBet, ...],
    metrics: BacktestMetrics,
) -> str:
    return content_addressed_id(
        identity_type="rolling-origin-backtest-result-v1",
        payload={
            "decision_run_id": decision_run_id,
            "mode": strategy.mode.value,
            "strategy_id": strategy.strategy_id,
            "folds": _fold_payload(ordered_inputs),
            "candidate_opportunity_ids": cast(
                list[JsonValue],
                sorted(item.opportunity.opportunity_id for item in candidates),
            ),
            "settlements": [
                {
                    "bet_id": item.bet_id,
                    "fold_id": item.fold_id,
                    "kind": item.kind.value,
                    "opportunity_ids": list(item.opportunity_ids),
                    "result": item.result.value,
                    "stake_units": format(item.stake_units, "f"),
                    "profit_units": format(item.profit_units, "f"),
                    "decimal_odds": format(item.decimal_odds, "f"),
                }
                for item in bets
            ],
            "decisions": [
                {
                    "opportunity_id": item.opportunity_id,
                    "filter_config_id": item.filter_config_id,
                    "eligible": item.eligible,
                    "rejection_codes": [code.value for code in item.rejection_codes],
                    "accepted_rank": item.accepted_rank,
                }
                for item in sorted(opportunity_decisions, key=lambda row: row.opportunity_id)
            ],
            "opportunity_rejections": [
                {
                    "opportunity_id": item.opportunity.opportunity_id,
                    "codes": [code.value for code in item.codes],
                }
                for item in sorted(
                    opportunity_rejections,
                    key=lambda row: row.opportunity.opportunity_id,
                )
            ],
            "combination_rejections": [
                {
                    "opportunity_ids": list(item.opportunity_ids),
                    "reason": item.reason,
                }
                for item in combination_rejections
            ],
            "metrics": {
                "bet_count": metrics.bet_count,
                "net_profit_units": format(metrics.net_profit_units, "f"),
                "gross_return_units": format(metrics.gross_return_units, "f"),
                "roi": metrics.roi,
                "rejection_count": metrics.rejection_count,
                "rejection_counts_by_reason": [
                    [reason, count, sample_size]
                    for reason, count, sample_size in metrics.rejection_counts_by_reason
                ],
            },
        },
    )


def _rejection_counts_by_reason(
    opportunity_rejections: tuple[OpportunityRejection, ...],
    combination_rejections: tuple[CombinationRejection, ...],
) -> tuple[tuple[str, int, int], ...]:
    counts: dict[str, int] = defaultdict(int)
    for opp_rejection in opportunity_rejections:
        for code in opp_rejection.codes:
            counts[code.value] += 1
    for combo_rejection in combination_rejections:
        key = _combination_rejection_code(combo_rejection.reason)
        counts[key] += 1
    return tuple((reason, count, count) for reason, count in sorted(counts.items()))


def _combination_rejection_code(reason: str) -> str:
    lowered = reason.casefold()
    if "unknown dependency" in lowered:
        return "unknown-dependency"
    if "conflicting legs" in lowered:
        return "combination-conflict"
    if "selection_odds_range" in lowered or "combined_odds_range" in lowered:
        return "combination-odds-rules"
    if "maximum_event_horizon" in lowered:
        return "combination-timing"
    if "closing-line" in lowered:
        return "combination-timing"
    return "combination-rules"


def _validate_mode(opportunity: Opportunity, mode: BacktestMode) -> None:
    if mode is BacktestMode.CLOSING_LINE_HISTORICAL_BENCHMARK:
        if opportunity.evaluation_mode is not QuoteEvaluationMode.CLOSING_LINE_HISTORICAL_BENCHMARK:
            raise BacktestError("closing benchmark fold contains a non-closing opportunity")
    elif opportunity.evaluation_mode is not QuoteEvaluationMode.LIVE_SAFE:
        raise BacktestError("timestamped synthetic fold requires live-safe quote timing")
    if mode is BacktestMode.TIMESTAMPED_SYNTHETIC and opportunity.quoted_at_utc is None:
        raise BacktestError("timestamped synthetic candidate lacks quoted_at_utc")
