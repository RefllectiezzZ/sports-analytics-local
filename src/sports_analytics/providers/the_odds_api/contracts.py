"""Bounded provider contracts with no credential-bearing fields."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class ProviderQuota:
    """Quota telemetry returned by The Odds API."""

    remaining: int | None
    used: int | None
    last_cost: int | None


@dataclass(frozen=True, slots=True)
class ProviderOutcome:
    """One provider market outcome."""

    name: str
    price: Decimal
    point: Decimal | None


@dataclass(frozen=True, slots=True)
class ProviderMarket:
    """One bounded provider market."""

    key: str
    last_update_utc: datetime
    outcomes: tuple[ProviderOutcome, ...]


@dataclass(frozen=True, slots=True)
class ProviderBookmaker:
    """One bookmaker and all valid markets returned for an event."""

    key: str
    title: str
    last_update_utc: datetime
    markets: tuple[ProviderMarket, ...]


@dataclass(frozen=True, slots=True)
class ProviderEvent:
    """One strictly parsed pre-match football event."""

    provider_event_id: str
    sport_key: str
    sport_title: str
    commence_time_utc: datetime
    home_team: str
    away_team: str
    bookmakers: tuple[ProviderBookmaker, ...]


@dataclass(frozen=True, slots=True)
class ProviderOddsBatch:
    """One complete provider response and safe lineage metadata."""

    sport_key: str
    acquired_at_utc: datetime
    events: tuple[ProviderEvent, ...]
    quota: ProviderQuota
    content_sha256: str
    canonical_payload: object


@dataclass(frozen=True, slots=True)
class ProviderSport:
    """One sports-catalogue row."""

    key: str
    group: str
    title: str
    active: bool


@dataclass(frozen=True, slots=True)
class ProviderSportsCatalogue:
    """A bounded sports catalogue suitable for a local 24-hour cache."""

    acquired_at_utc: datetime
    sports: tuple[ProviderSport, ...]
    quota: ProviderQuota
    content_sha256: str
    canonical_payload: object
