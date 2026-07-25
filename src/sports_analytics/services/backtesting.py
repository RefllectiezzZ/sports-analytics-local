"""Football rolling-origin benchmark orchestration and artifact publication."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import cast

from sports_analytics.artifact_schemas import DATASET_SCHEMA_VERSIONS
from sports_analytics.artifact_serializers import build_backtest_datasets
from sports_analytics.artifacts import (
    TypedAnalyticalArtifact,
    load_typed_analytical_artifact,
    write_typed_analytical_artifact,
)
from sports_analytics.backtesting.football import (
    FootballClosingBenchmark,
    run_football_1x2_closing_benchmark,
)
from sports_analytics.core.exceptions import BacktestError, FeatureError
from sports_analytics.core.paths import RuntimePaths
from sports_analytics.data.types import JsonValue
from sports_analytics.features.football.datasets import load_feature_artifact
from sports_analytics.opportunities.contracts import OpportunityFilter
from sports_analytics.predictions.contracts import PredictionInputSnapshot
from sports_analytics.predictions.provenance import PredictionProvenance

FOOTBALL_CLOSING_BACKTEST_SCHEMA: str = "football-1x2-closing-backtest-v2"


@dataclass(frozen=True, slots=True)
class FootballBacktestRequest:
    """Fixed strategy inputs for a football closing-market benchmark."""

    feature_relative_directory: str
    feature_manifest_checksum: str | None = None
    minimum_probability: float = 0.0
    minimum_edge: float = 0.0
    minimum_expected_value: float = 0.0
    selection_minimum_odds: Decimal = Decimal("1.0001")
    selection_maximum_odds: Decimal = Decimal("100000")
    random_seed: int = 42


@dataclass(frozen=True, slots=True)
class PublishedFootballBacktest:
    """In-memory benchmark plus verified immutable artifact."""

    benchmark: FootballClosingBenchmark
    artifact: TypedAnalyticalArtifact


def run_and_publish_football_closing_backtest(
    *,
    paths: RuntimePaths,
    request: FootballBacktestRequest,
) -> PublishedFootballBacktest:
    """Load a verified feature artifact, run folds, and atomically publish results."""
    try:
        manifest, vectors, quotes, folds = load_feature_artifact(
            features_root=paths.features_directory,
            relative_directory=request.feature_relative_directory,
            expected_manifest_checksum=request.feature_manifest_checksum,
        )
    except FeatureError as exc:
        raise BacktestError(str(exc)) from exc
    checksum_path = (
        paths.features_directory
        / request.feature_relative_directory.replace("\\", "/")
        / "manifest_checksum.sha256"
    )
    manifest_checksum = checksum_path.read_text(encoding="utf-8").strip()
    filters = OpportunityFilter(
        minimum_probability=request.minimum_probability,
        minimum_edge=request.minimum_edge,
        minimum_expected_value=request.minimum_expected_value,
        selection_minimum_odds=request.selection_minimum_odds,
        selection_maximum_odds=request.selection_maximum_odds,
        sport_codes=frozenset({"football"}),
        market_keys=frozenset({"football.match-result.1x2.full-match"}),
        provider_ids=frozenset({"market-average"}),
        include_historical_benchmarks=True,
    )
    benchmark = run_football_1x2_closing_benchmark(
        vectors=vectors,
        quotes=quotes,
        folds=folds,
        feature_artifact_id=str(manifest["artifact_id"]),
        feature_manifest_checksum_sha256=manifest_checksum,
        filters=filters,
        random_seed=request.random_seed,
        input_snapshots=_input_snapshots(manifest.get("input_snapshots")),
    )
    input_snapshot_rows = tuple(
        {
            "snapshot_id": item.snapshot_id,
            "manifest_checksum_sha256": item.manifest_checksum_sha256,
            "schema_version": item.schema_version,
            "source_name": item.source_name,
        }
        for item in _input_snapshots(manifest.get("input_snapshots"))
    )
    datasets = build_backtest_datasets(
        result=benchmark.result,
        predictions=benchmark.predictions,
        evaluations=benchmark.evaluations,
        feature_artifact_id=str(manifest["artifact_id"]),
        feature_manifest_checksum_sha256=manifest_checksum,
        input_snapshots=cast(tuple[dict[str, JsonValue], ...], input_snapshot_rows),
        random_seed=request.random_seed,
        test_event_count=benchmark.test_event_count,
        complete_quote_event_count=benchmark.complete_quote_event_count,
        quote_coverage=benchmark.quote_coverage,
        provenance=PredictionProvenance.HISTORICAL_REPLAY.value,
    )
    competition_id = str(manifest["competition_id"])
    relative = (
        f"backtests/{FOOTBALL_CLOSING_BACKTEST_SCHEMA}/"
        f"{competition_id}/{benchmark.result.backtest_id}"
    )
    artifact = write_typed_analytical_artifact(
        root=paths.exports_directory,
        relative_directory=relative,
        artifact_kind="backtest",
        schema_version=FOOTBALL_CLOSING_BACKTEST_SCHEMA,
        datasets=datasets,
        dataset_schema_versions=DATASET_SCHEMA_VERSIONS,
    )
    verified = load_typed_analytical_artifact(
        root=paths.exports_directory,
        relative_directory=artifact.relative_directory,
        expected_kind="backtest",
        expected_schema_version=FOOTBALL_CLOSING_BACKTEST_SCHEMA,
        expected_checksum=artifact.checksum_sha256,
        expected_artifact_id=artifact.artifact_id,
    )
    return PublishedFootballBacktest(benchmark=benchmark, artifact=verified)


def _input_snapshots(raw: object) -> tuple[PredictionInputSnapshot, ...]:
    if not isinstance(raw, list):
        return ()
    snapshots: list[PredictionInputSnapshot] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        snapshot_id = item.get("snapshot_id")
        checksum = item.get("manifest_checksum_sha256")
        if type(snapshot_id) is not str or type(checksum) is not str:
            continue
        schema = item.get("schema_version")
        source = item.get("source_name")
        snapshots.append(
            PredictionInputSnapshot(
                snapshot_id=snapshot_id,
                manifest_checksum_sha256=checksum,
                schema_version=schema if type(schema) is str else "snapshot-manifest-v1",
                source_name=source if type(source) is str else "unknown-source",
            )
        )
    return tuple(sorted(snapshots, key=lambda value: value.snapshot_id))
