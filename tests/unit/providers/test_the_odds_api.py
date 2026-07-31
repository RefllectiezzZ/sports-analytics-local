from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from sports_analytics.core.exceptions import PermanentSourceError
from sports_analytics.providers.the_odds_api.client import (
    ApiHttpResponse,
    ProviderSecret,
    TheOddsApiClient,
    TheOddsApiQuotaError,
)
from sports_analytics.providers.the_odds_api.mapping import (
    reconcile_provider_event,
    translate_bookmaker_quotes,
)
from sports_analytics.providers.the_odds_api.parser import parse_odds_response
from tests.helpers_snapshots import build_verified_participant_registry

NOW = datetime(2026, 8, 1, 12, tzinfo=UTC)
KEY = "test-provider-key-that-must-remain-private"


class FakeTransport:
    def __init__(
        self,
        body: object,
        *,
        status: int = 200,
        final_url: str | None = None,
    ) -> None:
        self.body = json.dumps(body, separators=(",", ":")).encode()
        self.status = status
        self.final_url = final_url
        self.calls: list[dict[str, object]] = []

    def get(
        self,
        url: str,
        *,
        timeout_seconds: float,
        headers: dict[str, str],
        maximum_bytes: int,
        maximum_redirects: int,
    ) -> ApiHttpResponse:
        self.calls.append(
            {
                "url": url,
                "timeout_seconds": timeout_seconds,
                "headers": headers,
                "maximum_bytes": maximum_bytes,
                "maximum_redirects": maximum_redirects,
            }
        )
        return ApiHttpResponse(
            self.status,
            {
                "x-requests-remaining": "487",
                "x-requests-used": "13",
                "x-requests-last": "1",
            },
            self.body,
            self.final_url or url,
        )


def _sports() -> list[dict[str, object]]:
    return [
        {
            "key": "soccer_epl",
            "group": "Soccer",
            "title": "EPL",
            "description": "English Premier League",
            "active": True,
            "has_outrights": False,
        }
    ]


def _event(
    *,
    home: str = "Team 11111111",
    away: str = "Team 22222222",
    commence: datetime | None = None,
    home_price: float = 2.2,
) -> list[dict[str, object]]:
    start = commence or NOW + timedelta(days=1)

    def bookmaker(key: str, title: str, adjustment: float) -> dict[str, object]:
        return {
            "key": key,
            "title": title,
            "last_update": "2026-08-01T12:00:00Z",
            "markets": [
                {
                    "key": "h2h",
                    "last_update": "2026-08-01T12:00:00Z",
                    "outcomes": [
                        {"name": home, "price": home_price + adjustment},
                        {"name": "Draw", "price": 3.5 + adjustment},
                        {"name": away, "price": 3.6 + adjustment},
                    ],
                },
                {
                    "key": "totals",
                    "last_update": "2026-08-01T12:00:00Z",
                    "outcomes": [
                        {"name": "Over", "price": 1.91, "point": 2.5},
                        {"name": "Under", "price": 1.99, "point": 2.5},
                        {"name": "Over", "price": 2.35, "point": 3.5},
                        {"name": "Under", "price": 1.61, "point": 3.5},
                    ],
                },
            ],
        }

    return [
        {
            "id": "provider-event-1",
            "sport_key": "soccer_epl",
            "sport_title": "EPL",
            "commence_time": start.isoformat().replace("+00:00", "Z"),
            "home_team": home,
            "away_team": away,
            "bookmakers": [
                bookmaker("alpha", "Alpha Book", 0.0),
                bookmaker("beta", "Beta Book", 0.1),
            ],
        }
    ]


def test_client_uses_exact_endpoint_bounds_and_redacts_key() -> None:
    transport = FakeTransport(_sports())
    client = TheOddsApiClient(
        secret=ProviderSecret(KEY),
        transport=transport,
        clock=lambda: NOW,
    )

    catalogue = client.get_sports()

    assert catalogue.sports[0].key == "soccer_epl"
    call = transport.calls[0]
    assert str(call["url"]).startswith("https://api.the-odds-api.com/v4/sports?")
    assert call["maximum_redirects"] == 0
    assert call["timeout_seconds"] == 15.0
    assert KEY not in repr(client)
    assert KEY not in repr(ProviderSecret(KEY))
    assert catalogue.quota.remaining == 487
    assert catalogue.quota.used == 13
    assert catalogue.quota.last_cost == 1


def test_client_rejects_redirect_auth_and_quota_without_leaking_key() -> None:
    redirected = FakeTransport(
        _sports(),
        final_url=f"https://api.the-odds-api.com/v4/sports?apiKey={KEY}&redirected=1",
    )
    client = TheOddsApiClient(
        secret=ProviderSecret(KEY),
        transport=redirected,
        clock=lambda: NOW,
    )
    with pytest.raises(PermanentSourceError) as redirected_error:
        client.get_sports()
    assert KEY not in str(redirected_error.value)

    auth = TheOddsApiClient(
        secret=ProviderSecret(KEY),
        transport=FakeTransport({}, status=401),
        clock=lambda: NOW,
    )
    with pytest.raises(PermanentSourceError) as auth_error:
        auth.get_sports()
    assert KEY not in str(auth_error.value)

    odds_transport = FakeTransport(_event())
    odds = TheOddsApiClient(
        secret=ProviderSecret(KEY),
        transport=odds_transport,
        clock=lambda: NOW,
    )
    with pytest.raises(TheOddsApiQuotaError, match="reserve reached"):
        odds.get_odds(
            sport_key="soccer_epl",
            regions=("eu",),
            markets=("h2h",),
            commence_time_from=NOW,
            commence_time_to=NOW + timedelta(days=2),
            quota_reserve=20,
            known_remaining=20,
        )
    assert not odds_transport.calls


