"""Explicit synthetic prediction contract validation (never production-eligible by default)."""

from __future__ import annotations

from datetime import datetime

from sports_analytics.predictions.contracts import (
    CanonicalSelectionIdentity,
    MarketPrediction,
    PredictionLineage,
    PredictionQualityFlags,
    SelectionProbability,
    build_market_prediction,
)


def build_synthetic_market_prediction(
    *,
    canonical_event_id: str,
    event_start_utc: datetime,
    predicted_at_utc: datetime,
    feature_available_at_utc: datetime,
    lineage: PredictionLineage,
    probabilities: tuple[SelectionProbability, ...],
    ordered_selection_space: tuple[CanonicalSelectionIdentity, ...] | None = None,
    quality: PredictionQualityFlags | None = None,
) -> MarketPrediction:
    """Validate caller-supplied probabilities without asserting repository verification."""
    resolved_quality = quality or PredictionQualityFlags()
    if resolved_quality.production_eligible:
        raise ValueError(
            "synthetic prediction mode cannot assert production eligibility without verification"
        )
    return build_market_prediction(
        canonical_event_id=canonical_event_id,
        event_start_utc=event_start_utc,
        predicted_at_utc=predicted_at_utc,
        feature_available_at_utc=feature_available_at_utc,
        lineage=lineage,
        probabilities=probabilities,
        ordered_selection_space=ordered_selection_space,
        quality=resolved_quality,
    )
