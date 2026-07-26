"""Leakage-safe rolling-origin backtesting and pure settlement."""

from sports_analytics.backtesting.contracts import (
    BacktestFold,
    BacktestMode,
    BacktestResult,
    FoldBacktestInput,
    SettledOpportunity,
    SettlementResult,
    StrategyConfiguration,
)
from sports_analytics.backtesting.engine import (
    calculate_backtest_metrics,
    run_backtest,
    settle_combination_flat_unit,
    settle_single_flat_unit,
)

__all__ = [
    "BacktestFold",
    "BacktestMode",
    "BacktestResult",
    "FoldBacktestInput",
    "SettledOpportunity",
    "SettlementResult",
    "StrategyConfiguration",
    "calculate_backtest_metrics",
    "run_backtest",
    "settle_combination_flat_unit",
    "settle_single_flat_unit",
]
