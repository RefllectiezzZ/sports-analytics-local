from __future__ import annotations

from decimal import Decimal

from sports_analytics.backtesting.football_scores import (
    Historical1x2Evaluation,
    load_historical_score_backtest,
    run_historical_score_backtest,
    write_historical_score_backtest,
)


def test_score_backtest_uses_only_labelled_real_historical_prices(tmp_path) -> None:
    rows = tuple(
        Historical1x2Evaluation(
            canonical_event_id=f"event-{index}",
            competition_id="league",
            season_id="2024",
            provider_id="historical-source",
            outcome_key="home",
            observed_outcome=("home" if index % 2 == 0 else "away"),
            model_probability=0.55,
            normalized_market_probability=0.45,
            offered_decimal_odds=Decimal("2.20"),
        )
        for index in range(6)
    )
    result = run_historical_score_backtest(rows)
    assert result.tested_events == 6
    assert result.accepted_selections == 6
    assert result.turnover == 6.0
    assert result.quote_classification == "historical-closing-benchmark"
    artifact = write_historical_score_backtest(
        root=tmp_path,
        relative_directory="backtest",
        backtest=result,
    )
    assert (
        load_historical_score_backtest(
            root=tmp_path,
            relative_directory="backtest",
            expected_checksum=artifact.checksum_sha256,
        ).artifact_id
        == artifact.artifact_id
    )
