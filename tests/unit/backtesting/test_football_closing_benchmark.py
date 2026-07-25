"""Production football 1X2 closing-market benchmark adapter tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, time

from tests.helpers_training import synthetic_finished_events

from sports_analytics.backtesting.contracts import BacktestMode
from sports_analytics.backtesting.football import run_football_1x2_closing_benchmark
from sports_analytics.evaluation.temporal import TemporalSplitConfig, build_rolling_origin_folds
from sports_analytics.features.football.datasets import ClosingMarketQuoteTriple
from sports_analytics.features.football.prematch import generate_prematch_features
from sports_analytics.features.football.specification import FOOTBALL_1X2_OUTCOME_SPACE
from sports_analytics.opportunities.contracts import OpportunityFilter


def test_football_benchmark_fits_each_fold_and_labels_closing_quotes() -> None:
    events = tuple(
        replace(
            event,
            scheduled_start_utc=datetime.combine(
                event.event_date,
                time(15, 0),
                tzinfo=UTC,
            ),
        )
        for event in synthetic_finished_events(
            season_ids=("eng-premier-league:2023-2024",),
            matches_per_season=30,
        )
    )
    vectors = generate_prematch_features(events)
    folds = build_rolling_origin_folds(
        vectors,
        outcome_space=FOOTBALL_1X2_OUTCOME_SPACE,
        config=TemporalSplitConfig(
            min_train_rows=15,
            min_calibration_rows=5,
            min_test_rows=5,
            step_rows=5,
            maximum_folds=2,
        ),
    )
    quotes = tuple(
        _quote(vector.metadata.canonical_event_id, index) for index, vector in enumerate(vectors)
    )
    benchmark = run_football_1x2_closing_benchmark(
        vectors=vectors,
        quotes=quotes,
        folds=folds,
        feature_artifact_id="feature-artifact",
        feature_manifest_checksum_sha256="a" * 64,
        filters=OpportunityFilter(
            minimum_edge=-1.0,
            minimum_expected_value=-1.0,
            include_historical_benchmarks=True,
        ),
        random_seed=42,
    )
    assert benchmark.result.mode is BacktestMode.CLOSING_LINE_HISTORICAL_BENCHMARK
    assert benchmark.result.metrics.bet_count > 0
    assert benchmark.complete_quote_event_count == benchmark.test_event_count
    assert benchmark.quote_coverage == 1.0
    assert {bet.kind.value for bet in benchmark.result.bets} == {"single"}
    assert "no pre-kickoff quote availability" in benchmark.result.disclaimer


def _quote(event_id: str, index: int) -> ClosingMarketQuoteTriple:
    observed = datetime(2025, 1, 1, tzinfo=UTC)
    return ClosingMarketQuoteTriple(
        canonical_event_id=event_id,
        source_name="football-data-co-uk",
        provider_type="source-market-average",
        provider_id="market-average",
        quote_phase="closing",
        source_observed_at_utc=observed,
        quoted_at_utc=None,
        quote_timestamp_precision="snapshot-observation-only",
        home_quote_series_id=f"home-series-{index}",
        draw_quote_series_id=f"draw-series-{index}",
        away_quote_series_id=f"away-series-{index}",
        home_quote_observation_id=f"home-observation-{index}",
        draw_quote_observation_id=f"draw-observation-{index}",
        away_quote_observation_id=f"away-observation-{index}",
        home_odds=2.1,
        draw_odds=3.4,
        away_odds=3.7,
    )
