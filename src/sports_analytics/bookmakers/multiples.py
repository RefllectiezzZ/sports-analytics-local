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
from sports_analytics.bookmakers.verified_evidence import (
    VerifiedBookmakerQuote,
    VerifiedQuoteCatalogue,
    leg_identity_from_verified_quote,
    require_catalogue_quote,
)
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
    canonical_participant_id: str | None
    line_type: str
    line_value: Decimal | None
    period: str
    participant_scope: str
    overtime_scope: str | None
    rules_scope: str | None
    quote_observation_id: str
    observed_at_utc: datetime
    snapshot_id: str
    snapshot_checksum_sha256: str
    decimal_odds: Decimal
    leg_key: str

    def __post_init__(self) -> None:
        if self.bookmaker_id not in {PROVIDER_BETANO_PT, PROVIDER_BETCLIC_PT}:
            msg = f"unsupported bookmaker_id: {self.bookmaker_id}"
            raise PermanentSourceError(msg)
        if not self.leg_key.strip():
            msg = "leg_key must be non-empty"
            raise PermanentSourceError(msg)
        if not self.snapshot_id or not self.snapshot_checksum_sha256:
            msg = "multiple leg requires verified snapshot id and checksum"
            raise PermanentSourceError(msg)
        object.__setattr__(self, "decimal_odds", validate_decimal_odds(self.decimal_odds))
        object.__setattr__(
            self,
            "observed_at_utc",
            require_utc(self.observed_at_utc, field_name="observed_at_utc"),
        )

    def material_identity(self) -> tuple[object, ...]:
        """Return materially relevant dimensions for eligibility comparison."""
        return (
            self.bookmaker_id,
            self.canonical_event_id,
            self.canonical_market_definition_id,
            self.canonical_selection_id,
            self.canonical_participant_id,
            self.line_type,
            self.line_value,
            self.period,
            self.participant_scope,
            self.overtime_scope,
            self.rules_scope,
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
    canonical_participant_id: str | None = None
    line_type: str = "none"
    line_value: Decimal | None = None
    period: str = "full-match"
    participant_scope: str = "event"
    overtime_scope: str | None = None
    rules_scope: str | None = "regulation-only"

    def __post_init__(self) -> None:
        if not self.leg_key.strip():
            msg = "leg_key must be non-empty"
            raise PermanentSourceError(msg)

    def material_identity(self) -> tuple[object, ...]:
        """Return materially relevant dimensions for eligibility comparison."""
        return (
            self.canonical_event_id,
            self.canonical_market_definition_id,
            self.canonical_selection_id,
            self.canonical_participant_id,
            self.line_type,
            self.line_value,
            self.period,
            self.participant_scope,
            self.overtime_scope,
            self.rules_scope,
        )


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
    allow_mixed_snapshots: bool = False,
    betano_catalogue: VerifiedQuoteCatalogue | None = None,
    betclic_catalogue: VerifiedQuoteCatalogue | None = None,
) -> ProviderMultipleComparison:
    """Compare complete Betano-only and Betclic-only multiples.

    Incomplete provider coverage makes that provider ineligible. Equal complete
    totals select Betano. Best individual legs are never mixed across providers.
    When catalogues are supplied, every quote must be an exact catalogue member.
    """
    evaluated_at = require_utc(evaluated_at_utc, field_name="evaluated_at_utc")
    if betano_catalogue is not None:
        betano_quotes_by_leg_key = {
            key: require_catalogue_quote(quote, catalogue=betano_catalogue)
            for key, quote in betano_quotes_by_leg_key.items()
        }
    if betclic_catalogue is not None:
        betclic_quotes_by_leg_key = {
            key: require_catalogue_quote(quote, catalogue=betclic_catalogue)
            for key, quote in betclic_quotes_by_leg_key.items()
        }
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
        allow_mixed_snapshots=allow_mixed_snapshots,
    )
    _reject_mixed_observation_windows(
        betclic_quotes_by_leg_key,
        evaluated_at_utc=evaluated_at,
        quote_maximum_age_seconds=quote_maximum_age_seconds,
        allow_mixed_snapshots=allow_mixed_snapshots,
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


def quote_matches_requested_leg(
    spec: RequestedMultipleLegSpec,
    verified: VerifiedBookmakerQuote,
) -> bool:
    """Return whether one verified quote matches every requested leg dimension."""
    identity = verified.identity
    if spec.material_identity() != (
        spec.canonical_event_id,
        spec.canonical_market_definition_id,
        spec.canonical_selection_id,
        spec.canonical_participant_id,
        spec.line_type,
        spec.line_value,
        spec.period,
        spec.participant_scope,
        spec.overtime_scope,
        spec.rules_scope,
    ):
        return False
    return (
        verified.identity.canonical_event_id == spec.canonical_event_id
        and verified.canonical_market_definition_id == spec.canonical_market_definition_id
        and verified.canonical_selection_id == spec.canonical_selection_id
        and identity.canonical_participant_id == spec.canonical_participant_id
        and identity.line_type == spec.line_type
        and identity.line_value == spec.line_value
        and identity.market_period == spec.period
        and identity.participant_scope == spec.participant_scope
        and identity.overtime_scope == spec.overtime_scope
        and identity.rules_scope == spec.rules_scope
    )


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
        if not quote_matches_requested_leg(spec, verified):
            return None, False
        quote = verified.to_priced_quote(
            evaluated_at_utc=evaluated_at_utc,
            maximum_age_seconds=quote_maximum_age_seconds,
        )
        if not quote.fresh:
            return None, False
        if quote.market_status != "open" or quote.selection_status != "active":
            return None, False
        identity = verified.identity
        legs.append(
            BookmakerMultipleLeg(
                bookmaker_id=provider_id,
                canonical_event_id=spec.canonical_event_id,
                canonical_market_definition_id=spec.canonical_market_definition_id,
                canonical_selection_id=spec.canonical_selection_id,
                canonical_participant_id=identity.canonical_participant_id,
                line_type=identity.line_type,
                line_value=identity.line_value,
                period=identity.market_period,
                participant_scope=identity.participant_scope,
                overtime_scope=identity.overtime_scope,
                rules_scope=identity.rules_scope,
                quote_observation_id=identity.quote_observation_id,
                observed_at_utc=quote.observed_at_utc,
                snapshot_id=verified.snapshot_id,
                snapshot_checksum_sha256=verified.snapshot_checksum_sha256,
                decimal_odds=quote.decimal_odds,
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
    seen: set[tuple[object, ...]] = set()
    for spec in specs:
        identity = spec.material_identity()
        if identity in seen:
            msg = "duplicate canonical bet identities are rejected across leg keys"
            raise PermanentSourceError(msg)
        seen.add(identity)


def _reject_mixed_observation_windows(
    quotes_by_leg_key: dict[str, VerifiedBookmakerQuote],
    *,
    evaluated_at_utc: datetime,
    quote_maximum_age_seconds: int,
    allow_mixed_snapshots: bool,
) -> None:
    observed: set[datetime] = set()
    snapshots: set[tuple[str, str]] = set()
    for verified in quotes_by_leg_key.values():
        quote = verified.to_priced_quote(
            evaluated_at_utc=evaluated_at_utc,
            maximum_age_seconds=quote_maximum_age_seconds,
        )
        if quote.fresh:
            if not quote.snapshot_id or not quote.snapshot_checksum_sha256:
                msg = "verified quote evidence requires snapshot id and checksum"
                raise PermanentSourceError(msg)
            snapshots.add((quote.snapshot_id, quote.snapshot_checksum_sha256))
        observed.add(quote.observed_at_utc)
        _ = leg_identity_from_verified_quote(verified)
    if len(observed) > 1:
        msg = "mixed observation windows are rejected for multiples"
        raise PermanentSourceError(msg)
    if not allow_mixed_snapshots and len(snapshots) > 1:
        msg = "mixed snapshots are rejected for multiples"
        raise PermanentSourceError(msg)
