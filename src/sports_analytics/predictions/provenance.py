"""Closed prediction provenance values for PR #8."""

from __future__ import annotations

from enum import StrEnum

from sports_analytics.core.exceptions import PredictionError


class PredictionProvenance(StrEnum):
    """Explicit non-live provenance labels supported in PR #8."""

    HISTORICAL_REPLAY = "historical-replay"
    SYNTHETIC_CONTRACT = "synthetic-contract"


def parse_prediction_provenance(
    value: object, *, field_name: str = "provenance"
) -> PredictionProvenance:
    """Parse one closed provenance value without coercion."""
    if type(value) is not str or not value:
        raise PredictionError(f"{field_name} must be a non-empty JSON string")
    try:
        return PredictionProvenance(value)
    except ValueError as exc:
        raise PredictionError(
            f"{field_name} must be one of: {', '.join(item.value for item in PredictionProvenance)}"
        ) from exc
