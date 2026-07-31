"""Explicit competition and market mapping for The Odds API."""

from __future__ import annotations

import hashlib
import unicodedata
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Final

from sports_analytics.bookmakers.operator_quotes import (
    FOOTBALL_RULES_SCOPE,
    REGULATION_SCOPE,
    OperatorQuoteInput,
    OperatorQuoteSourceKind,
)
from sports_analytics.core.exceptions import ConfigurationError, ValueEvaluationError
from sports_analytics.providers.the_odds_api.contracts import (
    ProviderBookmaker,
    ProviderEvent,
)
from sports_analytics.sports.football.participant_registry import (
    FootballParticipantRegistry,
    RegisteredFootballParticipant,
)
from sports_analytics.upcoming_events import UpcomingEvent

COMPETITION_TO_SPORT_KEY: Final[dict[str, str]] = {
    "eng-premier-league": "soccer_epl",
}
SPORT_KEY_TO_COMPETITION: Final[dict[str, str]] = {
    value: key for key, value in COMPETITION_TO_SPORT_KEY.items()
}
PROVIDER_TEAM_ALIASES: Final[dict[str, str]] = {
    "brighton and hove albion": "brighton",
    "manchester city": "man city",
    "manchester united": "man united",
    "newcastle united": "newcastle",
    "nottingham forest": "nott'm forest",
    "tottenham hotspur": "tottenham",
    "west ham united": "west ham",
    "wolverhampton wanderers": "wolves",
}


@dataclass(frozen=True, slots=True)
class ReconciledProviderEvent:
    """Provider event linked to one canonical upcoming event."""

    provider: ProviderEvent
    canonical: UpcomingEvent


@dataclass(frozen=True, slots=True)
class UnresolvedProviderEvent:
    """Fail-closed provider event reconciliation finding."""

    provider_event_id: str
    home_team: str
    away_team: str
    reason: str


def provider_sport_key(competition_id: str) -> str:
    try:
        return COMPETITION_TO_SPORT_KEY[competition_id]
    except KeyError as exc:
        raise ConfigurationError("competition has no explicit The Odds API mapping") from exc


def reconcile_provider_event(
    event: ProviderEvent,
    *,
    registry: FootballParticipantRegistry,
    acquired_at_utc: object,
) -> tuple[ReconciledProviderEvent | None, UnresolvedProviderEvent | None]:
    """Resolve exact aliases only and never create a participant."""
    from datetime import datetime

    if not isinstance(acquired_at_utc, datetime):
        raise ConfigurationError("provider acquisition timestamp is invalid")
    competition = SPORT_KEY_TO_COMPETITION.get(event.sport_key)
    if competition is None:
        return None, _unresolved(event, "provider-sport-key-unmapped")
    home = _resolve_participant(
        event.home_team,
        registry=registry,
        competition_id=competition,
        event_date=event.commence_time_utc.date(),
    )
    away = _resolve_participant(
        event.away_team,
        registry=registry,
        competition_id=competition,
        event_date=event.commence_time_utc.date(),
    )
    if isinstance(home, str):
        return None, _unresolved(event, f"home-team-{home}")
    if isinstance(away, str):
        return None, _unresolved(event, f"away-team-{away}")
    if home.canonical_participant_id == away.canonical_participant_id:
        return None, _unresolved(event, "participants-contradictory")
    season_year = (
        event.commence_time_utc.year
        if event.commence_time_utc.month >= 7
        else event.commence_time_utc.year - 1
    )
    season = f"{season_year:04d}-{season_year + 1:04d}"
    occurrence = _provider_identifier("toa-event", event.provider_event_id)
    batch = _provider_identifier(
        "toa-batch",
        f"{event.sport_key}:{acquired_at_utc.isoformat()}",
    )
    source = _provider_identifier("toa-observation", event.provider_event_id)
    from sports_analytics.sports.identifiers import build_canonical_event_id, build_season_id

    season_id = build_season_id(competition_id=competition, label=season)
    canonical_id = build_canonical_event_id(
        sport_code="football",
        competition_id=competition,
        season_id=season_id,
        home_canonical_participant_id=home.canonical_participant_id,
        away_canonical_participant_id=away.canonical_participant_id,
        event_occurrence_key=occurrence,
    )
    canonical = UpcomingEvent(
        canonical_event_id=canonical_id,
        sport_code="football",
        competition_id=competition,
        season_id=season_id,
        season_label=season,
        canonical_home_participant_id=home.canonical_participant_id,
        canonical_away_participant_id=away.canonical_participant_id,
        event_start_utc=event.commence_time_utc,
        event_occurrence_key=occurrence,
        event_status="scheduled",
        observed_at_utc=acquired_at_utc,
        source_kind="provider-api",
        source_observation_id=source,
        neutral_venue=None,
        operator_note=(f"external-provider-event-id={event.provider_event_id}"),
        import_batch_id=batch,
    )
    return ReconciledProviderEvent(event, canonical), None


