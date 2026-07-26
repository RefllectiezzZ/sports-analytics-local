"""Shared quote and prediction decision-time helpers."""

from __future__ import annotations

from datetime import datetime

from sports_analytics.core.exceptions import ValueEvaluationError
from sports_analytics.predictions.contracts import MarketPrediction
from sports_analytics.sports.contracts import require_utc
from sports_analytics.value.contracts import CompleteMarketQuote, QuoteEvaluationMode


def compute_decision_as_of(
    *,
    prediction: MarketPrediction,
    quote: CompleteMarketQuote,
    mode: QuoteEvaluationMode,
) -> datetime:
    """Return the common information cutoff for one prediction/quote pair."""
    if mode is QuoteEvaluationMode.CLOSING_LINE_HISTORICAL_BENCHMARK:
        return prediction.event_start_utc
    timestamps = [prediction.predicted_at_utc, quote.source_observed_at_utc]
    if quote.quoted_at_utc is not None:
        timestamps.append(quote.quoted_at_utc)
    return max(timestamps)


def validate_live_decision_timing(
    *,
    prediction: MarketPrediction,
    quote: CompleteMarketQuote,
    mode: QuoteEvaluationMode,
) -> datetime:
    """Validate live-safe timing and quote validity at the common decision time."""
    decision_as_of = compute_decision_as_of(prediction=prediction, quote=quote, mode=mode)
    if mode is QuoteEvaluationMode.CLOSING_LINE_HISTORICAL_BENCHMARK:
        return decision_as_of
    decision_as_of = require_utc(decision_as_of, field_name="decision_as_of")
    if decision_as_of >= prediction.event_start_utc:
        raise ValueEvaluationError(
            "max(prediction time, quote time, observation time) must be strictly before event start"
        )
    if quote.quote_valid_from_utc is not None and decision_as_of < quote.quote_valid_from_utc:
        raise ValueEvaluationError("quote was not yet valid at decision time")
    if quote.quote_valid_to_utc is not None and decision_as_of > quote.quote_valid_to_utc:
        raise ValueEvaluationError("quote had expired before decision time")
    return decision_as_of
