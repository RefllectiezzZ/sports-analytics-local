"""Analytics engine CLI for football 1X2 modelling and historical backtesting."""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Callable, Sequence
from decimal import Decimal, InvalidOperation
from pathlib import Path

from sports_analytics.artifacts import (
    load_analytical_artifact,
    load_typed_analytical_artifact,
)
from sports_analytics.bookmakers.operator_quotes import (
    export_operator_quote_csv_template,
    export_operator_quote_json_template,
)
from sports_analytics.core.cli import CONFIG_ERROR_EXIT, SUCCESS_EXIT, handle_common_modes
from sports_analytics.core.cli import build_argument_parser as build_common_argument_parser
from sports_analytics.core.exceptions import (
    ArtifactError,
    BacktestError,
    ConfigurationError,
    DatabaseError,
    EvaluationError,
    FeatureError,
    ModelError,
    PredictionError,
    RepositoryError,
    RuntimeBootstrapError,
    SportsAnalyticsError,
    TrainingError,
)
from sports_analytics.core.runtime import bootstrap_runtime, validate_configuration
from sports_analytics.core.validation import (
    parse_cli_bounded_int,
    parse_cli_positive_bounded_int,
)
from sports_analytics.data.codec import dumps_canonical_json, ensure_json_value
from sports_analytics.data.types import JsonValue
from sports_analytics.evaluation.temporal import TemporalSplitConfig
from sports_analytics.features.football.specification import (
    FOOTBALL_1X2_FEATURE_NAMES_V1,
    FOOTBALL_1X2_PREMATCH_FEATURES_V1,
)
from sports_analytics.predictions.service import (
    VerifiedPredictionRequest,
    generate_verified_football_1x2_prediction,
)
from sports_analytics.services.analysis_json import (
    build_combinations_from_json,
    evaluate_opportunities_from_json,
    generate_predictions_from_json,
    prediction_to_json,
    publish_analysis_with_paths,
    run_backtest_from_json,
    validate_combination_from_json,
)
from sports_analytics.services.backtesting import (
    FootballBacktestRequest,
    run_and_publish_football_closing_backtest,
)
from sports_analytics.services.combinations_trusted import (
    build_combinations_from_analysis_artifact,
)
from sports_analytics.services.football_product_cli import (
    capability_payload,
    run_football_product_json,
)
from sports_analytics.services.historical_analysis import publish_historical_analysis_with_paths
from sports_analytics.services.operations_cli import (
    add_operational_arguments,
    operational_mode_values,
    run_operational_mode,
)
from sports_analytics.services.training import (
    FeatureBuildRequest,
    TrainRequest,
    build_football_1x2_features,
    infer_from_feature_row,
    train_football_1x2_model,
    verify_model_artifact,
)


