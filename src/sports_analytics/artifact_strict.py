"""Strict JSON type parsers for typed analytical artifact validation."""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import TypeVar

from sports_analytics.core.exceptions import ArtifactError
from sports_analytics.data.types import JsonValue, validate_sha256_checksum
from sports_analytics.predictions.contracts import CanonicalSelectionIdentity

T = TypeVar("T")


def require_str(value: object, *, field: str) -> str:
    if type(value) is not str or not value:
        raise ArtifactError(f"{field} must be a non-empty JSON string")
    return value


def require_optional_str(value: object, *, field: str) -> str | None:
    if value is None:
        return None
    return require_str(value, field=field)


def require_bool(value: object, *, field: str) -> bool:
    if type(value) is not bool:
        raise ArtifactError(f"{field} must be a JSON boolean")
    return value


def require_int(value: object, *, field: str) -> int:
    if type(value) is not int or isinstance(value, bool):
        raise ArtifactError(f"{field} must be a JSON integer")
    return value


def require_positive_int(value: object, *, field: str) -> int:
    number = require_int(value, field=field)
    if number < 1:
        raise ArtifactError(f"{field} must be a positive integer")
    return number


def require_finite_number(value: object, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ArtifactError(f"{field} must be a finite JSON number")
    number = float(value)
    if not math.isfinite(number):
        raise ArtifactError(f"{field} must be finite")
    return number


def require_probability(value: object, *, field: str) -> float:
    number = require_finite_number(value, field=field)
    if not 0.0 <= number <= 1.0:
        raise ArtifactError(f"{field} must lie in [0, 1]")
    return number


def require_decimal_string(value: object, *, field: str) -> Decimal:
    if type(value) is not str or not value:
        raise ArtifactError(f"{field} must be a non-empty decimal string")
    try:
        decimal_value = Decimal(value)
    except InvalidOperation as exc:
        raise ArtifactError(f"{field} must be a canonical decimal string") from exc
    if not decimal_value.is_finite():
        raise ArtifactError(f"{field} must be finite")
    return decimal_value


def require_date_string(value: object, *, field: str) -> date:
    text = require_str(value, field=field)
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ArtifactError(f"{field} must be a canonical YYYY-MM-DD date string") from exc


def require_utc_timestamp_string(value: object, *, field: str) -> datetime:
    text = require_str(value, field=field)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ArtifactError(f"{field} must be a canonical UTC timestamp string") from exc
    if parsed.tzinfo is None:
        raise ArtifactError(f"{field} must include an explicit UTC offset")
    return parsed


def require_canonical_utc_timestamp_string(value: object, *, field: str) -> datetime:
    """Require an explicit canonical UTC timestamp (``Z`` or ``+00:00`` suffix only)."""
    text = require_str(value, field=field)
    if not (text.endswith("Z") or text.endswith("+00:00")):
        raise ArtifactError(f"{field} must use canonical UTC (Z or +00:00)")
    parsed = require_utc_timestamp_string(value, field=field)
    if parsed.utcoffset() != timedelta(0):
        raise ArtifactError(f"{field} must be UTC")
    return parsed


def require_canonical_selection_identity(
    value: object,
    *,
    field: str,
) -> CanonicalSelectionIdentity:
    """Parse one canonical selection identity without coercing malformed JSON types."""
    selection_raw = require_dict(value, field=field)
    participant = selection_raw.get("canonical_participant_id")
    canonical_participant_id = None
    if participant is not None:
        canonical_participant_id = require_str(
            participant,
            field=f"{field}.canonical_participant_id",
        )
    line_value_raw = selection_raw.get("line_value")
    line_value = None
    if line_value_raw is not None:
        line_value = require_decimal_string(line_value_raw, field=f"{field}.line_value")
    return CanonicalSelectionIdentity(
        sport_code=require_str(selection_raw.get("sport_code"), field=f"{field}.sport_code"),
        market_family=require_str(
            selection_raw.get("market_family"),
            field=f"{field}.market_family",
        ),
        market_key=require_str(selection_raw.get("market_key"), field=f"{field}.market_key"),
        market_period=require_str(
            selection_raw.get("market_period"),
            field=f"{field}.market_period",
        ),
        participant_scope=require_str(
            selection_raw.get("participant_scope"),
            field=f"{field}.participant_scope",
        ),
        canonical_participant_id=canonical_participant_id,
        line_type=require_str(selection_raw.get("line_type"), field=f"{field}.line_type"),
        line_value=line_value,
        outcome_key=require_str(selection_raw.get("outcome_key"), field=f"{field}.outcome_key"),
    )


def require_sha256_checksum(value: object, *, field: str) -> str:
    text = require_str(value, field=field)
    try:
        validate_sha256_checksum(text)
    except Exception as exc:
        raise ArtifactError(f"{field} must be a valid SHA-256 checksum") from exc
    return text


def require_dict(value: object, *, field: str) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise ArtifactError(f"{field} must be a JSON object")
    return value


def require_list(value: object, *, field: str) -> list[JsonValue]:
    if not isinstance(value, list):
        raise ArtifactError(f"{field} must be a JSON array")
    return value


def require_str_list(value: object, *, field: str) -> list[str]:
    items = require_list(value, field=field)
    result: list[str] = []
    for index, item in enumerate(items):
        result.append(require_str(item, field=f"{field}[{index}]"))
    return result
