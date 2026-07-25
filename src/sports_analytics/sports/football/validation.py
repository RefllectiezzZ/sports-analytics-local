"""Football field validation helpers for normalization."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Final

from sports_analytics.core.exceptions import NormalizationError

MAX_TEAM_NAME_LENGTH: Final[int] = 128
MAX_REFEREE_LENGTH: Final[int] = 128
MAX_GOALS: Final[int] = 99
MAX_SHOTS: Final[int] = 99
MAX_CORNERS: Final[int] = 99
MAX_FOULS: Final[int] = 99
MAX_CARDS: Final[int] = 99
MIN_DECIMAL_ODDS: Final[Decimal] = Decimal("1.01")
MAX_DECIMAL_ODDS: Final[Decimal] = Decimal("1000.00")

_RESULT_CODES: Final[dict[str, str]] = {"H": "home", "D": "draw", "A": "away"}


def map_result_code(value: str, *, field_name: str) -> str:
    """Map Football-Data FTR/HTR codes to canonical result labels."""
    if value not in _RESULT_CODES:
        msg = f"{field_name} must be one of H, D, A"
        raise NormalizationError(msg)
    return _RESULT_CODES[value]


def expected_result_from_goals(home_goals: int, away_goals: int) -> str:
    """Return the canonical result implied by goal counts."""
    if home_goals > away_goals:
        return "home"
    if home_goals < away_goals:
        return "away"
    return "draw"


def parse_optional_int(
    value: str,
    *,
    field_name: str,
    minimum: int = 0,
    maximum: int,
) -> int | None:
    """Parse an optional non-negative integer field from CSV text."""
    text = value.strip()
    if text == "":
        return None
    if text.lower() in {"true", "false"}:
        msg = f"{field_name} must not be a boolean"
        raise NormalizationError(msg)
    if any(ch in text for ch in {".", "e", "E", "+", "-"}) and not (
        text.isdigit() or (text.startswith("-") and text[1:].isdigit())
    ):
        # Reject floats/scientific notation; allow only optional leading minus digits.
        if not (text.startswith("-") and text[1:].isdigit()):
            msg = f"{field_name} must be an integer"
            raise NormalizationError(msg)
    if not (text.isdigit() or (text.startswith("-") and text[1:].isdigit())):
        msg = f"{field_name} must be an integer"
        raise NormalizationError(msg)
    number = int(text, 10)
    if number < minimum:
        msg = f"{field_name} must be >= {minimum}"
        raise NormalizationError(msg)
    if number > maximum:
        msg = f"{field_name} must be <= {maximum}"
        raise NormalizationError(msg)
    return number


def parse_required_pair(
    home_raw: str,
    away_raw: str,
    *,
    home_field: str,
    away_field: str,
    maximum: int,
) -> tuple[int | None, int | None]:
    """Parse a home/away statistic pair, rejecting partial presence."""
    home = parse_optional_int(home_raw, field_name=home_field, maximum=maximum)
    away = parse_optional_int(away_raw, field_name=away_field, maximum=maximum)
    if (home is None) != (away is None):
        msg = f"{home_field}/{away_field} must both be present or both empty"
        raise NormalizationError(msg)
    return home, away


def parse_decimal_odds(value: str, *, field_name: str) -> Decimal | None:
    """Parse optional decimal odds using Decimal, not binary floats."""
    text = value.strip()
    if text == "":
        return None
    lowered = text.lower()
    if lowered in {"nan", "inf", "+inf", "-inf", "infinity", "+infinity", "-infinity"}:
        msg = f"{field_name} must be a finite decimal odds value"
        raise NormalizationError(msg)
    if lowered in {"true", "false"}:
        msg = f"{field_name} must not be a boolean"
        raise NormalizationError(msg)
    if "," in text:
        msg = f"{field_name} must not use comma decimal separators"
        raise NormalizationError(msg)
    try:
        odds = Decimal(text)
    except InvalidOperation as exc:
        msg = f"{field_name} is not a valid decimal odds value"
        raise NormalizationError(msg) from exc
    if not odds.is_finite():
        msg = f"{field_name} must be a finite decimal odds value"
        raise NormalizationError(msg)
    if odds <= Decimal("1"):
        msg = f"{field_name} must be greater than 1"
        raise NormalizationError(msg)
    if odds < MIN_DECIMAL_ODDS or odds > MAX_DECIMAL_ODDS:
        msg = (
            f"{field_name} must be between {MIN_DECIMAL_ODDS} and "
            f"{MAX_DECIMAL_ODDS} inclusive"
        )
        raise NormalizationError(msg)
    # Quantize to schema scale without silent binary float rounding.
    quantized = odds.quantize(Decimal("0.0001"))
    return quantized


def require_aware_utc(value: datetime, *, field_name: str) -> datetime:
    """Require a timezone-aware datetime (UTC preferred)."""
    if value.tzinfo is None:
        msg = f"{field_name} must be timezone-aware"
        raise NormalizationError(msg)
    return value


def require_date(value: date, *, field_name: str) -> date:
    """Require a ``date`` instance."""
    if type(value) is not date:
        msg = f"{field_name} must be a date"
        raise NormalizationError(msg)
    return value
