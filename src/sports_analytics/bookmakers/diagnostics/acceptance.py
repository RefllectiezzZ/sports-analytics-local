"""Cross-provider acceptance reporting without raw provider payloads."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sports_analytics.bookmakers.diagnostics.redaction import redact_structure
from sports_analytics.bookmakers.multiples import (
    RequestedMultipleLegSpec,
    compare_provider_multiples,
)
from sports_analytics.bookmakers.selection import (
    DEFAULT_BOOKMAKER_SELECTION_POLICY,
    select_quote_pair,
)
from sports_analytics.bookmakers.types import PROVIDER_BETANO_PT, PROVIDER_BETCLIC_PT
from sports_analytics.bookmakers.verified_evidence import (
    VerifiedBookmakerQuote,
    VerifiedQuoteCatalogue,
)


@dataclass(frozen=True, slots=True)
class AcceptanceReport:
    """Sanitized cross-provider acceptance proof."""

    common_events: int
    common_markets: int
    betano_quotes_compared: int
    betclic_quotes_compared: int
    higher_price_selections: int
    betano_tie_preferences: int
    betano_only_multiple_total: str | None
    betclic_only_multiple_total: str | None
    selected_multiple_provider: str | None
    mixed_provider_multiple_rejected: bool
    summary: dict[str, Any]


def build_acceptance_report(
    *,
    betano_quotes: tuple[VerifiedBookmakerQuote, ...],
    betclic_quotes: tuple[VerifiedBookmakerQuote, ...],
    evaluated_at_utc: Any,
    leg_specs: tuple[RequestedMultipleLegSpec, ...] | None = None,
    betano_catalogue: VerifiedQuoteCatalogue | None = None,
    betclic_catalogue: VerifiedQuoteCatalogue | None = None,
) -> AcceptanceReport:
    """Prove cross-provider equivalence and same-bookmaker multiples."""
    betano_cat = betano_catalogue or _catalogue_from_quotes(
        betano_quotes,
        provider_id=PROVIDER_BETANO_PT,
    )
    betclic_cat = betclic_catalogue or _catalogue_from_quotes(
        betclic_quotes,
        provider_id=PROVIDER_BETCLIC_PT,
    )
    common_market_keys = _common_quote_keys(betano_quotes, betclic_quotes)
    higher = 0
    ties = 0
    for key in common_market_keys:
        betano = _quote_by_key(betano_quotes, key)
        betclic = _quote_by_key(betclic_quotes, key)
        if betano is None or betclic is None:
            continue
        comparison = select_quote_pair(
            betano,
            betclic,
            DEFAULT_BOOKMAKER_SELECTION_POLICY,
            evaluated_at_utc,
            betano_catalogue=betano_cat,
            betclic_catalogue=betclic_cat,
        )
        if comparison.reason_code.value == "higher-odds":
            if comparison.selected_bookmaker_id == PROVIDER_BETCLIC_PT:
                higher += 1
        elif comparison.reason_code.value == "equal-odds-preferred":
            ties += 1
    betano_by_leg: dict[str, VerifiedBookmakerQuote] = {}
    betclic_by_leg: dict[str, VerifiedBookmakerQuote] = {}
    specs = leg_specs or ()
    if specs:
        for spec in specs:
            for quote in betano_quotes:
                if _leg_matches_spec(quote, spec):
                    betano_by_leg[spec.leg_key] = quote
            for quote in betclic_quotes:
                if _leg_matches_spec(quote, spec):
                    betclic_by_leg[spec.leg_key] = quote
    multiple_comparison = None
    if len(specs) >= 2:
        # Always calculate Betano-only and Betclic-only multiples; never mixed.
        multiple_comparison = compare_provider_multiples(
            specs,
            betano_by_leg,
            betclic_by_leg,
            evaluated_at_utc=evaluated_at_utc,
            quote_maximum_age_seconds=DEFAULT_BOOKMAKER_SELECTION_POLICY.quote_maximum_age_seconds,
            betano_catalogue=betano_cat,
            betclic_catalogue=betclic_cat,
        )
    summary = redact_structure(
        {
            "common_identity_count": len(common_market_keys),
            "betano_quote_count": len(betano_quotes),
            "betclic_quote_count": len(betclic_quotes),
            "comparison_outcomes": {
                "higher_price_selections": higher,
                "betano_tie_preferences": ties,
            },
        }
    )
    return AcceptanceReport(
        common_events=len({key[0] for key in common_market_keys}),
        common_markets=len(common_market_keys),
        betano_quotes_compared=len(betano_quotes),
        betclic_quotes_compared=len(betclic_quotes),
        higher_price_selections=higher,
        betano_tie_preferences=ties,
        betano_only_multiple_total=(
            None
            if multiple_comparison is None or multiple_comparison.betano_multiple is None
            else format(multiple_comparison.betano_multiple.total_decimal_odds, "f")
        ),
        betclic_only_multiple_total=(
            None
            if multiple_comparison is None or multiple_comparison.betclic_multiple is None
            else format(multiple_comparison.betclic_multiple.total_decimal_odds, "f")
        ),
        selected_multiple_provider=(
            None
            if multiple_comparison is None or multiple_comparison.selected_multiple is None
            else multiple_comparison.selected_multiple.bookmaker_id
        ),
        mixed_provider_multiple_rejected=True,
        summary=summary if isinstance(summary, dict) else {"summary": summary},
    )


def _catalogue_from_quotes(
    quotes: tuple[VerifiedBookmakerQuote, ...],
    *,
    provider_id: str,
) -> VerifiedQuoteCatalogue:
    if not quotes:
        return VerifiedQuoteCatalogue(
            snapshot_id="acceptance-empty",
            snapshot_checksum_sha256="d" * 64,
            provider_id=provider_id,
            sport="football",
            quotes_by_observation_id=(),
            quotes_by_semantic_identity=(),
        )
    first = quotes[0]
    return VerifiedQuoteCatalogue(
        snapshot_id=first.snapshot_id,
        snapshot_checksum_sha256=first.snapshot_checksum_sha256,
        provider_id=first.provider_id,
        sport=first.sport,
        quotes_by_observation_id=tuple(
            (quote.identity.quote_observation_id, quote) for quote in quotes
        ),
        quotes_by_semantic_identity=(),
    )


def _leg_matches_spec(quote: VerifiedBookmakerQuote, spec: RequestedMultipleLegSpec) -> bool:
    identity = quote.identity
    line = None if identity.line_value is None else identity.line_value
    return (
        identity.canonical_event_id == spec.canonical_event_id
        and quote.canonical_market_definition_id == spec.canonical_market_definition_id
        and quote.canonical_selection_id == spec.canonical_selection_id
        and identity.line_type == spec.line_type
        and line == spec.line_value
        and identity.market_period == spec.period
        and identity.participant_scope == spec.participant_scope
        and identity.canonical_participant_id == spec.canonical_participant_id
        and identity.overtime_scope == spec.overtime_scope
        and identity.rules_scope == spec.rules_scope
    )


def _common_quote_keys(
    betano_quotes: tuple[VerifiedBookmakerQuote, ...],
    betclic_quotes: tuple[VerifiedBookmakerQuote, ...],
) -> set[tuple[object, ...]]:
    """Common cross-provider identity including line/period/scopes/pre-match."""
    betano_keys = {quote.comparison_identity() for quote in betano_quotes if quote.comparable}
    betclic_keys = {quote.comparison_identity() for quote in betclic_quotes if quote.comparable}
    return betano_keys & betclic_keys


def _quote_by_key(
    quotes: tuple[VerifiedBookmakerQuote, ...],
    key: tuple[object, ...],
) -> VerifiedBookmakerQuote | None:
    for quote in quotes:
        if quote.comparison_identity() == key:
            return quote
    return None
