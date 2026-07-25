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
# Conservative local-SQLite recovery batch for short BEGIN IMMEDIATE transactions.
MAX_RECOVERY_BATCH_SIZE: Final[int] = 5_000
# Reject digit strings longer than this before calling int() to avoid conversion traps.
_MAX_POSITIVE_DECIMAL_DIGITS: Final[int] = 64
_POSITIVE_DECIMAL_INT_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[1-9][0-9]*$")
# Canonical decimal int for CLI priority: optional leading minus, no plus/whitespace/leading zeros.
_BOUNDED_DECIMAL_INT_PATTERN: Final[re.Pattern[str]] = re.compile(r"^(0|-?[1-9][0-9]*)$")
# Conservative signed 32-bit bound keeps priorities SQLite-safe and UI/tooling friendly.
MAX_CLI_PRIORITY: Final[int] = 2_147_483_647
MIN_CLI_PRIORITY: Final[int] = -2_147_483_648
# Conservative maximum attempts bound for local SQLite job rows.
MAX_CLI_MAXIMUM_ATTEMPTS: Final[int] = 1_000
_MAX_BOUNDED_DECIMAL_DIGITS: Final[int] = 16


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


def add_duration(value: datetime, seconds: object, *, field_name: str) -> datetime:
    """Add a validated positive duration to a timezone-aware datetime.

    Raises ``RepositoryError`` for naive timestamps, invalid durations, and
    datetime-range overflow. Does not clamp.
    """
    if not isinstance(value, datetime):
        msg = f"{field_name} requires a timezone-aware datetime"
        raise RepositoryError(msg)
    if value.tzinfo is None:
        msg = f"{field_name} requires a timezone-aware datetime"
        raise RepositoryError(msg)
    duration = validate_positive_duration_seconds(seconds, field_name=field_name)
    try:
        return value + timedelta(seconds=duration)
    except (OverflowError, ValueError) as exc:
        msg = f"{field_name} overflows datetime range"
        raise RepositoryError(msg) from exc


def subtract_duration(value: datetime, seconds: object, *, field_name: str) -> datetime:
    """Subtract a validated positive duration from a timezone-aware datetime.

    Raises ``RepositoryError`` for naive timestamps, invalid durations, and
    datetime-range underflow. Does not clamp.
    """
    if not isinstance(value, datetime):
        msg = f"{field_name} requires a timezone-aware datetime"
        raise RepositoryError(msg)
    if value.tzinfo is None:
        msg = f"{field_name} requires a timezone-aware datetime"
        raise RepositoryError(msg)
    duration = validate_positive_duration_seconds(seconds, field_name=field_name)
    try:
        return value - timedelta(seconds=duration)
    except (OverflowError, ValueError) as exc:
        msg = f"{field_name} underflows datetime range"
        raise RepositoryError(msg) from exc


def parse_positive_decimal_int(
    value: object,
    *,
    field_name: str,
    maximum: int | None = None,
) -> int:
    """Parse a strict positive decimal integer from an int or canonical digit string.

    Accepts only real ``int`` values greater than zero, or strings matching
    ``^[1-9][0-9]*$`` (no sign, whitespace, leading zeros, floats, or scientific
    notation). Rejects ``bool``. When ``maximum`` is set, values above that bound
    are rejected. Digit strings are length-checked before ``int()`` so Python's
    integer-string conversion limit cannot leak as ``ValueError``.
    """
    if maximum is not None and (
        type(maximum) is not int or isinstance(maximum, bool) or maximum < 1
    ):
        msg = f"{field_name} maximum bound must be a positive int"
        raise RepositoryError(msg)
    if isinstance(value, bool):
        msg = f"{field_name} must be a positive integer"
        raise RepositoryError(msg)
    if type(value) is int:
        if value < 1:
            msg = f"{field_name} must be a positive integer"
            raise RepositoryError(msg)
        if maximum is not None and value > maximum:
            msg = f"{field_name} must be <= {maximum}"
            raise RepositoryError(msg)
        return value
    if isinstance(value, str):
        if _POSITIVE_DECIMAL_INT_PATTERN.fullmatch(value) is None:
            msg = f"{field_name} must be a positive integer"
            raise RepositoryError(msg)
        max_digits = len(str(maximum)) if maximum is not None else _MAX_POSITIVE_DECIMAL_DIGITS
        if len(value) > max_digits:
            msg = f"{field_name} must be a positive integer"
            raise RepositoryError(msg)
        try:
            parsed = int(value, 10)
        except ValueError as exc:
            msg = f"{field_name} must be a positive integer"
            raise RepositoryError(msg) from exc
        if maximum is not None and parsed > maximum:
            msg = f"{field_name} must be <= {maximum}"
            raise RepositoryError(msg)
        return parsed
    msg = f"{field_name} must be a positive integer"
    raise RepositoryError(msg)


def parse_cli_bounded_int(
    value: object,
    *,
    field_name: str,
    minimum: int = MIN_CLI_PRIORITY,
    maximum: int = MAX_CLI_PRIORITY,
) -> int:
    """Parse a CLI priority-style integer from an int or canonical decimal string.

    Policy:
    - accepts a real ``int`` (not ``bool``) within ``[minimum, maximum]``;
    - accepts canonical decimal strings matching ``^(0|-?[1-9][0-9]*)$``;
    - rejects whitespace, plus signs, leading zeros (except literal ``0``),
      floats, scientific notation, and over-long digit strings before ``int()``.
    """
    if isinstance(value, bool):
        msg = f"{field_name} must be a bounded integer"
        raise RepositoryError(msg)
    if type(value) is int:
        if value < minimum or value > maximum:
            msg = f"{field_name} must be between {minimum} and {maximum}"
            raise RepositoryError(msg)
        return value
    if not isinstance(value, str):
        msg = f"{field_name} must be a bounded integer"
        raise RepositoryError(msg)
    if _BOUNDED_DECIMAL_INT_PATTERN.fullmatch(value) is None:
        msg = f"{field_name} must be a bounded integer"
        raise RepositoryError(msg)
    digits = value[1:] if value.startswith("-") else value
    if len(digits) > _MAX_BOUNDED_DECIMAL_DIGITS:
        msg = f"{field_name} must be a bounded integer"
        raise RepositoryError(msg)
    try:
        parsed = int(value, 10)
    except ValueError as exc:
        msg = f"{field_name} must be a bounded integer"
        raise RepositoryError(msg) from exc
    if parsed < minimum or parsed > maximum:
        msg = f"{field_name} must be between {minimum} and {maximum}"
        raise RepositoryError(msg)
    return parsed


def parse_cli_positive_bounded_int(
    value: object,
    *,
    field_name: str,
    maximum: int = MAX_CLI_MAXIMUM_ATTEMPTS,
) -> int:
    """Parse a strict positive CLI integer with a conservative upper bound."""
    return parse_positive_decimal_int(value, field_name=field_name, maximum=maximum)