def build_argument_parser() -> argparse.ArgumentParser:
    """Build the analytics engine CLI argument parser."""
    parser = build_common_argument_parser(
        "engine",
        "Analytics engine for football 1X2 modelling and historical backtesting.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--build-football-1x2-features",
        action="store_true",
        help="Build an immutable football 1X2 feature artifact from explicit snapshots.",
    )
    mode.add_argument(
        "--train-football-1x2",
        action="store_true",
        help="Train, calibrate, and evaluate a football 1X2 logistic baseline model.",
    )
    mode.add_argument(
        "--verify-model",
        metavar="RELATIVE_PATH",
        default=None,
        help="Verify and inspect a persisted explicit model artifact.",
    )
    mode.add_argument(
        "--infer-football-1x2",
        action="store_true",
        help="Run calibrated inference for one JSON feature row.",
    )
    mode.add_argument(
        "--backtest-football-1x2",
        action="store_true",
        help="Run the rolling-origin Football-Data closing-line singles benchmark.",
    )
    mode.add_argument(
        "--verify-analysis-artifact",
        metavar="RELATIVE_DIRECTORY",
        default=None,
        help="Strictly verify an immutable analytical artifact under exports.",
    )
    mode.add_argument(
        "--verify-backtest-artifact",
        metavar="RELATIVE_DIRECTORY",
        default=None,
        help="Strictly verify a typed immutable backtest artifact under exports.",
    )
    mode.add_argument(
        "--artifact-summary",
        metavar="RELATIVE_DIRECTORY",
        default=None,
        help="Print a concise summary of one explicit typed artifact.",
    )
    mode.add_argument(
        "--generate-predictions",
        metavar="JSON_PATH",
        default=None,
        help=(
            "Synthetic/contract-validation prediction from explicit JSON "
            "(never production-eligible)."
        ),
    )
    mode.add_argument(
        "--generate-verified-predictions",
        metavar="JSON_PATH",
        default=None,
        help="Trusted football 1X2 prediction from explicit model and feature artifacts.",
    )
    mode.add_argument(
        "--evaluate-opportunities",
        metavar="JSON_PATH",
        default=None,
        help="Evaluate a complete quote and filter opportunities from explicit JSON.",
    )
    mode.add_argument(
        "--build-combinations",
        metavar="JSON_PATH",
        default=None,
        help="Build bounded combinations from synthetic-contract JSON opportunities only.",
    )
    mode.add_argument(
        "--validate-combination",
        metavar="JSON_PATH",
        default=None,
        help="Validate one manual combination from synthetic-contract JSON only.",
    )
    mode.add_argument(
        "--build-combinations-from-analysis",
        metavar="JSON_PATH",
        default=None,
        help=(
            "Build trusted combinations from verified analysis artifact opportunities "
            "by artifact directory and checksum."
        ),
    )
    mode.add_argument(
        "--run-backtest",
        metavar="JSON_PATH",
        default=None,
        help="Run a fixed rolling-origin strategy from explicit JSON folds.",
    )
    mode.add_argument(
        "--publish-analysis",
        metavar="JSON_PATH",
        default=None,
        help="Publish a synthetic-contract analysis artifact from explicit JSON inputs.",
    )
    mode.add_argument(
        "--publish-historical-analysis",
        metavar="JSON_PATH",
        default=None,
        help=(
            "Publish a historical-replay analysis artifact from verified model and "
            "feature artifacts plus quote inputs."
        ),
    )
    mode.add_argument(
        "--run-football-product",
        metavar="JSON_PATH",
        default=None,
        help=(
            "Run the bounded offline score-model, fair-odds, operator-quote, "
            "proposal, and persisted read-model workflow."
        ),
    )
    mode.add_argument(
        "--football-market-capabilities",
        action="store_true",
        help="Print the exact sport and market capability matrix.",
    )
    mode.add_argument(
        "--export-current-quote-template",
        action="store_true",
        help="Print the exact canonical current offered-quote CSV header.",
    )
    mode.add_argument(
        "--export-current-quote-json-template",
        action="store_true",
        help="Print one exact canonical current offered-quote JSON row.",
    )
    parser.add_argument(
        "--snapshot",
        action="append",
        default=None,
        metavar="RELATIVE_MANIFEST",
        help="Relative snapshot manifest path (repeatable). Required for feature build.",
    )
    parser.add_argument(
        "--features",
        default=None,
        metavar="RELATIVE_DIRECTORY",
        help="Relative feature artifact directory under the features root.",
    )
    parser.add_argument(
        "--model",
        default=None,
        metavar="RELATIVE_PATH",
        help="Relative model artifact path under the models root.",
    )
    parser.add_argument(
        "--feature-row-json",
        default=None,
        metavar="PATH",
        help="JSON file with ordered feature_names, feature_values, and specification version.",
    )
    parser.add_argument(
        "--model-checksum",
        default=None,
        metavar="SHA256",
        help="Expected model artifact checksum for verified prediction replay.",
    )
    parser.add_argument(
        "--feature-checksum",
        default=None,
        metavar="SHA256",
        help="Expected feature manifest checksum for verified prediction replay.",
    )
    parser.add_argument(
        "--checksum",
        default=None,
        metavar="SHA256",
        help="Optional expected artifact checksum.",
    )
    parser.add_argument(
        "--seed",
        default=None,
        metavar="INTEGER",
        help="Deterministic training seed (default: configuration seed).",
    )
    parser.add_argument(
        "--min-train-rows",
        default=None,
        metavar="INTEGER",
        help="Minimum training rows per fold when building features (default 60).",
    )
    parser.add_argument(
        "--min-calibration-rows",
        default=None,
        metavar="INTEGER",
        help="Minimum calibration rows per fold when building features (default 20).",
    )
    parser.add_argument(
        "--min-test-rows",
        default=None,
        metavar="INTEGER",
        help="Minimum test rows per fold when building features (default 20).",
    )
    parser.add_argument(
        "--maximum-folds",
        default=None,
        metavar="INTEGER",
        help="Maximum rolling-origin folds to retain when building features (default 8).",
    )
    parser.add_argument(
        "--minimum-events",
        default=None,
        metavar="INTEGER",
        help="Minimum finished events required to build features (default 30).",
    )
    parser.add_argument(
        "--minimum-probability",
        default=None,
        metavar="NUMBER",
        help="Backtest selection minimum model probability (default 0).",
    )
    parser.add_argument(
        "--minimum-edge",
        default=None,
        metavar="NUMBER",
        help="Backtest selection minimum normalized market edge (default 0).",
    )
    parser.add_argument(
        "--minimum-expected-value",
        default=None,
        metavar="NUMBER",
        help="Backtest selection minimum expected value (default 0).",
    )
    parser.add_argument(
        "--selection-minimum-odds",
        default=None,
        metavar="DECIMAL",
        help="Backtest per-selection decimal odds floor (default 1.0001).",
    )
    parser.add_argument(
        "--selection-maximum-odds",
        default=None,
        metavar="DECIMAL",
        help="Backtest per-selection decimal odds ceiling (default 100000).",
    )
    parser.add_argument(
        "--artifact-type",
        default=None,
        metavar="TYPE",
        help="Expected analytical artifact type for verification.",
    )
    parser.add_argument(
        "--artifact-schema",
        default=None,
        metavar="VERSION",
        help="Expected analytical artifact schema version for verification.",
    )
    add_operational_arguments(parser, mode)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the analytics engine CLI."""
    parser = build_argument_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        _validate_modes(parser, args)
        common_exit = handle_common_modes(args)
        if common_exit is not None:
            return common_exit
        operational_exit = run_operational_mode(args)
        if operational_exit is not None:
            return operational_exit

        if args.build_football_1x2_features:
            return _build_features(args)
        if args.train_football_1x2:
            return _train(args)
        if args.verify_model is not None:
            return _verify_model(args)
        if args.infer_football_1x2:
            return _infer(args)
        if args.backtest_football_1x2:
            return _backtest(args)
        if args.verify_analysis_artifact is not None:
            return _verify_analysis_artifact(args)
        if args.verify_backtest_artifact is not None:
            return _verify_typed_artifact(args, kind="backtest")
        if args.artifact_summary is not None:
            return _summarize_typed_artifact(args)
        if args.generate_predictions is not None:
            return _run_json_mode(args.generate_predictions, generate_predictions_from_json)
        if args.generate_verified_predictions is not None:
            return _generate_verified_predictions(args)
        if args.evaluate_opportunities is not None:
            return _run_json_mode(
                args.evaluate_opportunities,
                evaluate_opportunities_from_json,
            )
        if args.build_combinations is not None:
            return _run_json_mode(args.build_combinations, build_combinations_from_json)
        if args.build_combinations_from_analysis is not None:
            return _build_combinations_from_analysis(args)
        if args.validate_combination is not None:
            return _run_json_mode(
                args.validate_combination,
                validate_combination_from_json,
            )
        if args.run_backtest is not None:
            return _run_json_mode(args.run_backtest, run_backtest_from_json)
        if args.publish_analysis is not None:
            return _publish_analysis(args)
        if args.publish_historical_analysis is not None:
            return _publish_historical_analysis(args)
        if args.run_football_product is not None:
            return _run_football_product(args)
        if args.football_market_capabilities:
            print(dumps_canonical_json(capability_payload()))
            return SUCCESS_EXIT
        if args.export_current_quote_template:
            print(export_operator_quote_csv_template().decode("utf-8"), end="")
            return SUCCESS_EXIT
        if args.export_current_quote_json_template:
            print(export_operator_quote_json_template().decode("utf-8"), end="")
            return SUCCESS_EXIT

        parser.error(
            "select an engine mode such as --build-football-1x2-features or --train-football-1x2"
        )
        return CONFIG_ERROR_EXIT
    except (
        ConfigurationError,
        RuntimeBootstrapError,
        DatabaseError,
        RepositoryError,
        FeatureError,
        ModelError,
        EvaluationError,
        PredictionError,
        TrainingError,
        BacktestError,
        ArtifactError,
        SportsAnalyticsError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return CONFIG_ERROR_EXIT


def _validate_modes(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    engine_modes = [
        args.build_football_1x2_features,
        args.train_football_1x2,
        args.verify_model is not None,
        args.infer_football_1x2,
        args.backtest_football_1x2,
        args.verify_analysis_artifact is not None,
        args.verify_backtest_artifact is not None,
        args.artifact_summary is not None,
        args.generate_predictions is not None,
        args.generate_verified_predictions is not None,
        args.evaluate_opportunities is not None,
        args.build_combinations is not None,
        args.build_combinations_from_analysis is not None,
        args.validate_combination is not None,
        args.run_backtest is not None,
        args.publish_analysis is not None,
        args.publish_historical_analysis is not None,
        args.run_football_product is not None,
        args.football_market_capabilities,
        args.export_current_quote_template,
        args.export_current_quote_json_template,
        *operational_mode_values(args),
    ]
    if sum(1 for enabled in engine_modes if enabled) > 1:
        parser.error("engine modes are mutually exclusive")
    common = args.validate_config or args.database_status or args.migrate_database
    domain_args = any(
        value is not None
        for value in (
            args.snapshot,
            args.features,
            args.model,
            args.feature_row_json,
            args.checksum,
            args.seed,
            args.min_train_rows,
            args.min_calibration_rows,
            args.min_test_rows,
            args.maximum_folds,
            args.minimum_events,
            args.minimum_probability,
            args.minimum_edge,
            args.minimum_expected_value,
            args.selection_minimum_odds,
            args.selection_maximum_odds,
            args.model_checksum,
            args.feature_checksum,
            args.as_of_utc,
            args.artifact_type,
            args.artifact_schema,
        )
    )
    if common and (any(engine_modes) or domain_args):
        parser.error("engine modes cannot be combined with shared CLI modes")
    backtest_arguments = any(
        value is not None
        for value in (
            args.minimum_probability,
            args.minimum_edge,
            args.minimum_expected_value,
            args.selection_minimum_odds,
            args.selection_maximum_odds,
        )
    )
    if backtest_arguments and not args.backtest_football_1x2:
        parser.error("backtest filter arguments require --backtest-football-1x2")
    artifact_arguments = args.artifact_type is not None or args.artifact_schema is not None
    artifact_mode = (
        args.verify_analysis_artifact is not None
        or args.verify_backtest_artifact is not None
        or args.artifact_summary is not None
    )
    if artifact_arguments and not artifact_mode:
        parser.error("artifact type/schema arguments require an artifact mode")
    if args.build_football_1x2_features and not args.snapshot:
        parser.error("--build-football-1x2-features requires one or more --snapshot values")
    if args.train_football_1x2 and args.features is None:
        parser.error("--train-football-1x2 requires --features")
    if args.backtest_football_1x2 and args.features is None:
        parser.error("--backtest-football-1x2 requires --features")
    if args.infer_football_1x2 and (args.model is None or args.feature_row_json is None):
        parser.error("--infer-football-1x2 requires --model and --feature-row-json")
    if args.generate_verified_predictions is not None and (
        args.model is None
        or args.features is None
        or args.model_checksum is None
        or args.feature_checksum is None
    ):
        parser.error(
            "--generate-verified-predictions requires --model, --features, "
            "--model-checksum, and --feature-checksum"
        )
    if artifact_mode and args.artifact_schema is None:
        parser.error("artifact verification and summary require --artifact-schema")


def _build_features(args: argparse.Namespace) -> int:
    runtime = bootstrap_runtime(
        "engine",
        config_path=args.config,
        env_file=args.env_file,
    )
    minimum_events = 30
    if args.minimum_events is not None:
        try:
            minimum_events = parse_cli_positive_bounded_int(
                args.minimum_events,
                field_name="minimum_events",
            )
        except RepositoryError as exc:
            raise ConfigurationError(str(exc)) from exc
    split = TemporalSplitConfig(
        min_train_rows=_optional_positive(args.min_train_rows, "min_train_rows", 60),
        min_calibration_rows=_optional_positive(
            args.min_calibration_rows, "min_calibration_rows", 20
        ),
        min_test_rows=_optional_positive(args.min_test_rows, "min_test_rows", 20),
        maximum_folds=_optional_positive(args.maximum_folds, "maximum_folds", 8),
    )
    artifact = build_football_1x2_features(
        paths=runtime.paths,
        request=FeatureBuildRequest(
            relative_manifest_paths=tuple(args.snapshot),
            minimum_events=minimum_events,
            split_config=split,
        ),
    )
    print(
        "built feature artifact "
        f"id={artifact.artifact_id} "
        f"relative_directory={artifact.relative_directory} "
        f"rows={artifact.feature_row_count} "
        f"manifest_sha256={artifact.manifest_checksum_sha256}"
    )
    return SUCCESS_EXIT


def _train(args: argparse.Namespace) -> int:
    runtime = bootstrap_runtime(
        "engine",
        config_path=args.config,
        env_file=args.env_file,
    )
    seed = runtime.settings.application.deterministic_seed
    if args.seed is not None:
        try:
            seed = parse_cli_bounded_int(
                args.seed,
                field_name="seed",
                minimum=0,
                maximum=4_294_967_295,
            )
        except RepositoryError as exc:
            raise ConfigurationError(str(exc)) from exc
    result = train_football_1x2_model(
        paths=runtime.paths,
        request=TrainRequest(
            feature_relative_directory=args.features,
            feature_manifest_checksum=args.checksum,
            random_seed=seed,
        ),
    )
    print(
        "trained model "
        f"relative_directory={result.final_artifact_relative_directory} "
        f"checksum={result.final_artifact_checksum} "
        f"folds={len(result.folds)} "
        f"trained_through={result.trained_through_date.isoformat()} "
        f"calibrated_through={result.calibrated_through_date.isoformat()}"
    )
    return SUCCESS_EXIT


def _verify_model(args: argparse.Namespace) -> int:
    settings, paths = validate_configuration(config_path=args.config, env_file=args.env_file)
    del settings
    artifact = verify_model_artifact(
        paths=paths,
        relative_path=args.verify_model,
        expected_checksum=args.checksum,
    )
    summary = {
        "relative_path": artifact.relative_path,
        "checksum_sha256": artifact.checksum_sha256,
        "model_specification_version": artifact.specification.model_specification_version,
        "feature_specification_version": artifact.specification.feature_specification_version,
        "trained_through_date": artifact.trained_through_date.isoformat(),
        "calibrated_through_date": artifact.calibrated_through_date.isoformat(),
        "calibration_temperature": artifact.temperature,
        "outcome_labels": list(artifact.parameters.outcome_labels),
        "ordered_feature_names": list(artifact.parameters.feature_names),
        "serialization": artifact.document.get("serialization"),
        "evaluation_summary": artifact.document.get("evaluation_summary"),
    }
    print(dumps_canonical_json(ensure_json_value(summary)))
    return SUCCESS_EXIT


def _infer(args: argparse.Namespace) -> int:
    settings, paths = validate_configuration(config_path=args.config, env_file=args.env_file)
    del settings
    try:
        payload = json.loads(Path(args.feature_row_json).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FeatureError("feature row JSON is malformed or unreadable") from exc
    if not isinstance(payload, dict):
        raise FeatureError("feature row JSON must be an object")
    try:
        feature_names = tuple(payload["feature_names"])
        feature_values = tuple(float(value) for value in payload["feature_values"])
        specification = str(
            payload.get("feature_specification_version", FOOTBALL_1X2_PREMATCH_FEATURES_V1)
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise FeatureError(
            "feature row JSON is missing required fields or has invalid values"
        ) from exc
    if feature_names != FOOTBALL_1X2_FEATURE_NAMES_V1:
        raise FeatureError("feature_names must match the ordered v1 whitelist")
    probabilities = infer_from_feature_row(
        paths=paths,
        model_relative_path=args.model,
        feature_names=feature_names,
        feature_values=feature_values,
        feature_specification_version=specification,
        expected_checksum=args.checksum,
    )
    print(dumps_canonical_json(ensure_json_value(probabilities)))
    return SUCCESS_EXIT


def _backtest(args: argparse.Namespace) -> int:
    runtime = bootstrap_runtime(
        "engine",
        config_path=args.config,
        env_file=args.env_file,
    )
    seed = runtime.settings.application.deterministic_seed
    if args.seed is not None:
        try:
            seed = parse_cli_bounded_int(
                args.seed,
                field_name="seed",
                minimum=0,
                maximum=4_294_967_295,
            )
        except RepositoryError as exc:
            raise ConfigurationError(str(exc)) from exc
    published = run_and_publish_football_closing_backtest(
        paths=runtime.paths,
        request=FootballBacktestRequest(
            feature_relative_directory=args.features,
            feature_manifest_checksum=args.checksum,
            minimum_probability=_optional_finite_float(
                args.minimum_probability,
                "minimum_probability",
                0.0,
            ),
            minimum_edge=_optional_finite_float(args.minimum_edge, "minimum_edge", 0.0),
            minimum_expected_value=_optional_finite_float(
                args.minimum_expected_value,
                "minimum_expected_value",
                0.0,
            ),
            selection_minimum_odds=_optional_decimal(
                args.selection_minimum_odds,
                "selection_minimum_odds",
                Decimal("1.0001"),
            ),
            selection_maximum_odds=_optional_decimal(
                args.selection_maximum_odds,
                "selection_maximum_odds",
                Decimal("100000"),
            ),
            random_seed=seed,
        ),
    )
    result = published.benchmark.result
    print(
        "completed closing-line historical benchmark "
        f"id={result.backtest_id} "
        f"artifact={published.artifact.relative_directory} "
        f"bets={result.metrics.bet_count} "
        f"roi={result.metrics.roi:.12g} "
        f"coverage={published.benchmark.quote_coverage:.12g}"
    )
    return SUCCESS_EXIT


def _verify_analysis_artifact(args: argparse.Namespace) -> int:
    settings, paths = validate_configuration(config_path=args.config, env_file=args.env_file)
    del settings
    if args.artifact_type in {None, "analysis"}:
        artifact = load_typed_analytical_artifact(
            root=paths.exports_directory,
            relative_directory=args.verify_analysis_artifact,
            expected_kind="analysis",
            expected_schema_version=args.artifact_schema,
            expected_checksum=args.checksum,
        )
        _print_typed_artifact(artifact)
        return SUCCESS_EXIT
    legacy_artifact = load_analytical_artifact(
        root=paths.exports_directory,
        relative_directory=args.verify_analysis_artifact,
        expected_artifact_type=args.artifact_type,
        expected_schema_version=args.artifact_schema,
        expected_checksum=args.checksum,
    )
    print(
        dumps_canonical_json(
            {
                "artifact_id": legacy_artifact.artifact_id,
                "artifact_type": legacy_artifact.artifact_type,
                "schema_version": legacy_artifact.schema_version,
                "checksum_sha256": legacy_artifact.checksum_sha256,
                "relative_directory": legacy_artifact.relative_directory,
            }
        )
    )
    return SUCCESS_EXIT


def _verify_typed_artifact(args: argparse.Namespace, *, kind: str) -> int:
    settings, paths = validate_configuration(config_path=args.config, env_file=args.env_file)
    del settings
    relative = (
        args.verify_backtest_artifact if kind == "backtest" else args.verify_analysis_artifact
    )
    artifact = load_typed_analytical_artifact(
        root=paths.exports_directory,
        relative_directory=relative,
        expected_kind=kind,
        expected_schema_version=args.artifact_schema,
        expected_checksum=args.checksum,
    )
    _print_typed_artifact(artifact)
    return SUCCESS_EXIT


def _summarize_typed_artifact(args: argparse.Namespace) -> int:
    if args.artifact_type not in {"analysis", "backtest"}:
        raise ConfigurationError("--artifact-summary requires --artifact-type analysis or backtest")
    settings, paths = validate_configuration(config_path=args.config, env_file=args.env_file)
    del settings
    artifact = load_typed_analytical_artifact(
        root=paths.exports_directory,
        relative_directory=args.artifact_summary,
        expected_kind=args.artifact_type,
        expected_schema_version=args.artifact_schema,
        expected_checksum=args.checksum,
    )
    _print_typed_artifact(artifact)
    return SUCCESS_EXIT


def _print_typed_artifact(artifact: object) -> None:
    from sports_analytics.artifacts import TypedAnalyticalArtifact

    if not isinstance(artifact, TypedAnalyticalArtifact):
        raise ArtifactError("typed artifact summary received an invalid artifact")
    print(
        dumps_canonical_json(
            {
                "artifact_id": artifact.artifact_id,
                "artifact_kind": artifact.artifact_kind,
                "schema_version": artifact.schema_version,
                "checksum_sha256": artifact.checksum_sha256,
                "relative_directory": artifact.relative_directory,
                "datasets": {item.name: item.row_count for item in artifact.datasets},
            }
        )
    )


def _run_json_mode(
    path_text: str,
    operation: Callable[[object], dict[str, JsonValue]],
) -> int:
    normalized = path_text.replace("\\", "/")
    path = Path(normalized)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigurationError(f"cannot read JSON input: {normalized}") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigurationError(
            f"JSON input is malformed at line {exc.lineno}, column {exc.colno}"
        ) from exc
    result = operation(payload)
    print(dumps_canonical_json(ensure_json_value(result)))
    return SUCCESS_EXIT


def _run_football_product(args: argparse.Namespace) -> int:
    runtime = bootstrap_runtime(
        "engine",
        config_path=args.config,
        env_file=args.env_file,
    )
    result = run_football_product_json(
        path_text=args.run_football_product,
        exports_root=runtime.paths.exports_directory,
    )
    print(dumps_canonical_json(ensure_json_value(result)))
    return SUCCESS_EXIT


def _publish_analysis(args: argparse.Namespace) -> int:
    runtime = bootstrap_runtime(
        "engine",
        config_path=args.config,
        env_file=args.env_file,
    )
    normalized = args.publish_analysis.replace("\\", "/")
    path = Path(normalized)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigurationError(f"cannot read JSON input: {normalized}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigurationError(
            f"JSON input is malformed at line {exc.lineno}, column {exc.colno}"
        ) from exc
    result = publish_analysis_with_paths(payload, paths=runtime.paths)
    print(dumps_canonical_json(ensure_json_value(result)))
    return SUCCESS_EXIT


def _publish_historical_analysis(args: argparse.Namespace) -> int:
    runtime = bootstrap_runtime(
        "engine",
        config_path=args.config,
        env_file=args.env_file,
    )
    if type(args.model) is not str or not args.model:
        raise ConfigurationError("--model is required for historical analysis publication")
    if type(args.features) is not str or not args.features:
        raise ConfigurationError("--features is required for historical analysis publication")
    if type(args.model_checksum) is not str or not args.model_checksum:
        raise ConfigurationError("--model-checksum is required for historical analysis publication")
    if type(args.feature_checksum) is not str or not args.feature_checksum:
        raise ConfigurationError(
            "--feature-checksum is required for historical analysis publication"
        )
    normalized = args.publish_historical_analysis.replace("\\", "/")
    path = Path(normalized)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigurationError(f"cannot read JSON input: {normalized}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigurationError(
            f"JSON input is malformed at line {exc.lineno}, column {exc.colno}"
        ) from exc
    result = publish_historical_analysis_with_paths(
        payload,
        paths=runtime.paths,
        model_relative_path=args.model,
        model_checksum_sha256=args.model_checksum,
        feature_relative_directory=args.features,
        feature_manifest_checksum_sha256=args.feature_checksum,
    )
    print(dumps_canonical_json(ensure_json_value(result)))
    return SUCCESS_EXIT


def _generate_verified_predictions(args: argparse.Namespace) -> int:
    runtime = bootstrap_runtime(
        "engine",
        config_path=args.config,
        env_file=args.env_file,
    )
    normalized = args.generate_verified_predictions.replace("\\", "/")
    path = Path(normalized)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigurationError(f"cannot read JSON input: {normalized}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigurationError(
            f"JSON input is malformed at line {exc.lineno}, column {exc.colno}"
        ) from exc
    if not isinstance(document, dict):
        raise ConfigurationError("verified prediction request must be a JSON object")
    from datetime import datetime

    from sports_analytics.predictions.provenance import parse_prediction_provenance

    event_start_raw = document.get("event_start_utc")
    predicted_at_raw = document.get("predicted_at_utc")
    canonical_event_id = document.get("canonical_event_id")
    if type(event_start_raw) is not str or not event_start_raw:
        raise ConfigurationError("event_start_utc must be a non-empty JSON string")
    if type(predicted_at_raw) is not str or not predicted_at_raw:
        raise ConfigurationError("predicted_at_utc must be a non-empty JSON string")
    if type(canonical_event_id) is not str or not canonical_event_id:
        raise ConfigurationError("canonical_event_id must be a non-empty JSON string")
    try:
        event_start = datetime.fromisoformat(event_start_raw.replace("Z", "+00:00"))
        predicted_at = datetime.fromisoformat(predicted_at_raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ConfigurationError("verified prediction request timestamps are malformed") from exc
    provenance = parse_prediction_provenance(
        document.get("provenance", "historical-replay"),
        field_name="provenance",
    )
    if type(args.model_checksum) is not str or not args.model_checksum:
        raise ConfigurationError("--model-checksum must be a non-empty SHA-256 digest")
    if type(args.feature_checksum) is not str or not args.feature_checksum:
        raise ConfigurationError("--feature-checksum must be a non-empty SHA-256 digest")
    prediction = generate_verified_football_1x2_prediction(
        paths=runtime.paths,
        request=VerifiedPredictionRequest(
            model_relative_path=args.model,
            model_checksum_sha256=args.model_checksum,
            feature_relative_directory=args.features,
            feature_manifest_checksum_sha256=args.feature_checksum,
            canonical_event_id=canonical_event_id,
            event_start_utc=event_start,
            predicted_at_utc=predicted_at,
            provenance=provenance,
        ),
    )
    print(dumps_canonical_json(ensure_json_value(prediction_to_json(prediction))))
    return SUCCESS_EXIT


def _build_combinations_from_analysis(args: argparse.Namespace) -> int:
    runtime = bootstrap_runtime(
        "engine",
        config_path=args.config,
        env_file=args.env_file,
    )
    normalized = args.build_combinations_from_analysis.replace("\\", "/")
    path = Path(normalized)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigurationError(f"cannot read JSON input: {normalized}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigurationError(
            f"JSON input is malformed at line {exc.lineno}, column {exc.colno}"
        ) from exc
    if not isinstance(payload, dict):
        raise ConfigurationError("trusted combination request must be a JSON object")
    relative_directory = payload.get("relative_directory")
    expected_checksum = payload.get("checksum_sha256")
    if type(relative_directory) is not str or not relative_directory:
        raise ConfigurationError("relative_directory must be a non-empty JSON string")
    if type(expected_checksum) is not str or not expected_checksum:
        raise ConfigurationError("checksum_sha256 must be a non-empty JSON string")
    rules_payload = payload.get("rules", {})
    if not isinstance(rules_payload, dict):
        raise ConfigurationError("rules must be a JSON object")
    from sports_analytics.services.analysis_json import _rules

    filter_config_id = payload.get("filter_config_id")
    if filter_config_id is not None and (type(filter_config_id) is not str or not filter_config_id):
        raise ConfigurationError("filter_config_id must be a non-empty JSON string when provided")
    result = build_combinations_from_analysis_artifact(
        paths=runtime.paths,
        relative_directory=relative_directory,
        expected_checksum=expected_checksum,
        rules=_rules(rules_payload),
        filter_config_id=filter_config_id,
    )
    print(
        dumps_canonical_json(
            ensure_json_value(
                {
                    "evidence_label": "trusted-verified-analysis-artifact",
                    "combinations": [
                        {
                            "combination_id": item.combination_id,
                            "opportunity_ids": [leg.opportunity_id for leg in item.legs],
                            "expected_value": item.expected_value,
                        }
                        for item in result.combinations
                    ],
                    "rejection_count": len(result.rejections),
                    "truncated": result.truncated,
                }
            )
        )
    )
    return SUCCESS_EXIT


def _optional_positive(value: str | None, field_name: str, default: int) -> int:
    if value is None:
        return default
    try:
        return parse_cli_positive_bounded_int(value, field_name=field_name)
    except RepositoryError as exc:
        raise ConfigurationError(str(exc)) from exc


def _optional_finite_float(value: str | None, field_name: str, default: float) -> float:
    if value is None:
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ConfigurationError(f"{field_name} must be a finite number") from exc
    if not math.isfinite(parsed):
        raise ConfigurationError(f"{field_name} must be a finite number")
    return parsed


def _optional_decimal(value: str | None, field_name: str, default: Decimal) -> Decimal:
    if value is None:
        return default
    try:
        parsed = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ConfigurationError(f"{field_name} must be a finite decimal") from exc
    if not parsed.is_finite():
        raise ConfigurationError(f"{field_name} must be a finite decimal")
    return parsed
