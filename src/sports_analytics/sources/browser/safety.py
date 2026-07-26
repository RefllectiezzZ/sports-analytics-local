"""Navigation and origin safety for ordinary browser automation.

Rejects arbitrary URLs, HTTP downgrades, private networks, embedded credentials,
and unapproved hosts. Does not implement stealth, CAPTCHA bypass, or proxy
rotation.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from typing import Final
from urllib.parse import urlparse

from sports_analytics.core.exceptions import PermanentSourceError
from sports_analytics.sources.browser.contracts import BrowserBlockReason

_FORBIDDEN_SCHEMES: Final[frozenset[str]] = frozenset(
    {
        "http",
        "file",
        "ftp",
        "data",
        "javascript",
        "about",
        "blob",
        "chrome",
        "chrome-extension",
    }
)


@dataclass(frozen=True, slots=True)
class ApprovedNavigation:
    """A validated HTTPS URL on an exact provider hostname allowlist."""

    url: str
    hostname: str
    path: str


def classify_block_signals(*, title: str | None, body_text: str | None) -> BrowserBlockReason | None:
    """Best-effort classification of CAPTCHA / access-denial pages.

    Stops acquisition; never attempts bypass.
    """
    haystack = " ".join(part for part in (title or "", body_text or "") if part).lower()
    if not haystack.strip():
        return None
    captcha_markers = ("captcha", "recaptcha", "hcaptcha", "cf-challenge", "challenge-platform")
    if any(marker in haystack for marker in captcha_markers):
        return BrowserBlockReason.CAPTCHA
    if "access denied" in haystack or "403 forbidden" in haystack:
        return BrowserBlockReason.ACCESS_DENIED
    if "sign in" in haystack or "log in" in haystack or "authentication required" in haystack:
        if "odds" not in haystack and "bet" not in haystack:
            return BrowserBlockReason.AUTHENTICATION_REQUIRED
    if "not available in your region" in haystack or "geo-restricted" in haystack:
        return BrowserBlockReason.REGIONAL_REFUSAL
    if "bot detected" in haystack or "automated access" in haystack or "unusual traffic" in haystack:
        return BrowserBlockReason.ANTI_AUTOMATION
    return None


def validate_provider_navigation_url(
    url: str,
    *,
    allowed_hostnames: frozenset[str] | set[str] | tuple[str, ...],
) -> ApprovedNavigation:
    """Validate a fixed provider navigation or redirect target."""
    if not isinstance(url, str) or not url:
        msg = "navigation URL must be a non-empty string"
        raise PermanentSourceError(msg)
    if any(ch.isspace() for ch in url):
        msg = "navigation URL must not contain whitespace"
        raise PermanentSourceError(msg)
    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    if scheme in _FORBIDDEN_SCHEMES or scheme != "https":
        msg = f"navigation URL must use HTTPS without alternative schemes: {scheme or 'missing'}"
        raise PermanentSourceError(msg)
    if parsed.username is not None or parsed.password is not None:
        msg = "navigation URL must not embed credentials"
        raise PermanentSourceError(msg)
    hostname = (parsed.hostname or "").lower()
    if not hostname:
        msg = "navigation URL must include a hostname"
        raise PermanentSourceError(msg)
    allowlist = {item.lower() for item in allowed_hostnames}
    if hostname not in allowlist:
        msg = f"hostname {hostname!r} is outside the provider allowlist"
        raise PermanentSourceError(msg)
    _reject_private_or_loopback_hostname(hostname)
    path = parsed.path or "/"
    return ApprovedNavigation(url=url, hostname=hostname, path=path)


def _reject_private_or_loopback_hostname(hostname: str) -> None:
    if hostname in {"localhost", "localhost.localdomain"}:
        msg = "localhost and loopback hosts are forbidden"
        raise PermanentSourceError(msg)
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        # Hostname is not a literal IP. Resolve only for safety checks when possible;
        # failure to resolve is not treated as approval of private networks.
        try:
            infos = socket.getaddrinfo(hostname, None)
        except OSError:
            return
        for info in infos:
            raw = info[4][0]
            try:
                _reject_ip(ipaddress.ip_address(raw))
            except ValueError:
                continue
        return
    _reject_ip(address)


def _reject_ip(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> None:
    if (
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    ):
        msg = f"private, loopback, or reserved address is forbidden: {address}"
        raise PermanentSourceError(msg)