def test_parser_rejects_malformed_invalid_and_started_events() -> None:
    with pytest.raises(PermanentSourceError, match="malformed"):
        parse_odds_response(
            b"{",
            sport_key="soccer_epl",
            acquired_at_utc=NOW,
            headers={},
        )
    invalid = _event(home_price=1.0)
    with pytest.raises(PermanentSourceError, match="valid range"):
        parse_odds_response(
            json.dumps(invalid).encode(),
            sport_key="soccer_epl",
            acquired_at_utc=NOW,
            headers={},
        )
    started = _event(commence=NOW)
    with pytest.raises(PermanentSourceError, match="already started"):
        parse_odds_response(
            json.dumps(started).encode(),
            sport_key="soccer_epl",
            acquired_at_utc=NOW,
            headers={},
        )


def test_mapping_retains_bookmakers_complete_h2h_and_exact_total_lines(
    tmp_path: Path,
) -> None:
    _artifact, registry, _reference = build_verified_participant_registry(
        tmp_path,
        root=tmp_path,
        canonical_participant_ids=(
            "11111111-1111-5111-8111-111111111111",
            "22222222-2222-5222-8222-222222222222",
        ),
        relative_directory="registry",
        competition_id="eng-premier-league",
        evaluated_at_utc=NOW,
    )
    batch = parse_odds_response(
        json.dumps(_event()).encode(),
        sport_key="soccer_epl",
        acquired_at_utc=NOW,
        headers={},
    )
    reconciled, finding = reconcile_provider_event(
        batch.events[0],
        registry=registry,
        acquired_at_utc=NOW,
    )
    assert finding is None
    assert reconciled is not None

    quotes = translate_bookmaker_quotes(
        reconciled,
        acquired_at_utc=NOW,
        enabled_markets=("h2h", "totals"),
    )

    assert {item.provider_id for item in quotes} == {"toa-book-alpha", "toa-book-beta"}
    h2h = [item for item in quotes if item.market_family == "match-result"]
    assert len(h2h) == 6
    assert {item.outcome_key for item in h2h} == {"home", "draw", "away"}
    totals = [item for item in quotes if item.market_family == "total-goals"]
    assert len(totals) == 8
    assert {item.line_value for item in totals} == {
        Decimal("2.5"),
        Decimal("3.5"),
    }
    assert all(
        item.operator_note == "external-provider-event-id=provider-event-1" for item in quotes
    )


def test_incomplete_bookmaker_market_is_not_manufactured_across_books(
    tmp_path: Path,
) -> None:
    payload = _event()
    bookmakers = payload[0]["bookmakers"]
    assert isinstance(bookmakers, list)
    first_book = bookmakers[0]
    second_book = bookmakers[1]
    assert isinstance(first_book, dict)
    assert isinstance(second_book, dict)
    first_markets = first_book["markets"]
    second_markets = second_book["markets"]
    assert isinstance(first_markets, list)
    assert isinstance(second_markets, list)
    first_market = first_markets[0]
    second_market = second_markets[0]
    assert isinstance(first_market, dict)
    assert isinstance(second_market, dict)
    first = first_market["outcomes"]
    second = second_market["outcomes"]
    assert isinstance(first, list)
    assert isinstance(second, list)
    del first[-1]
    del second[0]
    _artifact, registry, _reference = build_verified_participant_registry(
        tmp_path,
        root=tmp_path,
        canonical_participant_ids=(
            "11111111-1111-5111-8111-111111111111",
            "22222222-2222-5222-8222-222222222222",
        ),
        relative_directory="registry",
        competition_id="eng-premier-league",
        evaluated_at_utc=NOW,
    )
    event = parse_odds_response(
        json.dumps(payload).encode(),
        sport_key="soccer_epl",
        acquired_at_utc=NOW,
        headers={},
    ).events[0]
    reconciled, _finding = reconcile_provider_event(
        event,
        registry=registry,
        acquired_at_utc=NOW,
    )
    assert reconciled is not None
    quotes = translate_bookmaker_quotes(
        reconciled,
        acquired_at_utc=NOW,
        enabled_markets=("h2h",),
    )
    assert quotes == ()


def test_unknown_team_fails_closed_without_registry_mutation(tmp_path: Path) -> None:
    _artifact, registry, _reference = build_verified_participant_registry(
        tmp_path,
        root=tmp_path,
        canonical_participant_ids=(
            "11111111-1111-5111-8111-111111111111",
            "22222222-2222-5222-8222-222222222222",
        ),
        relative_directory="registry",
        competition_id="eng-premier-league",
        evaluated_at_utc=NOW,
    )
    before = registry.participants
    event = parse_odds_response(
        json.dumps(_event(home="Unknown United")).encode(),
        sport_key="soccer_epl",
        acquired_at_utc=NOW,
        headers={},
    ).events[0]

    reconciled, finding = reconcile_provider_event(
        event,
        registry=registry,
        acquired_at_utc=NOW,
    )

    assert reconciled is None
    assert finding is not None
    assert finding.reason == "home-team-unmatched"
    assert registry.participants == before