def translate_bookmaker_quotes(
    reconciled: ReconciledProviderEvent,
    *,
    acquired_at_utc: object,
    enabled_markets: tuple[str, ...],
    freshness: timedelta = timedelta(minutes=30),
) -> tuple[OperatorQuoteInput, ...]:
    """Translate complete same-bookmaker markets into canonical quote inputs."""
    from datetime import datetime

    if not isinstance(acquired_at_utc, datetime):
        raise ValueEvaluationError("provider acquisition timestamp is invalid")
    quotes: list[OperatorQuoteInput] = []
    for bookmaker in reconciled.provider.bookmakers:
        for market in bookmaker.markets:
            if market.key not in enabled_markets:
                continue
            if market.key == "h2h":
                quotes.extend(
                    _h2h_quotes(
                        reconciled,
                        bookmaker=bookmaker,
                        market=market,
                        acquired_at_utc=acquired_at_utc,
                        freshness=freshness,
                    )
                )
            elif market.key == "totals":
                quotes.extend(
                    _totals_quotes(
                        reconciled,
                        bookmaker=bookmaker,
                        market=market,
                        acquired_at_utc=acquired_at_utc,
                        freshness=freshness,
                    )
                )
    return tuple(
        sorted(
            quotes,
            key=lambda item: (
                item.provider_id,
                item.market_family,
                "" if item.line_value is None else format(item.line_value, "f"),
                item.outcome_key,
            ),
        )
    )


def _h2h_quotes(
    reconciled: ReconciledProviderEvent,
    *,
    bookmaker: ProviderBookmaker,
    market: object,
    acquired_at_utc: object,
    freshness: timedelta,
) -> tuple[OperatorQuoteInput, ...]:
    from datetime import datetime

    from sports_analytics.providers.the_odds_api.contracts import ProviderMarket

    assert isinstance(market, ProviderMarket)
    assert isinstance(acquired_at_utc, datetime)
    expected = {
        _normalize(reconciled.provider.home_team): "home",
        "draw": "draw",
        _normalize(reconciled.provider.away_team): "away",
    }
    mapped: dict[str, Decimal] = {}
    for outcome in market.outcomes:
        if outcome.point is not None:
            return ()
        key = expected.get(_normalize(outcome.name))
        if key is None or key in mapped:
            return ()
        mapped[key] = outcome.price
    if set(mapped) != {"home", "draw", "away"}:
        return ()
    return tuple(
        _quote(
            reconciled,
            bookmaker=bookmaker,
            market_family="match-result",
            outcome_key=outcome,
            line=None,
            price=mapped[outcome],
            observed_at_utc=market.last_update_utc,
            acquired_at_utc=acquired_at_utc,
            freshness=freshness,
        )
        for outcome in ("home", "draw", "away")
    )


