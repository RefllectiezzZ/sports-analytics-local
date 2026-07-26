"""Shared sport domain enumerations."""

from __future__ import annotations

from enum import StrEnum


class SportCode(StrEnum):
    """Supported sport codes for canonical datasets."""

    FOOTBALL = "football"
    BASKETBALL = "basketball"
    TENNIS = "tennis"


class CompetitionType(StrEnum):
    """Canonical competition classification."""

    DOMESTIC_LEAGUE = "domestic-league"
