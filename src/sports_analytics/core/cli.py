"""Shared CLI foundation for root entry-point scripts."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from typing import Final

from sports_analytics.core.exceptions import ConfigurationError, RuntimeBootstrapError
from sports_analytics.core.runtime import (
    bootstrap_runtime,
    format_validation_success,
    validate_configuration,
)

SUCCESS_EXIT: Final[int] = 0
CONFIG_ERROR_EXIT: Final[int] = 2

_PLACEHOLDER_MESSAGES: Final[dict[str, str]] = {
    "app": "Streamlit application is not implemented yet.",
    "scraper": "Data ingestion coordinator is not implemented yet.",
    "engine": "Analytics engine is not implemented yet.",
    "worker": "Background worker is not implemented yet.",
    "run_local": "Local startup coordinator is not implemented yet.",
}


def build_argument_parser(component: str, description: str) -> argparse.ArgumentParser:
    """Build the shared argparse parser for a root component."""
    parser = argparse.ArgumentParser(prog=component, description=description)
    parser.add_argument(
        "--config",
        dest="config",
        metavar="PATH",
        default=None,
        help="Explicit TOML configuration file path.",
    )
    parser.add_argument(
        "--env-file",
        dest="env_file",
        metavar="PATH",
        default=None,
        help="Explicit .env file path.",
    )
    parser.add_argument(
        "--validate-config",
        dest="validate_config",
        action="store_true",
        help=(
            "Validate and resolve configuration without creating directories, "
            "log files, or seeding global random state."
        ),
    )
    return parser


def run_component(
    component: str,
    description: str,
    argv: Sequence[str] | None = None,
) -> int:
    """Parse CLI arguments and run validation or placeholder bootstrap.

    Returns ``0`` on success and ``2`` for expected configuration or bootstrap
    errors. Unexpected programming errors propagate with their traceback.
    """
    parser = build_argument_parser(component, description)
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        if args.validate_config:
            settings, paths = validate_configuration(
                config_path=args.config,
                env_file=args.env_file,
            )
            print(format_validation_success(settings, paths))
            return SUCCESS_EXIT

        context = bootstrap_runtime(
            component,
            config_path=args.config,
            env_file=args.env_file,
        )
        message = _PLACEHOLDER_MESSAGES.get(
            component,
            f"{component}: business functionality is not implemented yet.",
        )
        context.logger.info("%s", message)
        print(f"{component}.py: {message}")
        return SUCCESS_EXIT
    except (ConfigurationError, RuntimeBootstrapError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return CONFIG_ERROR_EXIT
