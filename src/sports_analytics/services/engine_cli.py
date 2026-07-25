"""Analytics engine CLI for football 1X2 features, training, and inference."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from sports_analytics.core.cli import CONFIG_ERROR_EXIT, SUCCESS_EXIT, handle_common_modes
from sports_analytics.core.cli import build_argument_parser as build_common_argument_parser
from sports_analytics.core.exceptions import (
    ConfigurationError,
    DatabaseError,
    EvaluationError,
    FeatureError,
    ModelError,
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
from sports_analytics.evaluation.temporal import TemporalSplitConfig
from sports_analytics.features.contracts import (
    FOOTBALL_1X2_FEATURE_NAMES_V1,
    FOOTBALL_1X2_PREMATCH_FEATURES_V1,
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
        "Analytics engine for football 1X2 feature generation, training, and inference.",
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
        help="Minimum training rows per fold (default 60).",
    )
    parser.add_argument(
        "--min-calibration-rows",
        default=None,
        metavar="INTEGER",
        help="Minimum calibration rows per fold (default 20).",
    )
    parser.add_argument(
        "--min-test-rows",
        default=None,
        metavar="INTEGER",
        help="Minimum test rows per fold (default 20).",
    )
    parser.add_argument(
        "--minimum-events",
        default=None,
        metavar="INTEGER",
        help="Minimum finished events required to build features (default 30).",
    )
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

        if args.build_football_1x2_features:
            return _build_features(args)
        if args.train_football_1x2:
            return _train(args)
        if args.verify_model is not None:
            return _verify_model(args)
        if args.infer_football_1x2:
            return _infer(args)

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
        TrainingError,
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
            args.minimum_events,
        )
    )
    if common and (any(engine_modes) or domain_args):
        parser.error("engine modes cannot be combined with shared CLI modes")
    if args.build_football_1x2_features and not args.snapshot:
        parser.error("--build-football-1x2-features requires one or more --snapshot values")
    if args.train_football_1x2 and args.features is None:
        parser.error("--train-football-1x2 requires --features")
    if args.infer_football_1x2 and (args.model is None or args.feature_row_json is None):
        parser.error("--infer-football-1x2 requires --model and --feature-row-json")


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
    artifact = build_football_1x2_features(
        paths=runtime.paths,
        request=FeatureBuildRequest(
            relative_manifest_paths=tuple(args.snapshot),
            minimum_events=minimum_events,
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
    split = TemporalSplitConfig(
        min_train_rows=_optional_positive(args.min_train_rows, "min_train_rows", 60),
        min_calibration_rows=_optional_positive(
            args.min_calibration_rows, "min_calibration_rows", 20
        ),
        min_test_rows=_optional_positive(args.min_test_rows, "min_test_rows", 20),
    )
    result = train_football_1x2_model(
        paths=runtime.paths,
        request=TrainRequest(
            feature_relative_directory=args.features,
            feature_manifest_checksum=args.checksum,
            random_seed=seed,
            split_config=split,
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
        "model_specification_version": artifact.model_specification_version,
        "feature_specification_version": artifact.feature_specification_version,
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
    payload = json.loads(Path(args.feature_row_json).read_text(encoding="utf-8"))
    feature_names = tuple(payload["feature_names"])
    feature_values = tuple(float(value) for value in payload["feature_values"])
    specification = str(
        payload.get("feature_specification_version", FOOTBALL_1X2_PREMATCH_FEATURES_V1)
    )
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


def _optional_positive(value: str | None, field_name: str, default: int) -> int:
    if value is None:
        return default
    try:
        return parse_cli_positive_bounded_int(value, field_name=field_name)
    except RepositoryError as exc:
        raise ConfigurationError(str(exc)) from exc
