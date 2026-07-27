"""Offline singles quote-selection policy coverage for PR #11 §23."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from sports_analytics.bookmakers.selection import BookmakerSelectionPolicy, select_quote_pair
from sports_analytics.bookmakers.types import (
    PROVIDER_BETANO_PT,
    PROVIDER_BETCLIC_PT,
    QuoteSelectionReason,
    SelectionMode,
)
from sports_analytics.bookmakers.verified_evidence import VerifiedBookmakerQuote
from sports_analytics.core.exceptions import PermanentSourceError
from tests.unit.bookmakers.verified_quote_helpers import (
    catalogue_for,
    empty_catalogue,
    verified_quote,
)

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)


def _quote(
    provider_id: str,
    odds: str,
    *,
    age_seconds: int = 0,
    selection_id: str = "home",
    snapshot_id: str = "snap-1",
    snapshot_checksum_sha256: str = "a" * 64,
) -> VerifiedBookmakerQuote:
    return verified_quote(
        provider_id=provider_id,
        odds=odds,
        age_seconds=age_seconds,
        observed_at=NOW,
        selection_id=selection_id,
        snapshot_id=snapshot_id,
        snapshot_checksum_sha256=snapshot_checksum_sha256,
    )


def test_verified_selectable_quote_requires_snapshot_evidence() -> None:
    quote = VerifiedBookmakerQuote(
        snapshot_id="",
        snapshot_checksum_sha256="",
        provider_id=PROVIDER_BETANO_PT,
        sport="football",
        identity=_quote(PROVIDER_BETANO_PT, "2.00").identity,
        decimal_odds=_quote(PROVIDER_BETANO_PT, "2.00").decimal_odds,
        observed_at_utc=NOW,
        market_status="open",
        selection_status="active",
        source_file_sha256="b" * 64,
        canonical_market_definition_id="football-match-result-1x2",
        canonical_selection_id="home",
        source_event_id="source-a",
    )
    with pytest.raises(PermanentSourceError, match="verified snapshot_id"):
        select_quote_pair(
            quote,
            None,
            BookmakerSelectionPolicy(),
            NOW,
            betano_catalogue=catalogue_for(quote),
            betclic_catalogue=empty_catalogue(provider_id=PROVIDER_BETCLIC_PT),
        )
    with pytest.raises(PermanentSourceError, match="preferred_bookmaker"):
        BookmakerSelectionPolicy(preferred_bookmaker="other")
    with pytest.raises(PermanentSourceError, match="comparison_bookmaker"):
        BookmakerSelectionPolicy(comparison_bookmaker="other")


def test_preferred_only_when_betclic_missing() -> None:
    betano = _quote(PROVIDER_BETANO_PT, "1.90")
    result = select_quote_pair(
        betano,
        None,
        BookmakerSelectionPolicy(),
        NOW,
        betano_catalogue=catalogue_for(betano),
        betclic_catalogue=empty_catalogue(provider_id=PROVIDER_BETCLIC_PT),
    )
    assert result.selected_bookmaker_id == PROVIDER_BETANO_PT
    assert result.reason_code is QuoteSelectionReason.PREFERRED_ONLY
    assert result.selected_quote is not None
    assert result.selected_quote.provider_id == PROVIDER_BETANO_PT


def test_comparison_fallback_when_betano_missing() -> None:
    betclic = _quote(PROVIDER_BETCLIC_PT, "2.10")
    result = select_quote_pair(
        None,
        betclic,
        BookmakerSelectionPolicy(),
        NOW,
        betano_catalogue=empty_catalogue(provider_id=PROVIDER_BETANO_PT),
        betclic_catalogue=catalogue_for(betclic),
    )
    assert result.selected_bookmaker_id == PROVIDER_BETCLIC_PT
    assert result.reason_code is QuoteSelectionReason.COMPARISON_FALLBACK


def test_higher_fresh_odds_selected() -> None:
    betano = _quote(PROVIDER_BETANO_PT, "1.90")
    betclic = _quote(PROVIDER_BETCLIC_PT, "2.05")
    result = select_quote_pair(
        betano,
        betclic,
        BookmakerSelectionPolicy(),
        NOW,
        betano_catalogue=catalogue_for(betano),
        betclic_catalogue=catalogue_for(betclic),
    )
    assert result.selected_bookmaker_id == PROVIDER_BETCLIC_PT
    assert result.reason_code is QuoteSelectionReason.HIGHER_ODDS
    assert result.absolute_difference == Decimal("0.15")
    assert result.betano_quote is not None and result.betclic_quote is not None


def test_equal_odds_select_betano() -> None:
    betano = _quote(PROVIDER_BETANO_PT, "2.00")
    betclic = _quote(PROVIDER_BETCLIC_PT, "2.00")
    result = select_quote_pair(
        betano,
        betclic,
        BookmakerSelectionPolicy(),
        NOW,
        betano_catalogue=catalogue_for(betano),
        betclic_catalogue=catalogue_for(betclic),
    )
    assert result.selected_bookmaker_id == PROVIDER_BETANO_PT
    assert result.reason_code is QuoteSelectionReason.EQUAL_ODDS_PREFERRED


def test_stale_betclic_does_not_replace_fresh_betano() -> None:
    betano = _quote(PROVIDER_BETANO_PT, "1.80")
    betclic = _quote(PROVIDER_BETCLIC_PT, "2.50", age_seconds=10_000)
    result = select_quote_pair(
        betano,
        betclic,
        BookmakerSelectionPolicy(quote_maximum_age_seconds=300),
        NOW,
        betano_catalogue=catalogue_for(betano),
        betclic_catalogue=catalogue_for(betclic),
    )
    assert result.selected_bookmaker_id == PROVIDER_BETANO_PT
    assert result.reason_code is QuoteSelectionReason.PREFERRED_RETAINED_STALE_COMPARISON
    assert result.betclic_fresh is False


def test_neither_available_when_both_stale_or_missing() -> None:
    betano = _quote(PROVIDER_BETANO_PT, "1.80", age_seconds=9999)
    betclic = _quote(PROVIDER_BETCLIC_PT, "2.00", age_seconds=9999)
    result = select_quote_pair(
        betano,
        betclic,
        BookmakerSelectionPolicy(quote_maximum_age_seconds=60),
        NOW,
        betano_catalogue=catalogue_for(betano),
        betclic_catalogue=catalogue_for(betclic),
    )
    assert result.selected_quote is None
    assert result.reason_code is QuoteSelectionReason.NEITHER_AVAILABLE


def test_both_mode_retains_separate_quotes_without_winner() -> None:
    policy = BookmakerSelectionPolicy(selection_mode=SelectionMode.BOTH)
    betano = _quote(PROVIDER_BETANO_PT, "1.90")
    betclic = _quote(PROVIDER_BETCLIC_PT, "2.10")
    result = select_quote_pair(
        betano,
        betclic,
        policy,
        NOW,
        betano_catalogue=catalogue_for(betano),
        betclic_catalogue=catalogue_for(betclic),
    )
    assert result.selected_quote is None
    assert result.reason_code is QuoteSelectionReason.BOTH_RETAINED
    assert result.betano_quote is not None
    assert result.betclic_quote is not None


def test_forced_modes_and_best_mode() -> None:
    betano = _quote(PROVIDER_BETANO_PT, "1.90")
    betclic = _quote(PROVIDER_BETCLIC_PT, "2.20")
    catalogues = dict(
        betano_catalogue=catalogue_for(betano),
        betclic_catalogue=catalogue_for(betclic),
    )
    forced_betano = select_quote_pair(
        betano, betclic, BookmakerSelectionPolicy(selection_mode=SelectionMode.BETANO), NOW, **catalogues
    )
    forced_betclic = select_quote_pair(
        betano, betclic, BookmakerSelectionPolicy(selection_mode=SelectionMode.BETCLIC), NOW, **catalogues
    )
    best = select_quote_pair(
        betano, betclic, BookmakerSelectionPolicy(selection_mode=SelectionMode.BEST), NOW, **catalogues
    )
    assert forced_betano.reason_code is QuoteSelectionReason.MODE_FORCED_PREFERRED
    assert forced_betano.selected_bookmaker_id == PROVIDER_BETANO_PT
    assert forced_betclic.reason_code is QuoteSelectionReason.MODE_FORCED_COMPARISON
    assert forced_betclic.selected_bookmaker_id == PROVIDER_BETCLIC_PT
    assert best.reason_code is QuoteSelectionReason.BEST_SELECTED
    assert best.selected_bookmaker_id == PROVIDER_BETCLIC_PT


def test_every_quote_retains_source_identity() -> None:
    betano = _quote(PROVIDER_BETANO_PT, "1.95")
    betclic = _quote(PROVIDER_BETCLIC_PT, "2.00")
    result = select_quote_pair(
        betano,
        betclic,
        BookmakerSelectionPolicy(),
        NOW,
        betano_catalogue=catalogue_for(betano),
        betclic_catalogue=catalogue_for(betclic),
    )
    assert result.betano_quote is not None
    assert result.betclic_quote is not None
    assert result.betano_quote.provider_id == PROVIDER_BETANO_PT
    assert result.betclic_quote.provider_id == PROVIDER_BETCLIC_PT
    assert result.selected_quote is not None
    assert result.selected_quote.provider_id == result.selected_bookmaker_id


def test_future_observed_timestamp_is_not_fresh() -> None:
    future = NOW + timedelta(hours=1)
    quote = verified_quote(
        provider_id=PROVIDER_BETANO_PT,
        odds="2.00",
        observed_at=future,
        age_seconds=0,
    )
    result = select_quote_pair(
        quote,
        None,
        BookmakerSelectionPolicy(),
        NOW,
        betano_catalogue=catalogue_for(quote),
        betclic_catalogue=empty_catalogue(provider_id=PROVIDER_BETCLIC_PT),
    )
    assert result.selected_quote is None
    assert result.reason_code is QuoteSelectionReason.NEITHER_AVAILABLE
