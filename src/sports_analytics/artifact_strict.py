"""Strict JSON type parsers for typed analytical artifact validation."""

from __future__ import annotations

import math
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import TypeVar

from sports_analytics.core.exceptions import ArtifactError
from sports_analytics.data.types import JsonValue, validate_sha256_checksum

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
