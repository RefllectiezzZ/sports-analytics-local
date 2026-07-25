"""Football season parsing and football source-version identifiers.

Canonical and source-scoped entity identifiers live in
:mod:`sports_analytics.sports.identifiers` because they are sport-agnostic.
"""

from __future__ import annotations

import re
from typing import Final

from sports_analytics.core.exceptions import NormalizationError, RepositoryError
from sports_analytics.data.types import validate_identifier
from sports_analytics.sports.identifiers import build_season_id as build_season_id

_CANONICAL_SEASON_PATTERN: Final[re.Pattern[str]] = re.compile(r"^([0-9]{4})-([0-9]{4})$")
# Conservative supported start years for two-digit source season codes.
MIN_SUPPORTED_START_YEAR: Final[int] = 1993
MAX_SUPPORTED_START_YEAR: Final[int] = 2092

__all__ = [
    "MAX_SUPPORTED_START_YEAR",
    "MIN_SUPPORTED_START_YEAR",
    "build_season_id",
    "build_source_version",
    "parse_canonical_season",
]


def parse_canonical_season(value: str) -> tuple[str, int, int, str]:
    """Parse ``YYYY-YYYY`` into label, start year, end year, and source code.

    Rejects whitespace, signs, two-digit inputs, non-consecutive years, and
    start years outside the documented conservative range
    [1993, 2092].
    """
    if not isinstance(value, str):
        msg = "season must be a string in YYYY-YYYY format"
        raise NormalizationError(msg)
    if value != value.strip():
        msg = "season must not have leading or trailing whitespace"
        raise NormalizationError(msg)
    if not value:
        msg = "season must be non-empty"
        raise NormalizationError(msg)
    match = _CANONICAL_SEASON_PATTERN.fullmatch(value)
    if match is None:
        msg = "season must use YYYY-YYYY with exactly four decimal digits per year"
        raise NormalizationError(msg)
    start_year = int(match.group(1), 10)
    end_year = int(match.group(2), 10)
    if end_year != start_year + 1:
        msg = "season end_year must equal start_year + 1"
        raise NormalizationError(msg)
    if start_year < MIN_SUPPORTED_START_YEAR or start_year > MAX_SUPPORTED_START_YEAR:
        msg = (
            f"season start_year must be between {MIN_SUPPORTED_START_YEAR} and "
            f"{MAX_SUPPORTED_START_YEAR} inclusive"
        )
        raise NormalizationError(msg)
    # Two-digit source codes collide across centuries; keep the supported window
    # narrow enough that YY maps uniquely within the documented range.
    source_season_code = f"{start_year % 100:02d}{end_year % 100:02d}"
    try:
        validate_identifier(source_season_code, field_name="source_season_code")
    except RepositoryError as exc:
        raise NormalizationError(str(exc)) from exc
    return value, start_year, end_year, source_season_code


def build_source_version(
    *,
    source_competition_code: str,
    source_season_code: str,
    raw_sha256: str,
) -> str:
    """Build a deterministic source_version identifier for snapshot deduplication."""
    code = source_competition_code.lower()
    value = f"{code}:{source_season_code}:sha256:{raw_sha256}"
    try:
        return validate_identifier(value, field_name="source_version")
    except RepositoryError as exc:
        raise NormalizationError(str(exc)) from exc
