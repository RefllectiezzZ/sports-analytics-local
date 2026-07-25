"""Complete-market quote and model-value contracts."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Final

from sports_analytics.core.exceptions import PredictionError, ValueEvaluationError
from sports_analytics.markets.contracts import (
    OddsQuote,
    ProviderType,
    QuotePhase,
    QuoteTimestampPrecision,
    validate_decimal_odds,
)
from sports_analytics.predictions.contracts import (
    CanonicalSelectionIdentity,
    MarketPrediction,
)
from sports_analytics.sports.contracts import require_utc, validate_domain_identifier

VALUE_EVALUATION_VERSION: Final[str] = "complete-market-value-v1"


class QuoteEvaluationMode(StrEnum):
    """Whether a quote is actionable or only an historical benchmark."""

    LIVE_SAFE = "live-safe"
    CLOSING_LINE_HISTORICAL_BENCHMARK = "closing-line-historical-benchmark"


@dataclass(frozen=True, slots=True)
class PricedSelection:
    """One quote observation mapped to canonical selection identity."""

    selection: CanonicalSelectionIdentity
    decimal_odds: Decimal
    quote_series_id: str
    quote_observation_id: str

    def __post_init__(self) -> None:
        try:
            object.__setattr__(
                self,
                "decimal_odds",
                validate_decimal_odds(self.decimal_odds),
            )
        except Exception as exc:
            raise ValueEvaluationError(str(exc)) from exc
        if not self.quote_series_id or not self.quote_observation_id:
            raise ValueEvaluationError("quote identities must be non-empty")


@dataclass(frozen=True, slots=True)
class CompleteMarketQuote:
    """All outcomes of one provider market observed through one source."""

    canonical_event_id: str
    source_name: str
    provider_type: str
    provider_id: str
    quote_phase: str
    source_observed_at_utc: datetime
    quoted_at_utc: datetime | None
    quote_timestamp_precision: str
    quote_valid_from_utc: datetime | None
    quote_valid_to_utc: datetime | None
    selections: tuple[PricedSelection, ...]

    def __post_init__(self) -> None:
        if not self.canonical_event_id or not self.source_name:
            raise ValueEvaluationError("event and source identities must be non-empty")
        if not self.provider_type or not self.provider_id:
            raise ValueEvaluationError("provider type and provider id must be explicit")
        try:
            validate_domain_identifier(self.source_name, field_name="source_name")
            validate_domain_identifier(self.provider_id, field_name="provider_id")
            ProviderType(self.provider_type)
            QuotePhase(self.quote_phase)
            QuoteTimestampPrecision(self.quote_timestamp_precision)
        except Exception as exc:
            raise ValueEvaluationError(f"invalid quote identity or taxonomy: {exc}") from exc
        object.__setattr__(
            self,
            "source_observed_at_utc",
            _utc(self.source_observed_at_utc, "source_observed_at_utc"),
        )
        if self.quoted_at_utc is not None:
            object.__setattr__(
                self,
                "quoted_at_utc",
                _utc(self.quoted_at_utc, "quoted_at_utc"),
            )
        if (
            self.quote_timestamp_precision
            in {QuoteTimestampPrecision.EXACT.value, QuoteTimestampPrecision.MINUTE.value}
            and self.quoted_at_utc is None
        ):
            raise ValueEvaluationError("known quote timestamp precision requires quoted_at_utc")
        if (
            self.quote_timestamp_precision
            == QuoteTimestampPrecision.SNAPSHOT_OBSERVATION_ONLY.value
            and self.quoted_at_utc is not None
        ):
            raise ValueEvaluationError("observation-only precision must not carry quoted_at_utc")
        if self.quote_valid_from_utc is not None:
            object.__setattr__(
                self,
                "quote_valid_from_utc",
                _utc(self.quote_valid_from_utc, "quote_valid_from_utc"),
            )
        if self.quote_valid_to_utc is not None:
            object.__setattr__(
                self,
                "quote_valid_to_utc",
                _utc(self.quote_valid_to_utc, "quote_valid_to_utc"),
            )
        if not self.selections:
            raise ValueEvaluationError("a complete market quote cannot be empty")
        selection_ids = tuple(item.selection.selection_id for item in self.selections)
        if len(selection_ids) != len(set(selection_ids)):
            raise ValueEvaluationError("complete market quote contains duplicate selections")
        if len({item.selection.market_identity for item in self.selections}) != 1:
            raise ValueEvaluationError("quote selections do not describe one canonical market")
        if (
            self.quote_valid_from_utc is not None
            and self.quote_valid_to_utc is not None
            and self.quote_valid_to_utc < self.quote_valid_from_utc
        ):
            raise ValueEvaluationError("quote validity end precedes validity start")

    @property
    def sport_code(self) -> str:
        return self.selections[0].selection.sport_code

    @property
    def market_key(self) -> str:
        return self.selections[0].selection.market_key


@dataclass(frozen=True, slots=True)
class SelectionValue:
    """Model versus market value calculation for one selection."""

    selection: CanonicalSelectionIdentity
    model_probability: float
    decimal_odds: Decimal
    raw_implied_probability: float
    normalized_implied_probability: float
    edge: float
    expected_value: float


@dataclass(frozen=True, slots=True)
class MarketValueEvaluation:
    """Value calculations for a complete prediction and complete quote."""

    evaluation_version: str
    mode: QuoteEvaluationMode
    prediction: MarketPrediction
    quote: CompleteMarketQuote
    overround: float
    selections: tuple[SelectionValue, ...]
    warning: str | None

    @property
    def is_actionable(self) -> bool:
        return self.mode is QuoteEvaluationMode.LIVE_SAFE


def complete_market_quote_from_odds_quotes(
    quotes: tuple[OddsQuote, ...],
) -> CompleteMarketQuote:
    """Build a complete quote while rejecting mixed source/provider observations."""
    if not quotes:
        raise ValueEvaluationError("cannot build a complete market quote from no quotes")
    first = quotes[0]
    expected = (
        first.canonical_event_id,
        first.source_name,
        first.provider_type,
        first.provider_id,
        first.quote_phase,
        first.source_observed_at_utc,
        first.quoted_at_utc,
        first.quote_timestamp_precision,
        first.quote_valid_from_utc,
        first.quote_valid_to_utc,
    )
    priced: list[PricedSelection] = []
    for quote in quotes:
        actual = (
            quote.canonical_event_id,
            quote.source_name,
            quote.provider_type,
            quote.provider_id,
            quote.quote_phase,
            quote.source_observed_at_utc,
            quote.quoted_at_utc,
            quote.quote_timestamp_precision,
            quote.quote_valid_from_utc,
            quote.quote_valid_to_utc,
        )
        if actual != expected:
            raise ValueEvaluationError(
                "complete market quote must not mix events, sources, providers, or timestamps"
            )
        priced.append(
            PricedSelection(
                selection=CanonicalSelectionIdentity.from_selection(quote.selection),
                decimal_odds=quote.decimal_odds,
                quote_series_id=quote.quote_series_id,
                quote_observation_id=quote.quote_observation_id,
            )
        )
    return CompleteMarketQuote(
        canonical_event_id=first.canonical_event_id,
        source_name=first.source_name,
        provider_type=first.provider_type,
        provider_id=first.provider_id,
        quote_phase=first.quote_phase,
        source_observed_at_utc=first.source_observed_at_utc,
        quoted_at_utc=first.quoted_at_utc,
        quote_timestamp_precision=first.quote_timestamp_precision,
        quote_valid_from_utc=first.quote_valid_from_utc,
        quote_valid_to_utc=first.quote_valid_to_utc,
        selections=tuple(sorted(priced, key=lambda item: item.selection.selection_id)),
    )


def evaluate_complete_market(
    *,
    prediction: MarketPrediction,
    quote: CompleteMarketQuote,
    mode: QuoteEvaluationMode,
) -> MarketValueEvaluation:
    """Compute raw implied probability, overround, normalized edge, and EV."""
    if prediction.canonical_event_id != quote.canonical_event_id:
        raise ValueEvaluationError("prediction and quote canonical event ids differ")
    predicted = {item.selection.selection_id: item for item in prediction.probabilities}
    priced = {item.selection.selection_id: item for item in quote.selections}
    if set(predicted) != set(priced):
        missing = sorted(set(predicted) - set(priced))
        extra = sorted(set(priced) - set(predicted))
        raise ValueEvaluationError(
            f"quote must exactly cover the predicted outcome space; missing={missing} extra={extra}"
        )
    _validate_quote_timing(prediction=prediction, quote=quote, mode=mode)
    raw = {key: 1.0 / float(item.decimal_odds) for key, item in priced.items()}
    implied_total = math.fsum(raw.values())
    if not math.isfinite(implied_total) or implied_total <= 0.0:
        raise ValueEvaluationError("complete market implied probability total is invalid")
    overround = implied_total - 1.0
    values = tuple(
        SelectionValue(
            selection=predicted[key].selection,
            model_probability=predicted[key].probability,
            decimal_odds=priced[key].decimal_odds,
            raw_implied_probability=raw[key],
            normalized_implied_probability=raw[key] / implied_total,
            edge=predicted[key].probability - (raw[key] / implied_total),
            expected_value=(predicted[key].probability * float(priced[key].decimal_odds)) - 1.0,
        )
        for key in sorted(predicted)
    )
    warning = None
    if mode is QuoteEvaluationMode.CLOSING_LINE_HISTORICAL_BENCHMARK:
        warning = (
            "Historical closing-line benchmark only; quote availability before kickoff "
            "is not claimed."
        )
    return MarketValueEvaluation(
        evaluation_version=VALUE_EVALUATION_VERSION,
        mode=mode,
        prediction=prediction,
        quote=quote,
        overround=overround,
        selections=values,
        warning=warning,
    )


def _validate_quote_timing(
    *,
    prediction: MarketPrediction,
    quote: CompleteMarketQuote,
    mode: QuoteEvaluationMode,
) -> None:
    if mode is QuoteEvaluationMode.CLOSING_LINE_HISTORICAL_BENCHMARK:
        if quote.quote_phase != QuotePhase.CLOSING.value:
            raise ValueEvaluationError("historical closing-line mode requires closing quotes")
        return
    if quote.quote_phase == QuotePhase.CLOSING.value:
        raise ValueEvaluationError("closing quotes are not accepted as live-safe prices")
    if quote.quote_timestamp_precision not in {
        QuoteTimestampPrecision.EXACT.value,
        QuoteTimestampPrecision.MINUTE.value,
    }:
        raise ValueEvaluationError("live-safe evaluation requires a provider quote timestamp")
    if quote.quoted_at_utc is None:
        raise ValueEvaluationError("live-safe evaluation requires quoted_at_utc")
    from sports_analytics.value.timing import validate_live_decision_timing

    validate_live_decision_timing(prediction=prediction, quote=quote, mode=mode)


def _utc(value: datetime, field_name: str) -> datetime:
    try:
        return require_utc(value, field_name=field_name)
    except Exception as exc:
        if isinstance(exc, (PredictionError, ValueEvaluationError)):
            raise
        raise ValueEvaluationError(str(exc)) from exc
