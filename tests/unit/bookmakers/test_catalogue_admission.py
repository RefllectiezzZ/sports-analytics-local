"""Sealed verified-quote catalogue admission and acceptance identity tests."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from sports_analytics.bookmakers.diagnostics.acceptance import build_acceptance_report
from sports_analytics.bookmakers.selection import BookmakerSelectionPolicy, select_quote_pair
from sports_analytics.bookmakers.verified_evidence import (
    VerifiedQuoteCatalogue,
    require_catalogue_quote,
)
from sports_analytics.core.exceptions import PermanentSourceError
from tests.unit.bookmakers.verified_quote_helpers import verified_quote

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)


def _catalogue_for(*quotes):
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


def test_manual_dataclass_quote_rejected_by_catalogue_api() -> None:
    real = verified_quote(provider_id="betano-pt", odds="1.50", observed_at=NOW)
    catalogue = _catalogue_for(real)
    copy = verified_quote(provider_id="betano-pt", odds="1.50", observed_at=NOW)
    # Same observation id textually, but not the catalogue member object.
    with pytest.raises(PermanentSourceError, match="not admitted"):
        require_catalogue_quote(copy, catalogue=catalogue)


def test_selection_rejects_non_catalogue_member_when_catalogue_required() -> None:
    real = verified_quote(provider_id="betano-pt", odds="1.50", observed_at=NOW)
    catalogue = _catalogue_for(real)
    impostor = verified_quote(provider_id="betano-pt", odds="9.99", observed_at=NOW)
    with pytest.raises(PermanentSourceError, match="not admitted"):
        select_quote_pair(
            impostor,
            None,
            BookmakerSelectionPolicy(),
            NOW,
            betano_catalogue=catalogue,
        )


def test_acceptance_requires_full_comparison_identity_including_line() -> None:
    over25 = verified_quote(
        provider_id="betano-pt",
        odds="1.80",
        observed_at=NOW,
        market_key="football.totals.goals.full-match",
        canonical_market_definition_id="football-total-goals",
        selection_id="over",
        line_type="total",
        line_value=Decimal("2.5"),
    )
    over35 = verified_quote(
        provider_id="betclic-pt",
        odds="1.90",
        observed_at=NOW,
        market_key="football.totals.goals.full-match",
        canonical_market_definition_id="football-total-goals",
        selection_id="over",
        line_type="total",
        line_value=Decimal("3.5"),
    )
    report = build_acceptance_report(
        betano_quotes=(over25,),
        betclic_quotes=(over35,),
        evaluated_at_utc=NOW,
    )
    assert report.common_markets == 0


def test_acceptance_matches_identical_full_identity() -> None:
    betano = verified_quote(
        provider_id="betano-pt",
        odds="1.80",
        observed_at=NOW,
        market_key="football.totals.goals.full-match",
        canonical_market_definition_id="football-total-goals",
        selection_id="over",
        line_type="total",
        line_value=Decimal("2.5"),
    )
    betclic = verified_quote(
        provider_id="betclic-pt",
        odds="1.90",
        observed_at=NOW,
        market_key="football.totals.goals.full-match",
        canonical_market_definition_id="football-total-goals",
        selection_id="over",
        line_type="total",
        line_value=Decimal("2.5"),
    )
    report = build_acceptance_report(
        betano_quotes=(betano,),
        betclic_quotes=(betclic,),
        evaluated_at_utc=NOW,
    )
    assert report.common_markets == 1
