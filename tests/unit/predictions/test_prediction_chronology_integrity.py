"""Focused regressions for PR #8 prediction-chronology microfix."""

from __future__ import annotations

from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from tests.unit.backtesting.test_football_backtest_publication import _feature_artifact
from tests.unit.predictions.test_prediction_value_layer import START, _prediction
from tests.unit.predictions.test_second_correction_regressions import _runtime, _trained_fixture
from tests.unit.predictions.test_surgical_final_integrity import (
    _analysis_payload,
    _two_market_analysis_datasets,
)

from sports_analytics.artifact_schemas import (
    _recompute_prediction_id,
    validate_dataset_row_schema,
)
from sports_analytics.artifact_serializers import serialize_prediction_row
from sports_analytics.artifacts import load_typed_analytical_artifact
from sports_analytics.core.exceptions import ArtifactError
from sports_analytics.predictions.contracts import (
    PredictionLineage,
    PredictionQualityFlags,
    SelectionProbability,
    build_market_prediction,
)
from sports_analytics.predictions.provenance import PredictionProvenance
from sports_analytics.predictions.replay import derive_historical_replay_cutoff_utc
from sports_analytics.predictions.service import (
    VerifiedPredictionRequest,
    generate_verified_football_1x2_prediction,
)
from sports_analytics.services.analysis import ANALYSIS_ARTIFACT_SCHEMA
from sports_analytics.services.analysis_json import publish_analysis_with_paths
from sports_analytics.services.backtesting import (
    FOOTBALL_CLOSING_BACKTEST_SCHEMA,
    FootballBacktestRequest,
    run_and_publish_football_closing_backtest,
)
from sports_analytics.services.historical_analysis import publish_historical_analysis_with_paths
from sports_analytics.value.contracts import CompleteMarketQuote, PricedSelection


def _valid_synthetic_prediction_row() -> dict[str, object]:
    return serialize_prediction_row(_prediction(), provenance="synthetic-contract")


def _recompute_prediction_row(row: dict[str, object]) -> dict[str, object]:
    forged = dict(row)
    forged["prediction_id"] = _recompute_prediction_id(forged)
    return forged


def _historical_replay_prediction_row() -> dict[str, object]:
    event_start = START
    replay_cutoff = derive_historical_replay_cutoff_utc(event_start)
    base = _prediction()
    lineage = PredictionLineage(
        model_artifact_id="model-replay",
        model_checksum_sha256="a" * 64,
        model_specification_version="model-v1",
        feature_artifact_id="feature-replay",
        feature_manifest_checksum_sha256="b" * 64,
        feature_specification_version="feature-v1",
        feature_row_id="event-1",
        trained_through_date=date(2024, 2, 1),
        calibrated_through_date=date(2024, 2, 2),
    )
    prediction = build_market_prediction(
        canonical_event_id="event-1",
        event_start_utc=event_start,
        predicted_at_utc=replay_cutoff,
        feature_available_at_utc=replay_cutoff,
        lineage=lineage,
        probabilities=(
            SelectionProbability(base.probabilities[0].selection, 0.6),
            SelectionProbability(base.probabilities[1].selection, 0.4),
        ),
        quality=PredictionQualityFlags(
            calibrated=False,
            model_artifact_verified=False,
            feature_artifact_verified=False,
            sufficient_history=False,
            data_quality_passed=False,
        ),
        provenance=PredictionProvenance.HISTORICAL_REPLAY,
    )
    return serialize_prediction_row(prediction, provenance="historical-replay")


def test_feature_availability_after_prediction_time_rejected() -> None:
    row = _recompute_prediction_row(
        {
            **_valid_synthetic_prediction_row(),
            "feature_available_at_utc": "2024-02-10T13:00:00.000000Z",
            "predicted_at_utc": "2024-02-10T12:00:00.000000Z",
        }
    )
    with pytest.raises(ArtifactError, match="feature_available_at_utc must not follow"):
        validate_dataset_row_schema("predictions", row, version="predictions-v2")


def test_prediction_at_event_start_rejected() -> None:
    row = _recompute_prediction_row(
        {
            **_valid_synthetic_prediction_row(),
            "predicted_at_utc": "2024-02-10T15:00:00.000000Z",
        }
    )
    with pytest.raises(ArtifactError, match="strictly before event_start_utc"):
        validate_dataset_row_schema("predictions", row, version="predictions-v2")


