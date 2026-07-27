"""Same-bookmaker multiples and cross-bookmaker singles comparison.

A bookmaker multiple is valid only when every leg uses quotes from exactly one
bookmaker. Mixed-provider collections must use
:class:`CrossBookmakerSinglesComparison` and are never labelled as multiples.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Final

from sports_analytics.bookmakers.priced_quote import BookmakerPricedQuote
from sports_analytics.bookmakers.types import (
    PROVIDER_BETANO_PT,
    PROVIDER_BETCLIC_PT,
    QuoteSelectionReason,
)
from sports_analytics.bookmakers.verified_evidence import VerifiedBookmakerQuote
from sports_analytics.core.exceptions import PermanentSourceError
from sports_analytics.markets.contracts import validate_decimal_odds
from sports_analytics.sports.contracts import require_utc

MIN_MULTIPLE_LEGS: Final[int] = 2


@dataclass(frozen=True, slots=True)
class BookmakerMultipleLeg:
    """One priced leg belonging to a same-bookmaker multiple."""

    bookmaker_id: str
    canonical_event_id: str
    canonical_market_definition_id: str
    canonical_selection_id: str
    decimal_odds: Decimal
    observed_at_utc: datetime
    leg_key: str

    def __post_init__(self) -> None:
        if self.bookmaker_id not in {PROVIDER_BETANO_PT, PROVIDER_BETCLIC_PT}:
            msg = f"unsupported bookmaker_id: {self.bookmaker_id}"
            raise PermanentSourceError(msg)
        if not self.leg_key.strip():
            msg = "leg_key must be non-empty"
            raise PermanentSourceError(msg)
        object.__setattr__(self, "decimal_odds", validate_decimal_odds(self.decimal_odds))
        object.__setattr__(
            self,
            "observed_at_utc",
            require_utc(self.observed_at_utc, field_name="observed_at_utc"),
        )


@dataclass(frozen=True, slots=True)
class BookmakerMultiple:
    """A multiple bet slip restricted to exactly one bookmaker."""

    bookmaker_id: str
    legs: tuple[BookmakerMultipleLeg, ...]
    total_decimal_odds: Decimal

    def __post_init__(self) -> None:
        if len(self.legs) < MIN_MULTIPLE_LEGS:
            msg = f"bookmaker multiple requires at least {MIN_MULTIPLE_LEGS} legs"
            raise PermanentSourceError(msg)
        bookmakers = {leg.bookmaker_id for leg in self.legs}
        if len(bookmakers) != 1 or self.bookmaker_id not in bookmakers:
            msg = "bookmaker multiple legs must share exactly one bookmaker_id"
            raise PermanentSourceError(msg)
        recomputed = _product(tuple(leg.decimal_odds for leg in self.legs))
        if recomputed != self.total_decimal_odds:
            msg = "total_decimal_odds must equal the product of leg odds"
            raise PermanentSourceError(msg)


@dataclass(frozen=True, slots=True)
class CrossBookmakerSinglesComparison:
    """Per-leg quotes from potentially different bookmakers.

    This is intentionally not a multiple and must never be stored or labelled as
    one. It exposes separate single prices only.
    """

    legs: tuple[tuple[str, BookmakerPricedQuote | None, BookmakerPricedQuote | None], ...]
    reason_code: QuoteSelectionReason = QuoteSelectionReason.BOTH_RETAINED


@dataclass(frozen=True, slots=True)
class RequestedMultipleLegSpec:
    """Identity of one requested multiple leg before provider pricing."""

    leg_key: str
    canonical_event_id: str
    canonical_market_definition_id: str
    canonical_selection_id: str

    def __post_init__(self) -> None:
        if not self.leg_key.strip():
            msg = "leg_key must be non-empty"
            raise PermanentSourceError(msg)


@dataclass(frozen=True, slots=True)
class ProviderMultipleComparison:
    """Comparison of complete same-bookmaker multiples across providers."""

    betano_multiple: BookmakerMultiple | None
    betclic_multiple: BookmakerMultiple | None
    selected_multiple: BookmakerMultiple | None
    reason_code: QuoteSelectionReason
    betano_eligible: bool
    betclic_eligible: bool


def build_same_bookmaker_multiple(
    legs: tuple[BookmakerMultipleLeg, ...] | list[BookmakerMultipleLeg],
) -> BookmakerMultiple:
    """Build a same-bookmaker multiple, rejecting mixed provider IDs."""
    ordered = tuple(legs)
    if len(ordered) < MIN_MULTIPLE_LEGS:
        msg = f"bookmaker multiple requires at least {MIN_MULTIPLE_LEGS} legs"
        raise PermanentSourceError(msg)
    bookmakers = {leg.bookmaker_id for leg in ordered}
    if len(bookmakers) != 1:
        msg = "mixed-provider legs are rejected as a bookmaker multiple"
        raise PermanentSourceError(msg)
    bookmaker_id = next(iter(bookmakers))
    total = _product(tuple(leg.decimal_odds for leg in ordered))
    return BookmakerMultiple(
        bookmaker_id=bookmaker_id,
        legs=ordered,
        total_decimal_odds=total,
    )


def compare_provider_multiples(
    requested_leg_specs: tuple[RequestedMultipleLegSpec, ...] | list[RequestedMultipleLegSpec],
    betano_quotes_by_leg_key: dict[str, VerifiedBookmakerQuote],
    betclic_quotes_by_leg_key: dict[str, VerifiedBookmakerQuote],
    *,
    evaluated_at_utc: datetime,
    quote_maximum_age_seconds: int,
) -> ProviderMultipleComparison:
    """Compare complete Betano-only and Betclic-only multiples.

    Incomplete provider coverage makes that provider ineligible. Equal complete
    totals select Betano. Best individual legs are never mixed across providers.
    """
    evaluated_at = require_utc(evaluated_at_utc, field_name="evaluated_at_utc")
    specs = tuple(requested_leg_specs)
    if len(specs) < MIN_MULTIPLE_LEGS:
        msg = f"requested multiple requires at least {MIN_MULTIPLE_LEGS} legs"
        raise PermanentSourceError(msg)
    keys = [item.leg_key for item in specs]
    if len(keys) != len(set(keys)):
        msg = "requested multiple leg keys must be unique"
        raise PermanentSourceError(msg)
    _reject_duplicate_canonical_identities(specs)
    _reject_mixed_observation_windows(
        betano_quotes_by_leg_key,
        evaluated_at_utc=evaluated_at,
        quote_maximum_age_seconds=quote_maximum_age_seconds,
    )
    _reject_mixed_observation_windows(
        betclic_quotes_by_leg_key,
        evaluated_at_utc=evaluated_at,
        quote_maximum_age_seconds=quote_maximum_age_seconds,
    )

    betano_multiple, betano_eligible = _build_provider_multiple(
        provider_id=PROVIDER_BETANO_PT,
        specs=specs,
        quotes_by_leg_key=betano_quotes_by_leg_key,
        evaluated_at_utc=evaluated_at,
        quote_maximum_age_seconds=quote_maximum_age_seconds,
    )
    betclic_multiple, betclic_eligible = _build_provider_multiple(
        provider_id=PROVIDER_BETCLIC_PT,
        specs=specs,
        quotes_by_leg_key=betclic_quotes_by_leg_key,
        evaluated_at_utc=evaluated_at,
        quote_maximum_age_seconds=quote_maximum_age_seconds,
    )

    if betano_multiple is None and betclic_multiple is None:
        return ProviderMultipleComparison(
            betano_multiple=None,
            betclic_multiple=None,
            selected_multiple=None,
            reason_code=QuoteSelectionReason.INCOMPLETE_MULTIPLE,
            betano_eligible=False,
            betclic_eligible=False,
        )
    if betano_multiple is not None and betclic_multiple is None:
        return ProviderMultipleComparison(
            betano_multiple=betano_multiple,
            betclic_multiple=None,
            selected_multiple=betano_multiple,
            reason_code=QuoteSelectionReason.PREFERRED_ONLY,
            betano_eligible=True,
            betclic_eligible=False,
        )
    if betano_multiple is None and betclic_multiple is not None:
        return ProviderMultipleComparison(
            betano_multiple=None,
            betclic_multiple=betclic_multiple,
            selected_multiple=betclic_multiple,
            reason_code=QuoteSelectionReason.COMPARISON_FALLBACK,
            betano_eligible=False,
            betclic_eligible=True,
        )

    assert betano_multiple is not None and betclic_multiple is not None
    if betclic_multiple.total_decimal_odds > betano_multiple.total_decimal_odds:
        selected = betclic_multiple
        reason = QuoteSelectionReason.HIGHER_ODDS
    elif betano_multiple.total_decimal_odds > betclic_multiple.total_decimal_odds:
        selected = betano_multiple
        reason = QuoteSelectionReason.HIGHER_ODDS
    else:
        selected = betano_multiple
        reason = QuoteSelectionReason.EQUAL_ODDS_PREFERRED
    return ProviderMultipleComparison(
        betano_multiple=betano_multiple,
        betclic_multiple=betclic_multiple,
        selected_multiple=selected,
        reason_code=reason,
        betano_eligible=betano_eligible,
        betclic_eligible=betclic_eligible,
    )


def build_cross_bookmaker_singles_comparison(
    *,
    leg_keys: tuple[str, ...],
    betano_quotes_by_leg_key: dict[str, BookmakerPricedQuote],
    betclic_quotes_by_leg_key: dict[str, BookmakerPricedQuote],
) -> CrossBookmakerSinglesComparison:
    """Retain per-leg singles from both providers without forming a multiple."""
    legs = tuple(
        (
            leg_key,
            betano_quotes_by_leg_key.get(leg_key),
            betclic_quotes_by_leg_key.get(leg_key),
        )
        for leg_key in leg_keys
    )
    return CrossBookmakerSinglesComparison(legs=legs)


def _build_provider_multiple(
    *,
    provider_id: str,
    specs: tuple[RequestedMultipleLegSpec, ...],
    quotes_by_leg_key: dict[str, VerifiedBookmakerQuote],
    evaluated_at_utc: datetime,
    quote_maximum_age_seconds: int,
) -> tuple[BookmakerMultiple | None, bool]:
    legs: list[BookmakerMultipleLeg] = []
    for spec in specs:
        verified = quotes_by_leg_key.get(spec.leg_key)
        if verified is None or verified.provider_id != provider_id:
            return None, False
        quote = verified.to_priced_quote(
            evaluated_at_utc=evaluated_at_utc,
            maximum_age_seconds=quote_maximum_age_seconds,
        )
        if not quote.fresh:
            return None, False
        if (
            quote.canonical_event_id != spec.canonical_event_id
            or quote.canonical_market_definition_id != spec.canonical_market_definition_id
            or quote.canonical_selection_id != spec.canonical_selection_id
        ):
            return None, False
        if quote.market_status != "open" or quote.selection_status != "active":
            return None, False
        legs.append(
            BookmakerMultipleLeg(
                bookmaker_id=provider_id,
                canonical_event_id=spec.canonical_event_id,
                canonical_market_definition_id=spec.canonical_market_definition_id,
                canonical_selection_id=spec.canonical_selection_id,
                decimal_odds=quote.decimal_odds,
                observed_at_utc=quote.observed_at_utc,
                leg_key=spec.leg_key,
            )
        )
    return build_same_bookmaker_multiple(tuple(legs)), True


def _product(values: tuple[Decimal, ...]) -> Decimal:
    total = Decimal("1")
    for value in values:
        total *= value
    return validate_decimal_odds(total)


def _reject_duplicate_canonical_identities(
    specs: tuple[RequestedMultipleLegSpec, ...],
) -> None:
    seen: set[tuple[str, str, str]] = set()
    for spec in specs:
        identity = (
            spec.canonical_event_id,
            spec.canonical_market_definition_id,
            spec.canonical_selection_id,
        )
        if identity in seen:
            msg = "duplicate canonical bet identities are rejected across leg keys"
            raise PermanentSourceError(msg)
        seen.add(identity)


def _reject_mixed_observation_windows(
    quotes_by_leg_key: dict[str, VerifiedBookmakerQuote],
    *,
    evaluated_at_utc: datetime,
    quote_maximum_age_seconds: int,
) -> None:
    observed: set[datetime] = set()
    for verified in quotes_by_leg_key.values():
        quote = verified.to_priced_quote(
            evaluated_at_utc=evaluated_at_utc,
            maximum_age_seconds=quote_maximum_age_seconds,
        )
        if quote.fresh:
            if not quote.snapshot_id or not quote.snapshot_checksum_sha256:
                msg = "verified quote evidence requires snapshot id and checksum"
                raise PermanentSourceError(msg)
        observed.add(quote.observed_at_utc)
    if len(observed) > 1:
        msg = "mixed observation windows are rejected for multiples"
        raise PermanentSourceError(msg)
