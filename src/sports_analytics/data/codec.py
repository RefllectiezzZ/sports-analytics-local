"""Canonical JSON and UTC timestamp helpers for operational persistence."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from typing import Any

from sports_analytics.core.exceptions import RepositoryError
from sports_analytics.data.types import JsonValue

_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


def dumps_canonical_json(value: JsonValue) -> str:
    """Serialize a JSON-compatible value to deterministic UTF-8 text.

    Keys are sorted, separators are compact, non-ASCII is preserved, and
    NaN/Infinity are rejected.
    """
    try:
        _reject_non_finite_numbers(value)
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        msg = f"value is not JSON-serializable for canonical storage: {exc}"
        raise RepositoryError(msg) from exc


def loads_canonical_json(text: str) -> JsonValue:
    """Parse stored JSON text into a JSON-compatible structure."""
    try:
        loaded: object = json.loads(text)
    except json.JSONDecodeError as exc:
        msg = f"stored JSON is malformed: {exc.msg}"
        raise RepositoryError(msg) from exc
    return _as_json_value(loaded)


def format_utc_timestamp(value: datetime) -> str:
    """Serialize a timezone-aware datetime as canonical UTC text."""
    if value.tzinfo is None:
        msg = "timestamp must be timezone-aware; naive datetimes are rejected"
        raise ValueError(msg)
    utc_value = value.astimezone(UTC)
    return utc_value.strftime(_TIMESTAMP_FORMAT)


def parse_utc_timestamp(text: str) -> datetime:
    """Parse a canonical UTC timestamp into an aware UTC datetime."""
    if not isinstance(text, str) or not text.endswith("Z"):
        msg = f"timestamp must use canonical UTC format ending with Z, got {text!r}"
        raise ValueError(msg)
    try:
        parsed = datetime.strptime(text, _TIMESTAMP_FORMAT).replace(tzinfo=UTC)
    except ValueError as exc:
        msg = f"malformed canonical UTC timestamp: {text!r}"
        raise ValueError(msg) from exc
    return parsed


def utc_now() -> datetime:
    """Return the current timezone-aware UTC datetime."""
    return datetime.now(tz=UTC)


def _reject_non_finite_numbers(value: object) -> None:
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        msg = "NaN and Infinity are not allowed in canonical JSON"
        raise ValueError(msg)
    if isinstance(value, dict):
        for item in value.values():
            _reject_non_finite_numbers(item)
    elif isinstance(value, list):
        for item in value:
            _reject_non_finite_numbers(item)


def _as_json_value(value: object) -> JsonValue:
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            msg = "NaN and Infinity are not allowed in canonical JSON"
            raise RepositoryError(msg)
        return value
    if isinstance(value, list):
        return [_as_json_value(item) for item in value]
    if isinstance(value, dict):
        result: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                msg = "JSON object keys must be strings"
                raise RepositoryError(msg)
            result[key] = _as_json_value(item)
        return result
    msg = f"unsupported JSON value type: {type(value).__name__}"
    raise RepositoryError(msg)


def ensure_json_value(value: Any) -> JsonValue:
    """Validate that ``value`` is JSON-compatible without silent coercion."""
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            msg = "NaN and Infinity are not allowed in canonical JSON"
            raise RepositoryError(msg)
        return value
    if isinstance(value, list):
        return [ensure_json_value(item) for item in value]
    if isinstance(value, dict):
        result: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                msg = "JSON object keys must be strings"
                raise RepositoryError(msg)
            result[key] = ensure_json_value(item)
        return result
    msg = f"unsupported JSON value type: {type(value).__name__}"
    raise RepositoryError(msg)