def _totals_quotes(
    reconciled: ReconciledProviderEvent,
    *,
    bookmaker: ProviderBookmaker,
    market: object,
    acquired_at_utc: object,
    freshness: timedelta,
) -> tuple[OperatorQuoteInput, ...]:
    from datetime import datetime

    from sports_analytics.providers.the_odds_api.contracts import ProviderMarket

    assert isinstance(market, ProviderMarket)
    assert isinstance(acquired_at_utc, datetime)
    lines: dict[Decimal, dict[str, Decimal]] = {}
    for outcome in market.outcomes:
        name = _normalize(outcome.name)
        if name not in {"over", "under"} or outcome.point is None:
            continue
        line = lines.setdefault(outcome.point, {})
        if name in line:
            continue
        line[name] = outcome.price
    quotes: list[OperatorQuoteInput] = []
    for point in sorted(lines):
        prices = lines[point]
        if set(prices) != {"over", "under"}:
            continue
        for outcome_key in ("over", "under"):
            quotes.append(
                _quote(
                    reconciled,
                    bookmaker=bookmaker,
                    market_family="total-goals",
                    outcome_key=outcome_key,
                    line=point,
                    price=prices[outcome_key],
                    observed_at_utc=market.last_update_utc,
                    acquired_at_utc=acquired_at_utc,
                    freshness=freshness,
                )
            )
    return tuple(quotes)


def _quote(
    reconciled: ReconciledProviderEvent,
    *,
    bookmaker: ProviderBookmaker,
    market_family: str,
    outcome_key: str,
    line: Decimal | None,
    price: Decimal,
    observed_at_utc: object,
    acquired_at_utc: object,
    freshness: timedelta,
) -> OperatorQuoteInput:
    from datetime import datetime

    assert isinstance(observed_at_utc, datetime)
    assert isinstance(acquired_at_utc, datetime)
    observed = min(observed_at_utc, acquired_at_utc)
    provider_id = _provider_identifier("toa-book", bookmaker.key)
    return OperatorQuoteInput(
        provider_id=provider_id,
        provider_display_name=bookmaker.title,
        sport_code="football",
        canonical_event_id=reconciled.canonical.canonical_event_id,
        market_family=market_family,
        outcome_key=outcome_key,
        line_value=line,
        market_period="full-match",
        participant_scope="event",
        canonical_participant_id=None,
        overtime_scope=REGULATION_SCOPE,
        rules_scope=FOOTBALL_RULES_SCOPE,
        offered_decimal_odds=price,
        observed_at_utc=observed,
        valid_until_utc=min(observed + freshness, reconciled.provider.commence_time_utc),
        source_kind=OperatorQuoteSourceKind.VERIFIED_SOURCE,
        operator_note=(f"external-provider-event-id={reconciled.provider.provider_event_id}"),
        import_batch_id=_provider_identifier(
            "toa-quotes",
            f"{reconciled.provider.provider_event_id}:{acquired_at_utc.isoformat()}",
        ),
    )


def _resolve_participant(
    name: str,
    *,
    registry: FootballParticipantRegistry,
    competition_id: str,
    event_date: object,
) -> RegisteredFootballParticipant | str:
    from datetime import date

    assert isinstance(event_date, date)
    normalized = _normalize(name)
    alias = PROVIDER_TEAM_ALIASES.get(normalized, normalized)
    matches = []
    for item in registry.participants_for_competition(competition_id):
        candidate_names = {
            _normalize(item.canonical_display_name),
            _normalize(item.source_participant_id),
        }
        if normalized in candidate_names or alias in candidate_names:
            matches.append(item)
    identities = {item.canonical_participant_id for item in matches}
    if not identities:
        return "unmatched"
    if len(identities) != 1:
        return "ambiguous"
    try:
        return registry.require_registered_participant(
            next(iter(identities)),
            competition_id=competition_id,
            event_date=event_date,
        )
    except ConfigurationError:
        return "outside-validity"


def _unresolved(event: ProviderEvent, reason: str) -> UnresolvedProviderEvent:
    return UnresolvedProviderEvent(
        provider_event_id=event.provider_event_id,
        home_team=event.home_team,
        away_team=event.away_team,
        reason=reason,
    )


def _normalize(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(normalized.replace("&", " and ").split())


def _provider_identifier(prefix: str, value: str) -> str:
    safe = "".join(
        character if character.isascii() and (character.isalnum() or character in "-_.") else "-"
        for character in value.casefold()
    ).strip("-")
    while "--" in safe:
        safe = safe.replace("--", "-")
    if not safe:
        safe = hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]
    return f"{prefix}-{safe}"[:128].rstrip("-")
