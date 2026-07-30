"""Frozen worker handler for the bounded offline football product workflow."""

from __future__ import annotations

from typing import Final

from sports_analytics.core.exceptions import ConfigurationError
from sports_analytics.data.types import JsonValue
from sports_analytics.jobs.context import JobExecutionContext
from sports_analytics.services.football_product_cli import (
    run_football_product_document,
)

RUN_FOOTBALL_PRODUCT_JOB_TYPE: Final[str] = "analysis.football-product"


def run_football_product_handler(
    context: JobExecutionContext,
    payload: JsonValue,
) -> JsonValue:
    """Execute one typed, bounded payload without dynamic imports or network access."""
    context.checkpoint()
    if context._exports_directory is None:
        raise ConfigurationError("football product job requires bound runtime exports")
    result = run_football_product_document(
        document=payload,
        exports_root=context._exports_directory,
    )
    context.checkpoint()
    return result
