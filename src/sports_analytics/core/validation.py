"""Shared numeric and duration validation helpers."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from math import isfinite
from typing import Final

from sports_analytics.core.exceptions import RepositoryError

# Bounded so timedelta, Event.wait, sleep, join, and subprocess timeouts stay usable.
# 30 days stays under common Windows wait-object millisecond limits (~49.7 days).
MAX_DURATION_SECONDS: Final[float] = 60.0 * 60.0 * 24.0 * 30.0
_POSITIVE_DECIMAL_INT_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[1-9][0-9]*$")


def validate_positive_finite_number(
    value: object,
    *,
    field_name: str,
    maximum: float | None = None,
) -> float:
    """Require a strict positive finite number (reject bool, NaN, and infinities).

    Converts conversion and overflow failures into ``RepositoryError`` so callers
    never observe bare ``OverflowError``, ``ValueError``, or ``TypeError``.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        msg = f"{field_name} must be a positive finite number"
        raise RepositoryError(msg)
    try:
        number = float(value)
    except (OverflowError, ValueError, TypeError) as exc:
        msg = f"{field_name} must be a positive finite number"
        raise RepositoryError(msg) from exc
    if not isfinite(number) or number <= 0:
        msg = f"{field_name} must be a positive finite number"
        raise RepositoryError(msg)
    if maximum is not None:
        try:
            maximum_number = float(maximum)
        except (OverflowError, ValueError, TypeError) as exc:
            msg = f"{field_name} maximum bound is invalid"
            raise RepositoryError(msg) from exc
        if not isfinite(maximum_number) or maximum_number <= 0:
            msg = f"{field_name} maximum bound must be a positive finite number"
            raise RepositoryError(msg)
        if number > maximum_number:
            msg = f"{field_name} must be <= {maximum_number}"
            raise RepositoryError(msg)
    return number


def validate_positive_duration_seconds(value: object, *, field_name: str) -> float:
    """Require a positive finite duration usable with timedelta and wait timeouts.

    Rejects values that are mathematically finite but cannot safely construct a
    ``timedelta`` or participate in datetime arithmetic within
    ``MAX_DURATION_SECONDS`` (30 days).
    """
    number = validate_positive_finite_number(
        value,
        field_name=field_name,
        maximum=MAX_DURATION_SECONDS,
    )
    try:
        delta = timedelta(seconds=number)
        probe = datetime(2000, 1, 1, tzinfo=UTC)
        _ = probe + delta
        _ = probe - delta
    except (OverflowError, ValueError) as exc:
        msg = f"{field_name} is not a representable positive duration"
        raise RepositoryError(msg) from exc
    return number


def parse_positive_decimal_int(value: object, *, field_name: str) -> int:
    """Parse a strict positive decimal integer from an int or canonical digit string.

    Accepts only real ``int`` values greater than zero, or strings matching
    ``^[1-9][0-9]*$`` (no sign, whitespace, leading zeros, floats, or scientific
    notation). Rejects ``bool``.
    """
    if isinstance(value, bool):
        msg = f"{field_name} must be a positive integer"
        raise RepositoryError(msg)
    if type(value) is int:
        if value < 1:
            msg = f"{field_name} must be a positive integer"
            raise RepositoryError(msg)
        return value
    if isinstance(value, str):
        if _POSITIVE_DECIMAL_INT_PATTERN.fullmatch(value) is None:
            msg = f"{field_name} must be a positive integer"
            raise RepositoryError(msg)
        return int(value, 10)
    msg = f"{field_name} must be a positive integer"
    raise RepositoryError(msg)