def test_prediction_after_event_start_rejected() -> None:
    row = _recompute_prediction_row(
        {
            **_valid_synthetic_prediction_row(),
            "predicted_at_utc": "2024-02-10T16:00:00.000000Z",
        }
    )
    with pytest.raises(ArtifactError, match="strictly before event_start_utc"):
        validate_dataset_row_schema("predictions", row, version="predictions-v2")


def test_training_cutoff_reaching_event_date_rejected() -> None:
    row = deepcopy(_valid_synthetic_prediction_row())
    lineage = dict(row["lineage"])
    lineage["trained_through_date"] = "2024-02-10"
    lineage["calibrated_through_date"] = "2024-02-10"
    row["lineage"] = lineage
    row = _recompute_prediction_row(row)
    with pytest.raises(ArtifactError, match="trained_through_date must be before event start date"):
        validate_dataset_row_schema("predictions", row, version="predictions-v2")


def test_calibration_cutoff_reaching_event_date_rejected() -> None:
    row = deepcopy(_valid_synthetic_prediction_row())
    lineage = dict(row["lineage"])
    lineage["calibrated_through_date"] = "2024-02-10"
    row["lineage"] = lineage
    row = _recompute_prediction_row(row)
    with pytest.raises(
        ArtifactError,
        match="calibrated_through_date must be before event start date",
    ):
        validate_dataset_row_schema("predictions", row, version="predictions-v2")


def test_historical_replay_with_arbitrary_earlier_cutoff_rejected() -> None:
    row = _recompute_prediction_row(
        {
            **_historical_replay_prediction_row(),
            "predicted_at_utc": "2024-02-10T12:00:00.000000Z",
            "feature_available_at_utc": "2024-02-10T12:00:00.000000Z",
        }
    )
    with pytest.raises(ArtifactError, match="must equal replay cutoff"):
        validate_dataset_row_schema("predictions", row, version="predictions-v2")


def test_historical_replay_with_unequal_feature_and_prediction_times_rejected() -> None:
    replay_cutoff = derive_historical_replay_cutoff_utc(START)
    row = _recompute_prediction_row(
        {
            **_historical_replay_prediction_row(),
            "predicted_at_utc": replay_cutoff.isoformat().replace("+00:00", "Z"),
            "feature_available_at_utc": (replay_cutoff - timedelta(hours=1))
            .isoformat()
            .replace(
                "+00:00",
                "Z",
            ),
        }
    )
    with pytest.raises(
        ArtifactError,
        match="feature_available_at_utc must equal predicted_at_utc",
    ):
        validate_dataset_row_schema("predictions", row, version="predictions-v2")


def test_valid_synthetic_prediction_artifacts_still_reload(tmp_path: Path) -> None:
    datasets = _two_market_analysis_datasets(tmp_path)
    for row in datasets["predictions"]:
        validate_dataset_row_schema("predictions", row, version="predictions-v2")


