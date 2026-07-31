"""Strict JSON parsing for The Odds API v4 responses."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Final, cast

from sports_analytics.core.exceptions import PermanentSourceError
from sports_analytics.data.codec import dumps_canonical_json
from sports_analytics.data.types import JsonValue
from sports_analytics.providers.the_odds_api.contracts import (
    ProviderBookmaker,
    ProviderEvent,
    ProviderMarket,
    ProviderOddsBatch,
    ProviderOutcome,
    ProviderQuota,
    ProviderSport,
    ProviderSportsCatalogue,
)

MAX_EVENTS: Final[int] = 500
MAX_BOOKMAKERS_PER_EVENT: Final[int] = 100
MAX_MARKETS_PER_BOOKMAKER: Final[int] = 20
MAX_OUTCOMES_PER_MARKET: Final[int] = 100
MAX_SPORTS: Final[int] = 500
MAX_TEXT: Final[int] = 300


def parse_odds_response(
    raw: bytes,
    *,
    sport_key: str,
    acquired_at_utc: datetime,
    headers: dict[str, str],
) -> ProviderOddsBatch:
    """Parse a bounded pre-match odds response with exact primitive types."""
    acquired = _utc(acquired_at_utc, "acquired_at_utc")
    value = _json(raw)
    rows = _array(value, "odds response", MAX_EVENTS)
    events = tuple(
        _event(item, index=index, acquired_at_utc=acquired, expected_sport_key=sport_key)
        for index, item in enumerate(rows)
    )
    ids = [item.provider_event_id for item in events]
    if len(ids) != len(set(ids)):
        raise PermanentSourceError("provider response contains duplicate event identities")
    ordered = tuple(
        sorted(events, key=lambda item: (item.commence_time_utc, item.provider_event_id))
    )
    payload = _json_safe(value)
    return ProviderOddsBatch(
        sport_key=sport_key,
        acquired_at_utc=acquired,
        events=ordered,
        quota=parse_quota_headers(headers),
        content_sha256=_digest(payload),
        canonical_payload=payload,
    )


def parse_sports_response(
    raw: bytes,
    *,
    acquired_at_utc: datetime,
    headers: dict[str, str],
) -> ProviderSportsCatalogue:
    """Parse the bounded sports catalogue used during key validation."""
    acquired = _utc(acquired_at_utc, "acquired_at_utc")
    value = _json(raw)
    rows = _array(value, "sports response", MAX_SPORTS)
    sports: list[ProviderSport] = []
    for index, item in enumerate(rows):
        row = _mapping(item, f"sports[{index}]")
        if set(row) != {"key", "group", "title", "description", "active", "has_outrights"}:
            raise PermanentSourceError("provider sports row fields are not exact")
        sports.append(
            ProviderSport(
                key=_text(row["key"], "sport key"),
                group=_text(row["group"], "sport group"),
                title=_text(row["title"], "sport title"),
                active=_bool(row["active"], "sport active"),
            )
        )
        _text(row["description"], "sport description")
        _bool(row["has_outrights"], "sport has_outrights")
    keys = [item.key for item in sports]
    if len(keys) != len(set(keys)):
        raise PermanentSourceError("provider sports response contains duplicate keys")
    payload = _json_safe(value)
    return ProviderSportsCatalogue(
        acquired_at_utc=acquired,
        sports=tuple(sorted(sports, key=lambda item: item.key)),
        quota=parse_quota_headers(headers),
        content_sha256=_digest(payload),
        canonical_payload=payload,
    )


def parse_quota_headers(headers: dict[str, str]) -> ProviderQuota:
    """Parse provider quota headers without trusting other response metadata."""
    normalized = {str(key).lower(): str(value) for key, value in headers.items()}
    return ProviderQuota(
        remaining=_non_negative_header(normalized.get("x-requests-remaining")),
        used=_non_negative_header(normalized.get("x-requests-used")),
        last_cost=_non_negative_header(normalized.get("x-requests-last")),
    )


def _event(
    value: object,
    *,
    index: int,
    acquired_at_utc: datetime,
    expected_sport_key: str,
) -> ProviderEvent:
    row = _mapping(value, f"events[{index}]")
    exact = {
        "id",
        "sport_key",
        "sport_title",
        "commence_time",
        "home_team",
        "away_team",
        "bookmakers",
    }
    if set(row) != exact:
        raise PermanentSourceError("provider event fields are not exact")
    event_id = _identifier_text(row["id"], "provider event id")
    sport_key = _identifier_text(row["sport_key"], "provider sport key")
    if sport_key != expected_sport_key:
        raise PermanentSourceError("provider event sport key contradicts request")
    start = _timestamp(row["commence_time"], "commence_time")
    if start <= acquired_at_utc:
        raise PermanentSourceError("provider event has already started")
    home = _text(row["home_team"], "home team")
    away = _text(row["away_team"], "away team")
    if home.casefold() == away.casefold():
        raise PermanentSourceError("provider event participants are contradictory")
    bookmaker_rows = _array(
        row["bookmakers"],
        "bookmakers",
        MAX_BOOKMAKERS_PER_EVENT,
    )
    bookmakers = tuple(
        _bookmaker(item, event_index=index, index=bookmaker_index)
        for bookmaker_index, item in enumerate(bookmaker_rows)
    )
    keys = [item.key for item in bookmakers]
    if len(keys) != len(set(keys)):
        raise PermanentSourceError("provider event contains duplicate bookmakers")
    return ProviderEvent(
        provider_event_id=event_id,
        sport_key=sport_key,
        sport_title=_text(row["sport_title"], "sport title"),
        commence_time_utc=start,
        home_team=home,
        away_team=away,
        bookmakers=tuple(sorted(bookmakers, key=lambda item: item.key)),
    )


def _bookmaker(value: object, *, event_index: int, index: int) -> ProviderBookmaker:
    row = _mapping(value, f"events[{event_index}].bookmakers[{index}]")
    if set(row) != {"key", "title", "last_update", "markets"}:
        raise PermanentSourceError("provider bookmaker fields are not exact")
    market_rows = _array(row["markets"], "bookmaker markets", MAX_MARKETS_PER_BOOKMAKER)
    markets = tuple(
        _market(item, bookmaker_index=index, index=market_index)
        for market_index, item in enumerate(market_rows)
    )
    identities = [
        (
            item.key,
            tuple(sorted(outcome.point for outcome in item.outcomes if outcome.point is not None)),
        )
        for item in markets
    ]
    if len(identities) != len(set(identities)):
        raise PermanentSourceError("provider bookmaker contains duplicate markets")
    return ProviderBookmaker(
        key=_identifier_text(row["key"], "bookmaker key"),
        title=_text(row["title"], "bookmaker title"),
        last_update_utc=_timestamp(row["last_update"], "bookmaker last_update"),
        markets=tuple(sorted(markets, key=lambda item: (item.key, item.last_update_utc))),
    )


def _market(value: object, *, bookmaker_index: int, index: int) -> ProviderMarket:
    row = _mapping(value, f"bookmakers[{bookmaker_index}].markets[{index}]")
    if set(row) != {"key", "last_update", "outcomes"}:
        raise PermanentSourceError("provider market fields are not exact")
    outcome_rows = _array(row["outcomes"], "market outcomes", MAX_OUTCOMES_PER_MARKET)
    outcomes = tuple(
        _outcome(item, index=outcome_index) for outcome_index, item in enumerate(outcome_rows)
    )
    identities = [(item.name.casefold(), item.point) for item in outcomes]
    if len(identities) != len(set(identities)):
        raise PermanentSourceError("provider market contains duplicate outcomes")
    return ProviderMarket(
        key=_identifier_text(row["key"], "market key"),
        last_update_utc=_timestamp(row["last_update"], "market last_update"),
        outcomes=outcomes,
    )


def _outcome(value: object, *, index: int) -> ProviderOutcome:
    row = _mapping(value, f"outcomes[{index}]")
    if set(row) not in ({"name", "price"}, {"name", "price", "point"}):
        raise PermanentSourceError("provider outcome fields are not exact")
    return ProviderOutcome(
        name=_text(row["name"], "outcome name"),
        price=_decimal(row["price"], "outcome price", greater_than_one=True),
        point=(
            None
            if "point" not in row
            else _decimal(row["point"], "outcome point", greater_than_one=False)
        ),
    )


def _json(raw: bytes) -> object:
    if not isinstance(raw, bytes) or not raw or b"\x00" in raw:
        raise PermanentSourceError("provider response bytes are invalid")
    try:
        return json.loads(raw.decode("utf-8"), parse_float=Decimal, parse_int=int)
    except (UnicodeError, json.JSONDecodeError, InvalidOperation) as exc:
        raise PermanentSourceError("provider response JSON is malformed") from exc


def _mapping(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(type(key) is not str for key in value):
        raise PermanentSourceError(f"{field} must be an object")
    return cast(dict[str, object], value)


def _array(value: object, field: str, maximum: int) -> list[object]:
    if not isinstance(value, list) or len(value) > maximum:
        raise PermanentSourceError(f"{field} must be a bounded array")
    return cast(list[object], value)


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip() or len(value) > MAX_TEXT:
        raise PermanentSourceError(f"{field} must be bounded trimmed text")
    if any(ord(character) < 32 for character in value):
        raise PermanentSourceError(f"{field} contains control characters")
    return value


def _identifier_text(value: object, field: str) -> str:
    text = _text(value, field)
    if any(character in text for character in ("/", "\\", "?", "#", ":")):
        raise PermanentSourceError(f"{field} contains prohibited characters")
    return text


def _bool(value: object, field: str) -> bool:
    if type(value) is not bool:
        raise PermanentSourceError(f"{field} must be boolean")
    return value


def _decimal(value: object, field: str, *, greater_than_one: bool) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, int | float | Decimal | str):
        raise PermanentSourceError(f"{field} must be numeric")
    try:
        parsed = Decimal(str(value))
    except InvalidOperation as exc:
        raise PermanentSourceError(f"{field} is malformed") from exc
    if not parsed.is_finite() or (greater_than_one and parsed <= 1):
        raise PermanentSourceError(f"{field} is outside the valid range")
    if not greater_than_one and not math.isfinite(float(parsed)):
        raise PermanentSourceError(f"{field} must be finite")
    return parsed


def _timestamp(value: object, field: str) -> datetime:
    text = _text(value, field)
    if not text.endswith("Z"):
        raise PermanentSourceError(f"{field} must be strict UTC")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as exc:
        raise PermanentSourceError(f"{field} is malformed") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise PermanentSourceError(f"{field} must be strict UTC")
    return parsed.astimezone(UTC)


def _utc(value: datetime, field: str) -> datetime:
    if value.tzinfo is None:
        raise PermanentSourceError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _non_negative_header(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value, 10)
    except ValueError as exc:
        raise PermanentSourceError("provider quota header is malformed") from exc
    if parsed < 0:
        raise PermanentSourceError("provider quota header is negative")
    return parsed


def _digest(value: object) -> str:
    return hashlib.sha256(dumps_canonical_json(cast(JsonValue, value)).encode("utf-8")).hexdigest()


def _json_safe(value: object) -> JsonValue:
    if value is None or type(value) in {bool, int, str}:
        return cast(JsonValue, value)
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {
            str(key): _json_safe(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    raise PermanentSourceError("provider response contains an unsupported JSON value")
