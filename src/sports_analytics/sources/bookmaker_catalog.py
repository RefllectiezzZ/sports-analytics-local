"""Shared bookmaker provider catalog contracts and fixed route helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from sports_analytics.core.exceptions import PermanentSourceError
from sports_analytics.data.types import validate_identifier
from sports_analytics.sources.browser.safety import validate_provider_navigation_url

SUPPORTED_BOOKMAKER_SPORTS: Final[tuple[str, ...]] = ("basketball", "football", "tennis")


@dataclass(frozen=True, slots=True)
class BookmakerProviderCatalog:
    """Project-owned fixed navigation catalog for one bookmaker provider."""

    provider_id: str
    display_name: str
    adapter_version: str
    parser_version: str
    allowed_hostnames: frozenset[str]
    locale: str
    jurisdiction: str
    sport_routes: dict[str, tuple[tuple[str, str], ...]]
    starting_route_id: str
    starting_url: str

    def __post_init__(self) -> None:
        validate_identifier(self.provider_id, field_name="provider_id")
        validate_identifier(self.adapter_version, field_name="adapter_version")
        validate_identifier(self.parser_version, field_name="parser_version")
        if not self.allowed_hostnames:
            msg = "allowed_hostnames must not be empty"
            raise PermanentSourceError(msg)
        validate_provider_navigation_url(
            self.starting_url,
            allowed_hostnames=self.allowed_hostnames,
        )
        for sport, routes in self.sport_routes.items():
            validate_identifier(sport, field_name="sport")
            if sport not in SUPPORTED_BOOKMAKER_SPORTS:
                msg = f"unsupported catalog sport: {sport}"
                raise PermanentSourceError(msg)
            for route_id, url in routes:
                validate_identifier(route_id, field_name="page_route_id")
                validate_provider_navigation_url(url, allowed_hostnames=self.allowed_hostnames)

    def routes_for_sport(self, sport: str) -> tuple[tuple[str, str], ...]:
        """Return fixed routes for ``sport`` or raise for unsupported sports."""
        try:
            return self.sport_routes[sport]
        except KeyError as exc:
            msg = f"unsupported sport for provider {self.provider_id}: {sport}"
            raise PermanentSourceError(msg) from exc


FORBIDDEN_JOB_PAYLOAD_KEYS: Final[frozenset[str]] = frozenset(
    {
        "url",
        "urls",
        "hostname",
        "hostnames",
        "browser_executable",
        "executable_path",
        "javascript",
        "script",
        "selector",
        "selectors",
        "cookie",
        "cookies",
        "header",
        "headers",
        "credential",
        "credentials",
        "password",
        "username",
        "token",
        "proxy",
        "proxies",
    }
)


def reject_forbidden_job_controls(payload: dict[str, object]) -> None:
    """Reject arbitrary browser/network controls from job payloads."""
    unknown = sorted(key for key in payload if key.lower() in FORBIDDEN_JOB_PAYLOAD_KEYS)
    if unknown:
        msg = f"job payload contains forbidden control keys: {', '.join(unknown)}"
        raise PermanentSourceError(msg)
