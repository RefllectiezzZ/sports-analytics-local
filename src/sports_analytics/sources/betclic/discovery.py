"""Safe browser-observed Betclic offering transport discovery."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

from sports_analytics.core.exceptions import PermanentSourceError
from sports_analytics.sources.betclic.catalog import (
    OFFERING_RESPONSE_HOSTNAME,
    OFFERING_RESPONSE_ROUTES,
)
from sports_analytics.sources.browser.safety import classify_https_public_url


@dataclass(frozen=True, slots=True)
class ApprovedBetclicResponse:
    """Exact reviewed response route naturally observed from the public page."""

    hostname: str
    path_template: str
    route_id: str
    metadata_only: bool


def approve_betclic_response_url(url: str) -> ApprovedBetclicResponse:
    """Approve only the exact HTTPS offering host and reviewed RPC paths."""
    parsed = urlparse(url)
    if parsed.scheme.casefold() != "https":
        msg = "Betclic response scheme is not approved"
        raise PermanentSourceError(msg)
    if parsed.username is not None or parsed.password is not None:
        msg = "Betclic response URL must not embed credentials"
        raise PermanentSourceError(msg)
    try:
        port = parsed.port
    except ValueError as exc:
        msg = "Betclic response port is invalid"
        raise PermanentSourceError(msg) from exc
    if port not in {None, 443}:
        msg = "Betclic response port is not approved"
        raise PermanentSourceError(msg)
    if parsed.hostname != OFFERING_RESPONSE_HOSTNAME:
        msg = "Betclic response hostname is not approved"
        raise PermanentSourceError(msg)
    if classify_https_public_url(url) != OFFERING_RESPONSE_HOSTNAME:
        msg = "Betclic response origin is not public and approved"
        raise PermanentSourceError(msg)
    route_id = OFFERING_RESPONSE_ROUTES.get(parsed.path)
    if route_id is None:
        msg = "Betclic response path is not approved"
        raise PermanentSourceError(msg)
    return ApprovedBetclicResponse(
        hostname=OFFERING_RESPONSE_HOSTNAME,
        path_template=parsed.path,
        route_id=route_id,
        metadata_only=parsed.path.endswith("/GetLiveCount"),
    )
