"""Betano-preferred / Betclic-comparison quote selection policy."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from sports_analytics.bookmakers.types import (
    BOOKMAKER_SELECTION_POLICY_ID,
    DEFAULT_QUOTE_MAXIMUM_AGE_SECONDS,
    PROVIDER_BETANO_PT,
    PROVIDER_BETCLIC_PT,
    QUOTE_EQUIVALENCE_POLICY_ID,
    QuoteSelectionReason,
    SelectionMode,
)
from sports_analytics.core.exceptions import PermanentSourceError
from sports_analytics.markets.contracts import validate_decimal_odds
from sports_analytics.sports.contracts import require_utc


@dataclass(frozen=True, slots=True)
class BookmakerSelectionPolicy:
    """Typed policy controlling preferred vs comparison bookmaker selection."""

    preferred_bookmaker: str = PROVIDER_BETANO_PT
    comparison_bookmaker: str = PROVIDER_BETCLIC_PT
    selection_mode: SelectionMode = SelectionMode.PREFERRED_UNLESS_BETTER
    quote_maximum_age_seconds: int = DEFAULT_QUOTE_MAXIMUM_AGE_SECONDS
    policy_id: str = BOOKMAKER_SELECTION_POLICY_ID
    equivalence_policy_id: str = QUOTE_EQUIVALENCE_POLICY_ID

    def __post_init__(self) -> None:
        if self.preferred_bookmaker != PROVIDER_BETANO_PT:
            msg = "preferred_bookmaker must be betano-pt for PR #11"
            raise PermanentSourceError(msg)
        if self.comparison_bookmaker != PROVIDER_BETCLIC_PT:
            msg = "comparison_bookmaker must be betclic-pt for PR #11"
            raise PermanentSourceError(msg)
        if self.quote_maximum_age_seconds < 0:
            msg = "quote_maximum_age_seconds must be non-negative"
            raise PermanentSourceError(msg)


DEFAULT_BOOKMAKER_SELECTION_POLICY: BookmakerSelectionPolicy = BookmakerSelectionPolicy()


@dataclass(frozen=True, slots=True)
class QuoteEquivalenceIdentity:
    """Verified identity for comparing two bookmaker quotes as the same bet."""

    canonical_event_id: str
    canonical_market_definition_id: str
    canonical_selection_id: str
    line: str | None = None
    period: str | None = None
    participant_scope: str | None = None
    overtime_scope: str | None = None
    rules_scope: str | None = None
    pre_match_state: str = "pre-match"
    comparison_policy_version: str = QUOTE_EQUIVALENCE_POLICY_ID


@dataclass(frozen=True, slots=True)
class BookmakerPricedQuote:
    """One priced selection from exactly one bookmaker provider."""

    provider_id: str
    decimal_odds: Decimal
    observed_at_utc: datetime
    canonical_event_id: str
    canonical_market_definition_id: str
    canonical_selection_id: str
    fresh: bool
    line: str | None = None
    period: str | None = None
    participant_scope: str | None = None
    overtime_scope: str | None = None
    rules_scope: str | None = None
    market_status: str = "open"
    selection_status: str = "active"
    snapshot_id: str | None = None
    snapshot_checksum_sha256: str | None = None

    def equivalence_identity(self) -> QuoteEquivalenceIdentity:
        return QuoteEquivalenceIdentity(
            canonical_event_id=self.canonical_event_id,
            canonical_market_definition_id=self.canonical_market_definition_id,
            canonical_selection_id=self.canonical_selection_id,
            line=self.line,
            period=self.period,
            participant_scope=self.participant_scope,
            overtime_scope=self.overtime_scope,
            rules_scope=self.rules_scope,
        )

    def __post_init__(self) -> None:
        object.__setattr__(self, "decimal_odds", validate_decimal_odds(self.decimal_odds))
        object.__setattr__(
            self,
            "observed_at_utc",
            require_utc(self.observed_at_utc, field_name="observed_at_utc"),
        )
        if self.provider_id not in {PROVIDER_BETANO_PT, PROVIDER_BETCLIC_PT}:
            msg = f"unsupported bookmaker provider_id: {self.provider_id}"
            raise PermanentSourceError(msg)
        if self.market_status != "open":
            msg = "only open markets may be selected for comparison"
            raise PermanentSourceError(msg)
        if self.selection_status != "active":
            msg = "only active selections may be selected for comparison"
            raise PermanentSourceError(msg)


def quotes_are_equivalent(
    left: BookmakerPricedQuote,
    right: BookmakerPricedQuote,
    *,
    policy: BookmakerSelectionPolicy,
) -> bool:
    if left.equivalence_identity() != right.equivalence_identity():
        return False
    return left.equivalence_identity().comparison_policy_version == policy.equivalence_policy_id


def quote_is_fresh_at(
    quote: BookmakerPricedQuote,
    *,
    compared_at: datetime,
    maximum_age_seconds: int,
) -> bool:
    observed = require_utc(quote.observed_at_utc, field_name="observed_at_utc")
    current = require_utc(compared_at, field_name="compared_at")
    if observed > current:
        return False
    age = current - observed
    return timedelta(seconds=0) <= age <= timedelta(seconds=maximum_age_seconds)


@dataclass(frozen=True, slots=True)
class BookmakerQuoteComparison:
    """Full auditable comparison between preferred and comparison quotes."""

    selected_bookmaker_id: str | None
    selected_quote: BookmakerPricedQuote | None
    betano_quote: BookmakerPricedQuote | None
    betclic_quote: BookmakerPricedQuote | None
    absolute_difference: Decimal | None
    percentage_difference: Decimal | None
    reason_code: QuoteSelectionReason
    equivalence_policy_id: str
    compared_at_utc: datetime
    betano_fresh: bool | None
    betclic_fresh: bool | None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "compared_at_utc",
            require_utc(self.compared_at_utc, field_name="compared_at_utc"),
        )
        if self.selected_quote is not None and self.selected_bookmaker_id is None:
            msg = "selected quote requires selected_bookmaker_id"
            raise PermanentSourceError(msg)
        if self.selected_quote is not None and (
            self.selected_quote.provider_id != self.selected_bookmaker_id
        ):
            msg = "selected_bookmaker_id must match selected quote provider"
            raise PermanentSourceError(msg)


def quote_is_fresh(
    quote: BookmakerPricedQuote,
    *,
    now: datetime,
    maximum_age_seconds: int,
) -> bool:
    """Return whether ``quote`` is within the configured freshness window."""
    return quote_is_fresh_at(
        quote,
        compared_at=now,
        maximum_age_seconds=maximum_age_seconds,
    )


def select_quote_pair(
    betano_quote: BookmakerPricedQuote | None,
    betclic_quote: BookmakerPricedQuote | None,
    policy: BookmakerSelectionPolicy,
    now: datetime,
) -> BookmakerQuoteComparison:
    """Select a bookmaker quote under the configured preference policy.

    Never hides source bookmaker identity. Stale comparison quotes must not
    replace a fresh preferred quote. ``both`` retains separate quotes without a
    single selected winner.
    """
    compared_at = require_utc(now, field_name="now")
    betano = _validated_side(betano_quote, expected=PROVIDER_BETANO_PT)
    betclic = _validated_side(betclic_quote, expected=PROVIDER_BETCLIC_PT)

    if (
        betano is not None
        and betclic is not None
        and not quotes_are_equivalent(betano, betclic, policy=policy)
    ):
        msg = "cannot compare mismatched quote equivalence identities"
        raise PermanentSourceError(msg)

    betano_fresh = (
        None
        if betano is None
        else quote_is_fresh_at(
            betano,
            compared_at=compared_at,
            maximum_age_seconds=policy.quote_maximum_age_seconds,
        )
    )
    betclic_fresh = (
        None
        if betclic is None
        else quote_is_fresh_at(
            betclic,
            compared_at=compared_at,
            maximum_age_seconds=policy.quote_maximum_age_seconds,
        )
    )

    # Re-stamp freshness onto quotes so callers see policy evaluation results.
    if betano is not None and betano_fresh is not None and betano.fresh != betano_fresh:
        betano = _with_fresh(betano, betano_fresh)
    if betclic is not None and betclic_fresh is not None and betclic.fresh != betclic_fresh:
        betclic = _with_fresh(betclic, betclic_fresh)

    usable_betano = betano if betano is not None and betano_fresh is True else None
    usable_betclic = betclic if betclic is not None and betclic_fresh is True else None
    if usable_betano is not None:
        _require_verified_selectable_quote(usable_betano)
    if usable_betclic is not None:
        _require_verified_selectable_quote(usable_betclic)

    if policy.selection_mode is SelectionMode.BOTH:
        return _comparison(
            selected_bookmaker_id=None,
            selected_quote=None,
            betano=betano,
            betclic=betclic,
            reason=QuoteSelectionReason.BOTH_RETAINED,
            policy=policy,
            compared_at=compared_at,
            betano_fresh=betano_fresh,
            betclic_fresh=betclic_fresh,
        )

    if policy.selection_mode is SelectionMode.BETANO:
        return _forced(
            preferred=usable_betano,
            comparison=usable_betclic,
            selected=usable_betano,
            reason=(
                QuoteSelectionReason.MODE_FORCED_PREFERRED
                if usable_betano is not None
                else QuoteSelectionReason.NEITHER_AVAILABLE
            ),
            policy=policy,
            compared_at=compared_at,
            betano=betano,
            betclic=betclic,
            betano_fresh=betano_fresh,
            betclic_fresh=betclic_fresh,
        )

    if policy.selection_mode is SelectionMode.BETCLIC:
        return _forced(
            preferred=usable_betano,
            comparison=usable_betclic,
            selected=usable_betclic,
            reason=(
                QuoteSelectionReason.MODE_FORCED_COMPARISON
                if usable_betclic is not None
                else QuoteSelectionReason.NEITHER_AVAILABLE
            ),
            policy=policy,
            compared_at=compared_at,
            betano=betano,
            betclic=betclic,
            betano_fresh=betano_fresh,
            betclic_fresh=betclic_fresh,
        )

    if usable_betano is None and usable_betclic is None:
        # Prefer retaining a stale preferred observation over a stale comparison
        # when neither is fresh, still labelling neither as a current selection.
        return _comparison(
            selected_bookmaker_id=None,
            selected_quote=None,
            betano=betano,
            betclic=betclic,
            reason=QuoteSelectionReason.NEITHER_AVAILABLE,
            policy=policy,
            compared_at=compared_at,
            betano_fresh=betano_fresh,
            betclic_fresh=betclic_fresh,
        )

    if usable_betano is not None and usable_betclic is None:
        reason = QuoteSelectionReason.PREFERRED_ONLY
        if betclic is not None and betclic_fresh is False:
            reason = QuoteSelectionReason.PREFERRED_RETAINED_STALE_COMPARISON
        return _comparison(
            selected_bookmaker_id=PROVIDER_BETANO_PT,
            selected_quote=usable_betano,
            betano=betano,
            betclic=betclic,
            reason=reason,
            policy=policy,
            compared_at=compared_at,
            betano_fresh=betano_fresh,
            betclic_fresh=betclic_fresh,
        )

    if usable_betano is None and usable_betclic is not None:
        return _comparison(
            selected_bookmaker_id=PROVIDER_BETCLIC_PT,
            selected_quote=usable_betclic,
            betano=betano,
            betclic=betclic,
            reason=QuoteSelectionReason.COMPARISON_FALLBACK,
            policy=policy,
            compared_at=compared_at,
            betano_fresh=betano_fresh,
            betclic_fresh=betclic_fresh,
        )

    assert usable_betano is not None and usable_betclic is not None
    if policy.selection_mode is SelectionMode.BEST or (
        policy.selection_mode is SelectionMode.PREFERRED_UNLESS_BETTER
    ):
        if usable_betclic.decimal_odds > usable_betano.decimal_odds:
            reason = (
                QuoteSelectionReason.BEST_SELECTED
                if policy.selection_mode is SelectionMode.BEST
                else QuoteSelectionReason.HIGHER_ODDS
            )
            selected = usable_betclic
            selected_id = PROVIDER_BETCLIC_PT
        elif usable_betano.decimal_odds > usable_betclic.decimal_odds:
            reason = (
                QuoteSelectionReason.BEST_SELECTED
                if policy.selection_mode is SelectionMode.BEST
                else QuoteSelectionReason.HIGHER_ODDS
            )
            selected = usable_betano
            selected_id = PROVIDER_BETANO_PT
        else:
            reason = QuoteSelectionReason.EQUAL_ODDS_PREFERRED
            selected = usable_betano
            selected_id = PROVIDER_BETANO_PT
        return _comparison(
            selected_bookmaker_id=selected_id,
            selected_quote=selected,
            betano=betano,
            betclic=betclic,
            reason=reason,
            policy=policy,
            compared_at=compared_at,
            betano_fresh=betano_fresh,
            betclic_fresh=betclic_fresh,
        )

    msg = f"unsupported selection_mode: {policy.selection_mode}"
    raise PermanentSourceError(msg)


def _require_verified_selectable_quote(quote: BookmakerPricedQuote) -> None:
    if not quote.fresh:
        return
    if not quote.snapshot_id or not quote.snapshot_checksum_sha256:
        msg = "selectable quote requires verified snapshot_id and checksum"
        raise PermanentSourceError(msg)
    if quote.market_status != "open" or quote.selection_status != "active":
        msg = "selectable quote requires open market and active selection"
        raise PermanentSourceError(msg)


def _validated_side(
    quote: BookmakerPricedQuote | None,
    *,
    expected: str,
) -> BookmakerPricedQuote | None:
    if quote is None:
        return None
    if quote.provider_id != expected:
        msg = f"expected provider {expected}, got {quote.provider_id}"
        raise PermanentSourceError(msg)
    return quote


def _with_fresh(quote: BookmakerPricedQuote, fresh: bool) -> BookmakerPricedQuote:
    return BookmakerPricedQuote(
        provider_id=quote.provider_id,
        decimal_odds=quote.decimal_odds,
        observed_at_utc=quote.observed_at_utc,
        canonical_event_id=quote.canonical_event_id,
        canonical_market_definition_id=quote.canonical_market_definition_id,
        canonical_selection_id=quote.canonical_selection_id,
        fresh=fresh,
        line=quote.line,
        period=quote.period,
        participant_scope=quote.participant_scope,
        overtime_scope=quote.overtime_scope,
        rules_scope=quote.rules_scope,
        market_status=quote.market_status,
        selection_status=quote.selection_status,
        snapshot_id=quote.snapshot_id,
        snapshot_checksum_sha256=quote.snapshot_checksum_sha256,
    )


def _forced(
    *,
    preferred: BookmakerPricedQuote | None,
    comparison: BookmakerPricedQuote | None,
    selected: BookmakerPricedQuote | None,
    reason: QuoteSelectionReason,
    policy: BookmakerSelectionPolicy,
    compared_at: datetime,
    betano: BookmakerPricedQuote | None,
    betclic: BookmakerPricedQuote | None,
    betano_fresh: bool | None,
    betclic_fresh: bool | None,
) -> BookmakerQuoteComparison:
    _ = (preferred, comparison)
    return _comparison(
        selected_bookmaker_id=None if selected is None else selected.provider_id,
        selected_quote=selected,
        betano=betano,
        betclic=betclic,
        reason=reason,
        policy=policy,
        compared_at=compared_at,
        betano_fresh=betano_fresh,
        betclic_fresh=betclic_fresh,
    )


def _comparison(
    *,
    selected_bookmaker_id: str | None,
    selected_quote: BookmakerPricedQuote | None,
    betano: BookmakerPricedQuote | None,
    betclic: BookmakerPricedQuote | None,
    reason: QuoteSelectionReason,
    policy: BookmakerSelectionPolicy,
    compared_at: datetime,
    betano_fresh: bool | None,
    betclic_fresh: bool | None,
) -> BookmakerQuoteComparison:
    absolute: Decimal | None = None
    percentage: Decimal | None = None
    if betano is not None and betclic is not None:
        absolute = abs(betano.decimal_odds - betclic.decimal_odds)
        if betano.decimal_odds != 0:
            percentage = (absolute / betano.decimal_odds) * Decimal("100")
    return BookmakerQuoteComparison(
        selected_bookmaker_id=selected_bookmaker_id,
        selected_quote=selected_quote,
        betano_quote=betano,
        betclic_quote=betclic,
        absolute_difference=absolute,
        percentage_difference=percentage,
        reason_code=reason,
        equivalence_policy_id=policy.equivalence_policy_id,
        compared_at_utc=compared_at,
        betano_fresh=betano_fresh,
        betclic_fresh=betclic_fresh,
    )
