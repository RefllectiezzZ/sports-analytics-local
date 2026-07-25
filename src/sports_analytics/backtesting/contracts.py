"""Rolling-origin backtesting, settlement, and metric contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, cast

from sports_analytics.combinations.contracts import CombinationRules
from sports_analytics.core.exceptions import BacktestError
from sports_analytics.data.types import JsonValue
from sports_analytics.models.identity import content_addressed_id
from sports_analytics.opportunities.contracts import (
    Opportunity,
    OpportunityDecision,
    OpportunityFilter,
    OpportunityRejection,
)

if TYPE_CHECKING:
    from sports_analytics.combinations.builder import CombinationRejection


class BacktestMode(StrEnum):
    """Supported evidence modes with intentionally different claims."""

    CLOSING_LINE_HISTORICAL_BENCHMARK = "closing-line-historical-benchmark"
    TIMESTAMPED_SYNTHETIC = "timestamped-synthetic"


class SettlementResult(StrEnum):
    """Pure selection settlement result."""

    WIN = "win"
    LOSS = "loss"
    PUSH = "push"
    VOID = "void"


class BetKind(StrEnum):
    SINGLE = "single"
    COMBINATION = "combination"


@dataclass(frozen=True, slots=True)
class BacktestFold:
    """Strictly ordered train/calibration/test date windows."""

    fold_id: str
    train_start_date: date
    train_end_date: date
    calibration_start_date: date
    calibration_end_date: date
    test_start_date: date
    test_end_date: date

    def __post_init__(self) -> None:
        if not self.fold_id:
            raise BacktestError("fold_id must be non-empty")
        if not (
            self.train_start_date
            <= self.train_end_date
            < self.calibration_start_date
            <= self.calibration_end_date
            < self.test_start_date
            <= self.test_end_date
        ):
            raise BacktestError("fold windows must be disjoint and chronologically ordered")


@dataclass(frozen=True, slots=True)
class StrategyConfiguration:
    """One fixed selection/build policy reused unchanged across all folds."""

    strategy_version: str
    mode: BacktestMode
    opportunity_filter: OpportunityFilter
    include_singles: bool = True
    include_combinations: bool = False
    combination_rules: CombinationRules | None = None

    def __post_init__(self) -> None:
        if not self.strategy_version:
            raise BacktestError("strategy_version must be non-empty")
        if not self.include_singles and not self.include_combinations:
            raise BacktestError("strategy must enable singles or combinations")
        if self.include_combinations and self.combination_rules is None:
            raise BacktestError("combination strategy requires combination_rules")
        if (
            self.mode is BacktestMode.CLOSING_LINE_HISTORICAL_BENCHMARK
            and self.include_combinations
        ):
            raise BacktestError("production closing-line accumulators are explicitly unsupported")

    @property
    def strategy_id(self) -> str:
        filters = self.opportunity_filter
        rules = self.combination_rules
        filter_payload: dict[str, JsonValue] = {
            "filter_config_id": filters.filter_config_id,
            "filter_version": filters.filter_version,
            "minimum_probability": filters.minimum_probability,
            "minimum_edge": filters.minimum_edge,
            "minimum_expected_value": filters.minimum_expected_value,
            "selection_minimum_odds": format(filters.selection_minimum_odds, "f"),
            "selection_maximum_odds": format(filters.selection_maximum_odds, "f"),
            "sport_codes": cast(list[JsonValue], sorted(filters.sport_codes)),
            "market_keys": cast(list[JsonValue], sorted(filters.market_keys)),
            "provider_ids": cast(list[JsonValue], sorted(filters.provider_ids)),
            "include_historical_benchmarks": filters.include_historical_benchmarks,
            "ranking_mode": filters.ranking_mode.value,
            "max_accepted_count": filters.max_accepted_count,
            "starts_at_or_after_utc": (
                None
                if filters.starts_at_or_after_utc is None
                else filters.starts_at_or_after_utc.isoformat()
            ),
            "starts_before_utc": (
                None if filters.starts_before_utc is None else filters.starts_before_utc.isoformat()
            ),
        }
        rules_payload: dict[str, JsonValue] | None = None
        if rules is not None:
            rules_payload = {
                "policy_id": rules.policy_id,
                "policy_version": rules.policy_version,
                "minimum_legs": rules.minimum_legs,
                "maximum_legs": rules.maximum_legs,
                "selection_minimum_odds": format(rules.selection_minimum_odds, "f"),
                "selection_maximum_odds": format(rules.selection_maximum_odds, "f"),
                "combined_minimum_odds": format(rules.combined_minimum_odds, "f"),
                "combined_maximum_odds": format(rules.combined_maximum_odds, "f"),
                "allow_unknown_dependencies": rules.allow_unknown_dependencies,
                "allowed_sport_codes": cast(list[JsonValue], sorted(rules.allowed_sport_codes)),
                "allowed_market_keys": cast(list[JsonValue], sorted(rules.allowed_market_keys)),
                "minimum_joint_probability": rules.minimum_joint_probability,
                "minimum_expected_value": rules.minimum_expected_value,
                "maximum_candidates": rules.maximum_candidates,
                "maximum_evaluated_combinations": rules.maximum_evaluated_combinations,
                "maximum_outputs": rules.maximum_outputs,
                "maximum_event_horizon_microseconds": (
                    rules.maximum_event_horizon.days * 86_400_000_000
                    + rules.maximum_event_horizon.seconds * 1_000_000
                    + rules.maximum_event_horizon.microseconds
                ),
                "allow_multiple_sports": rules.allow_multiple_sports,
                "allow_multiple_dates": rules.allow_multiple_dates,
            }
        return content_addressed_id(
            identity_type="backtest-strategy-v1",
            payload={
                "strategy_version": self.strategy_version,
                "mode": self.mode.value,
                "include_singles": self.include_singles,
                "include_combinations": self.include_combinations,
                "filters": filter_payload,
                "combination_rules": rules_payload,
            },
        )


@dataclass(frozen=True, slots=True)
class BacktestLineage:
    """Optional feature and snapshot lineage included in backtest result identity."""

    feature_artifact_id: str | None = None
    feature_manifest_checksum_sha256: str | None = None
    input_snapshots: tuple[dict[str, JsonValue], ...] = ()


@dataclass(frozen=True, slots=True)
class SettledOpportunity:
    """A historical/synthetic opportunity with an explicit pure settlement."""

    opportunity: Opportunity
    result: SettlementResult


@dataclass(frozen=True, slots=True)
class FoldBacktestInput:
    """All candidates and outcomes exposed to one untouched test fold."""

    fold: BacktestFold
    candidates: tuple[SettledOpportunity, ...]
    fold_model_id: str | None = None
    fold_model_checksum_sha256: str | None = None
    calibration_temperature: float | None = None
    random_seed: int | None = None


@dataclass(frozen=True, slots=True)
class SettledBet:
    """One flat-unit settled single or combination."""

    bet_id: str
    fold_id: str
    kind: BetKind
    opportunity_ids: tuple[str, ...]
    decimal_odds: Decimal
    result: SettlementResult
    stake_units: Decimal
    profit_units: Decimal
    event_start_utc: datetime | None = None
    sport_code: str = ""
    market_key: str = ""
    provider_id: str = ""
    model_probability: float = 0.0
    edge: float = 0.0
    expected_value: float = 0.0


@dataclass(frozen=True, slots=True)
class BacktestMetricAggregation:
    """One deterministic metric slice with an explicit sample size."""

    dimension: str
    key: str
    sample_size: int
    accepted_single_count: int
    accepted_combination_count: int
    net_profit_units: Decimal
    average_model_probability: float
    average_edge: float
    average_expected_value: float


@dataclass(frozen=True, slots=True)
class BacktestMetrics:
    """Required flat-unit performance and risk summary."""

    bet_count: int
    settled_decision_count: int
    win_count: int
    loss_count: int
    push_count: int
    void_count: int
    staked_units: Decimal
    returned_units: Decimal
    net_profit_units: Decimal
    roi: float
    hit_rate: float
    average_decimal_odds: float
    maximum_drawdown_units: Decimal
    candidate_count: int = 0
    rejection_count: int = 0
    accepted_single_count: int = 0
    accepted_combination_count: int = 0
    gross_return_units: Decimal = Decimal("0")
    cumulative_profit_units: tuple[Decimal, ...] = ()
    average_model_probability: float = 0.0
    average_edge: float = 0.0
    average_expected_value: float = 0.0
    all_prediction_count: int = 0
    selected_prediction_count: int = 0
    all_log_loss: float | None = None
    all_multiclass_brier_score: float | None = None
    selected_log_loss: float | None = None
    selected_multiclass_brier_score: float | None = None
    aggregations: tuple[BacktestMetricAggregation, ...] = ()
    rejection_counts_by_reason: tuple[tuple[str, int, int], ...] = ()


@dataclass(frozen=True, slots=True)
class BacktestResult:
    """Deterministic folds, settled bets, and aggregate metrics."""

    backtest_id: str
    decision_run_id: str
    backtest_result_id: str
    mode: BacktestMode
    strategy_id: str
    folds: tuple[BacktestFold, ...]
    bets: tuple[SettledBet, ...]
    metrics: BacktestMetrics
    disclaimer: str
    candidates: tuple[SettledOpportunity, ...] = ()
    opportunity_decisions: tuple[OpportunityDecision, ...] = ()
    opportunity_rejections: tuple[OpportunityRejection, ...] = ()
    combination_rejections: tuple[CombinationRejection, ...] = ()
