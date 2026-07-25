"""Deterministic football UUID and season identifier helpers."""

from __future__ import annotations

import re
import uuid
from typing import Final

from sports_analytics.core.exceptions import NormalizationError, RepositoryError
from sports_analytics.data.types import validate_identifier

# Project-owned UUIDv5 namespace for football entity identity.
FOOTBALL_ENTITY_NAMESPACE: Final[uuid.UUID] = uuid.UUID("6f8a2c91-4d3e-5b7a-9c1d-2e4f6a8b0c1d")

_CANONICAL_SEASON_PATTERN: Final[re.Pattern[str]] = re.compile(r"^([0-9]{4})-([0-9]{4})$")
# Conservative supported start years for two-digit source season codes.
MIN_SUPPORTED_START_YEAR: Final[int] = 1993
MAX_SUPPORTED_START_YEAR: Final[int] = 2092


def build_team_id(*, source_name: str, normalized_source_team_key: str) -> str:
    """Return a deterministic UUIDv5 for a source-scoped team identity."""
    key = f"team|{source_name}|{normalized_source_team_key}"
    return str(uuid.uuid5(FOOTBALL_ENTITY_NAMESPACE, key))


def build_game_id(*, source_game_key: str) -> str:
    """Return a deterministic UUIDv5 for a canonical source game key."""
    key = f"game|{source_game_key}"
    return str(uuid.uuid5(FOOTBALL_ENTITY_NAMESPACE, key))


def build_quote_id(
    *,
    game_id: str,
    market_type: str,
    selection: str,
    provider_type: str,
    provider_id: str,
    quote_phase: str,
    source_column_family: str,
) -> str:
    """Return a deterministic UUIDv5 for a 1X2 odds quote identity."""
    key = (
        f"quote|{game_id}|{market_type}|{selection}|{provider_type}|"
        f"{provider_id}|{quote_phase}|{source_column_family}"
    )
    return str(uuid.uuid5(FOOTBALL_ENTITY_NAMESPACE, key))


def build_source_game_key(
    *,
    source_name: str,
    competition_id: str,
    season_id: str,
    event_date: str,
    home_team_key: str,
    away_team_key: str,
) -> str:
    """Construct the canonical source game key used for game identity."""
    return (
        f"{source_name}|{competition_id}|{season_id}|{event_date}|"
        f"{home_team_key}|{away_team_key}"
    )


def build_season_id(*, competition_id: str, label: str) -> str:
    """Return a stable season identifier from competition and canonical label."""
    return validate_identifier(f"{competition_id}:{label}", field_name="season_id")


def parse_canonical_season(value: str) -> tuple[str, int, int, str]:
    """Parse ``YYYY-YYYY`` into label, start year, end year, and source code.

    Rejects whitespace, signs, two-digit inputs, non-consecutive years, and
    start years outside the documented conservative range
    [{MIN_SUPPORTED_START_YEAR}, {MAX_SUPPORTED_START_YEAR}].
    """.format(
        MIN_SUPPORTED_START_YEAR=MIN_SUPPORTED_START_YEAR,
        MAX_SUPPORTED_START_YEAR=MAX_SUPPORTED_START_YEAR,
    )
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