def test_valid_historical_analysis_and_football_backtest_artifacts_still_reload(
    tmp_path: Path,
) -> None:
    paths, artifact, trained, vector = _trained_fixture(tmp_path)
    event_start = vector.metadata.scheduled_start_utc
    assert event_start is not None
    prediction = generate_verified_football_1x2_prediction(
        paths=paths,
        request=VerifiedPredictionRequest(
            model_relative_path=trained.final_artifact_relative_directory,
            model_checksum_sha256=trained.final_artifact_checksum,
            feature_relative_directory=artifact.relative_directory,
            feature_manifest_checksum_sha256=artifact.manifest_checksum_sha256,
            canonical_event_id=vector.metadata.canonical_event_id,
            event_start_utc=event_start,
            predicted_at_utc=derive_historical_replay_cutoff_utc(event_start),
            provenance=PredictionProvenance.HISTORICAL_REPLAY,
        ),
    )
    quote = CompleteMarketQuote(
        canonical_event_id=vector.metadata.canonical_event_id,
        source_name="feed",
        provider_type="bookmaker",
        provider_id="provider-a",
        quote_phase="current",
        source_observed_at_utc=event_start - timedelta(hours=1),
        quoted_at_utc=event_start - timedelta(hours=2),
        quote_timestamp_precision="exact",
        quote_valid_from_utc=None,
        quote_valid_to_utc=None,
        selections=tuple(
            PricedSelection(
                selection=item.selection,
                decimal_odds=Decimal("2.10"),
                quote_series_id=f"series-{index}",
                quote_observation_id=f"quote-{index}",
            )
            for index, item in enumerate(prediction.probabilities)
        ),
    )
    quote_payload = {
        "canonical_event_id": quote.canonical_event_id,
        "source_name": quote.source_name,
        "provider_type": quote.provider_type,
        "provider_id": quote.provider_id,
        "quote_phase": quote.quote_phase,
        "source_observed_at_utc": quote.source_observed_at_utc.isoformat().replace("+00:00", "Z"),
        "quoted_at_utc": quote.quoted_at_utc.isoformat().replace("+00:00", "Z")
        if quote.quoted_at_utc is not None
        else None,
        "quote_timestamp_precision": quote.quote_timestamp_precision,
        "selections": [
            {
                "sport_code": priced.selection.sport_code,
                "market_family": priced.selection.market_family,
                "market_key": priced.selection.market_key,
                "market_period": priced.selection.market_period,
                "participant_scope": priced.selection.participant_scope,
                "canonical_participant_id": priced.selection.canonical_participant_id,
                "line_type": priced.selection.line_type,
                "line_value": (
                    None
                    if priced.selection.line_value is None
                    else str(priced.selection.line_value)
                ),
                "outcome_key": priced.selection.outcome_key,
                "decimal_odds": str(priced.decimal_odds),
                "quote_series_id": priced.quote_series_id,
                "quote_observation_id": priced.quote_observation_id,
            }
            for priced in quote.selections
        ],
    }
    published = publish_historical_analysis_with_paths(
        {
            "canonical_event_id": vector.metadata.canonical_event_id,
            "event_start_utc": event_start.isoformat().replace("+00:00", "Z"),
            "quote": quote_payload,
            "filters": {"minimum_edge": -1, "minimum_expected_value": -1},
        },
        paths=paths,
        model_relative_path=trained.final_artifact_relative_directory,
        model_checksum_sha256=trained.final_artifact_checksum,
        feature_relative_directory=artifact.relative_directory,
        feature_manifest_checksum_sha256=artifact.manifest_checksum_sha256,
    )
    loaded_analysis = load_typed_analytical_artifact(
        root=paths.exports_directory,
        relative_directory=str(published["relative_directory"]),
        expected_kind="analysis",
        expected_schema_version=ANALYSIS_ARTIFACT_SCHEMA,
        expected_checksum=str(published["checksum_sha256"]),
    )
    for row in loaded_analysis.dataset("predictions").rows:
        validate_dataset_row_schema("predictions", row, version="predictions-v2")

    # The content-addressed feature path is long; retain Windows path headroom.
    paths_backtest, feature_artifact = _feature_artifact(tmp_path / "b")
    published_backtest = run_and_publish_football_closing_backtest(
        paths=paths_backtest,
        request=FootballBacktestRequest(
            feature_relative_directory=feature_artifact.relative_directory,
            feature_manifest_checksum=feature_artifact.manifest_checksum_sha256,
            minimum_edge=-1.0,
            minimum_expected_value=-1.0,
            random_seed=42,
        ),
    )
    loaded_backtest = load_typed_analytical_artifact(
        root=paths_backtest.exports_directory,
        relative_directory=published_backtest.artifact.relative_directory,
        expected_kind="backtest",
        expected_schema_version=FOOTBALL_CLOSING_BACKTEST_SCHEMA,
        expected_checksum=published_backtest.artifact.checksum_sha256,
        expected_artifact_id=published_backtest.artifact.artifact_id,
    )
    for row in loaded_backtest.dataset("predictions").rows:
        validate_dataset_row_schema("predictions", row, version="predictions-v2")


def test_deterministic_repeated_publication_remains_unchanged(tmp_path: Path) -> None:
    paths = _runtime(tmp_path)
    payload = {**_analysis_payload(), "provenance": "synthetic-contract"}
    first = publish_analysis_with_paths(payload, paths=paths)
    second = publish_analysis_with_paths(
        {**payload, "relative_directory": f"{first['relative_directory']}-repeat"},
        paths=paths,
    )
    assert first["analysis_run_id"] == second["analysis_run_id"]
    assert first["artifact_id"] == second["artifact_id"]
    assert first["checksum_sha256"] == second["checksum_sha256"]
