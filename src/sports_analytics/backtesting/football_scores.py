"""Historical-price backtest for coherent score-model 1X2 probabilities."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Final

from sports_analytics.artifacts import (
    AnalyticalArtifact,
    load_analytical_artifact,
    write_analytical_artifact,
)
from sports_analytics.core.exceptions import ArtifactError, BacktestError
from sports_analytics.data.types import JsonValue

SCORE_BACKTEST_TYPE: Final[str] = "football-score-historical-price-backtest"
SCORE_BACKTEST_SCHEMA: Final[str] = "football-score-historical-price-backtest-v1"
HISTORICAL_CLOSING: Final[str] = "historical-closing-benchmark"


@dataclass(frozen=True, slots=True)
class Historical1x2Evaluation:
    canonical_event_id: str
    competition_id: str
    season_id: str
    provider_id: str
    outcome_key: str
    observed_outcome: str
    model_probability: float
    normalized_market_probability: float
    offered_decimal_odds: Decimal
    quote_classification: str = HISTORICAL_CLOSING

    def __post_init__(self) -> None:
        if self.outcome_key not in {"home", "draw", "away"} or self.observed_outcome not in {
            "home",
            "draw",
            "away",
        }:
            raise BacktestError("historical 1X2 outcome is invalid")
        if self.quote_classification != HISTORICAL_CLOSING:
            raise BacktestError("score backtest accepts only historical closing benchmarks")
        if (
            not 0.0 < self.model_probability < 1.0
            or not 0.0 < self.normalized_market_probability < 1.0
        ):
            raise BacktestError("historical probabilities must lie in (0, 1)")
        if not self.offered_decimal_odds.is_finite() or self.offered_decimal_odds <= 1:
            raise BacktestError("historical offered odds must be finite and greater than one")


@dataclass(frozen=True, slots=True)
class HistoricalScoreBacktestPolicy:
    minimum_edge: float = 0.02
    minimum_expected_value: float = 0.03


@dataclass(frozen=True, slots=True)
class HistoricalScoreBacktest:
    tested_events: int
    quote_rows: int
    complete_markets: int
    quote_coverage: float
    accepted_selections: int
    rejected_selections: int
    turnover: float
    profit_and_loss: float
    roi: float | None
    maximum_drawdown: float
    rejection_reasons: tuple[tuple[str, int], ...]
    odds_buckets: tuple[tuple[str, int, float, float | None], ...]
    edge_buckets: tuple[tuple[str, int, float, float | None], ...]
    calibration_by_price_bucket: tuple[tuple[str, int, float, float], ...]
    competitions: tuple[str, ...]
    seasons: tuple[str, ...]
    model: str
    market_baseline: str
    quote_classification: str
    limitations: tuple[str, ...]


def run_historical_score_backtest(
    rows: tuple[Historical1x2Evaluation, ...],
    *,
    policy: HistoricalScoreBacktestPolicy | None = None,
    model: str = "unspecified-score-model",
) -> HistoricalScoreBacktest:
    if not rows:
        raise BacktestError("historical score backtest requires priced rows")
    rules = policy or HistoricalScoreBacktestPolicy()
    # A 1X2 market settles exactly one outcome.  Historical rows are evaluated
    # independently for calibration, but the normal singles strategy must not
    # stake mutually-exclusive home/draw/away selections from the same quoted
    # market.  Select the deterministic best candidate per event/provider.
    accepted: list[Historical1x2Evaluation] = []
    rejected: dict[str, int] = {}
    qualifying: dict[tuple[str, str], list[Historical1x2Evaluation]] = {}
    for row in sorted(
        rows, key=lambda item: (item.canonical_event_id, item.provider_id, item.outcome_key)
    ):
        edge = row.model_probability - row.normalized_market_probability
        expected_value = row.model_probability * float(row.offered_decimal_odds) - 1.0
        reasons: list[str] = []
        if edge < rules.minimum_edge:
            reasons.append("edge-insufficient")
        if expected_value < rules.minimum_expected_value:
            reasons.append("conservative-ev-insufficient")
        if reasons:
            for reason in reasons:
                rejected[reason] = rejected.get(reason, 0) + 1
        else:
            qualifying.setdefault((row.canonical_event_id, row.provider_id), []).append(row)
    for candidates in qualifying.values():
        ranked = sorted(
            candidates,
            key=lambda item: (
                -(item.model_probability * float(item.offered_decimal_odds) - 1.0),
                -(item.model_probability - item.normalized_market_probability),
                item.outcome_key,
            ),
        )
        accepted.append(ranked[0])
        for _ in ranked[1:]:
            rejected["mutually-exclusive-1x2-selection"] = (
                rejected.get("mutually-exclusive-1x2-selection", 0) + 1
            )
    pnl_rows = [
        float(row.offered_decimal_odds) - 1.0 if row.outcome_key == row.observed_outcome else -1.0
        for row in accepted
    ]
    cumulative = 0.0
    peak = 0.0
    drawdown = 0.0
    for value in pnl_rows:
        cumulative += value
        peak = max(peak, cumulative)
        drawdown = max(drawdown, peak - cumulative)
    turnover = float(len(accepted))
    pnl = math.fsum(pnl_rows)
    outcomes_by_event: dict[str, set[str]] = {}
    for row in rows:
        outcomes_by_event.setdefault(row.canonical_event_id, set()).add(row.outcome_key)
    complete_markets = sum(
        outcomes == {"home", "draw", "away"} for outcomes in outcomes_by_event.values()
    )
    return HistoricalScoreBacktest(
        tested_events=len({row.canonical_event_id for row in rows}),
        quote_rows=len(rows),
        complete_markets=complete_markets,
        quote_coverage=complete_markets / len(outcomes_by_event),
        accepted_selections=len(accepted),
        rejected_selections=len(rows) - len(accepted),
        turnover=turnover,
        profit_and_loss=pnl,
        roi=None if turnover == 0 else pnl / turnover,
        maximum_drawdown=drawdown,
        rejection_reasons=tuple(sorted(rejected.items())),
        odds_buckets=_performance_buckets(
            accepted,
            key=lambda item: (
                "odds-under-2"
                if item.offered_decimal_odds < 2
                else "odds-2-to-3"
                if item.offered_decimal_odds < 3
                else "odds-3-plus"
            ),
        ),
        edge_buckets=_performance_buckets(
            accepted,
            key=lambda item: (
                "edge-under-5pct"
                if item.model_probability - item.normalized_market_probability < 0.05
                else "edge-5-to-10pct"
                if item.model_probability - item.normalized_market_probability < 0.10
                else "edge-10pct-plus"
            ),
        ),
        calibration_by_price_bucket=_calibration_buckets(rows),
        competitions=tuple(sorted({row.competition_id for row in rows})),
        seasons=tuple(sorted({row.season_id for row in rows})),
        model=model,
        market_baseline="normalized-historical-closing-1x2",
        quote_classification=HISTORICAL_CLOSING,
        limitations=(
            "closing benchmark is not an executable historical recommendation",
            "one-unit results are diagnostic and do not imply future profitability",
            "only genuinely priced 1X2 selections are evaluated",
            "at most one outcome is selected per event/provider 1X2 market",
        ),
    )


def write_historical_score_backtest(
    *,
    root: Path,
    relative_directory: str,
    backtest: HistoricalScoreBacktest,
) -> AnalyticalArtifact:
    return write_analytical_artifact(
        root=root,
        relative_directory=relative_directory,
        artifact_type=SCORE_BACKTEST_TYPE,
        schema_version=SCORE_BACKTEST_SCHEMA,
        payload=_payload(backtest),
    )


def load_historical_score_backtest(
    *,
    root: Path,
    relative_directory: str,
    expected_checksum: str | None = None,
) -> AnalyticalArtifact:
    artifact = load_analytical_artifact(
        root=root,
        relative_directory=relative_directory,
        expected_artifact_type=SCORE_BACKTEST_TYPE,
        expected_schema_version=SCORE_BACKTEST_SCHEMA,
        expected_checksum=expected_checksum,
    )
    payload = artifact.payload
    if (
        not isinstance(payload, dict)
        or payload.get("quote_classification") != HISTORICAL_CLOSING
        or payload.get("placement_state") != "historical-diagnostic-only"
    ):
        raise ArtifactError("historical score backtest trust state is invalid")
    return artifact


def _payload(backtest: HistoricalScoreBacktest) -> dict[str, JsonValue]:
    return {
        "tested_events": backtest.tested_events,
        "quote_rows": backtest.quote_rows,
        "complete_markets": backtest.complete_markets,
        "quote_coverage": backtest.quote_coverage,
        "accepted_selections": backtest.accepted_selections,
        "rejected_selections": backtest.rejected_selections,
        "turnover": backtest.turnover,
        "profit_and_loss": backtest.profit_and_loss,
        "roi": backtest.roi,
        "maximum_drawdown": backtest.maximum_drawdown,
        "rejection_reasons": [
            {"reason_code": reason, "rows": rows} for reason, rows in backtest.rejection_reasons
        ],
        "odds_buckets": [
            {"bucket": bucket, "selections": selections, "pnl": pnl, "roi": roi}
            for bucket, selections, pnl, roi in backtest.odds_buckets
        ],
        "edge_buckets": [
            {"bucket": bucket, "selections": selections, "pnl": pnl, "roi": roi}
            for bucket, selections, pnl, roi in backtest.edge_buckets
        ],
        "calibration_by_price_bucket": [
            {
                "bucket": bucket,
                "rows": rows,
                "mean_model_probability": model_probability,
                "empirical_frequency": empirical,
            }
            for bucket, rows, model_probability, empirical in (backtest.calibration_by_price_bucket)
        ],
        "competitions": list(backtest.competitions),
        "seasons": list(backtest.seasons),
        "model": backtest.model,
        "market_baseline": backtest.market_baseline,
        "quote_classification": backtest.quote_classification,
        "limitations": list(backtest.limitations),
        "placement_state": "historical-diagnostic-only",
    }


def _performance_buckets(
    rows: list[Historical1x2Evaluation],
    *,
    key: Callable[[Historical1x2Evaluation], str],
) -> tuple[tuple[str, int, float, float | None], ...]:
    buckets: dict[str, list[float]] = {}
    for row in rows:
        value = (
            float(row.offered_decimal_odds) - 1.0
            if row.outcome_key == row.observed_outcome
            else -1.0
        )
        buckets.setdefault(key(row), []).append(value)
    return tuple(
        (
            bucket,
            len(values),
            math.fsum(values),
            math.fsum(values) / len(values),
        )
        for bucket, values in sorted(buckets.items())
    )


def _calibration_buckets(
    rows: tuple[Historical1x2Evaluation, ...],
) -> tuple[tuple[str, int, float, float], ...]:
    buckets: dict[str, list[Historical1x2Evaluation]] = {}
    for row in rows:
        bucket = (
            "price-under-2"
            if row.offered_decimal_odds < 2
            else "price-2-to-3"
            if row.offered_decimal_odds < 3
            else "price-3-plus"
        )
        buckets.setdefault(bucket, []).append(row)
    return tuple(
        (
            bucket,
            len(values),
            math.fsum(item.model_probability for item in values) / len(values),
            sum(item.outcome_key == item.observed_outcome for item in values) / len(values),
        )
        for bucket, values in sorted(buckets.items())
    )
