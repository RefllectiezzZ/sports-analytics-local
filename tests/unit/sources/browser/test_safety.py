"""Offline browser navigation safety and block-classification tests."""

from __future__ import annotations

import pytest

from sports_analytics.core.exceptions import PermanentSourceError
from sports_analytics.sources.betano.catalog import ALLOWED_HOSTNAMES as BETANO_HOSTS
from sports_analytics.sources.browser.contracts import BrowserBlockReason
from sports_analytics.sources.browser.safety import (
    classify_block_signals,
    validate_provider_navigation_url,
)

ALLOWED = frozenset(BETANO_HOSTS)


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/odds",
        "https://www.betclic.pt/",
        "https://betano.com/",
    ],
)
def test_rejects_arbitrary_and_cross_host_urls(url: str) -> None:
    with pytest.raises(PermanentSourceError, match="outside the provider allowlist"):
        validate_provider_navigation_url(url, allowed_hostnames=ALLOWED)


@pytest.mark.parametrize(
    "url",
    [
        "http://www.betano.pt/",
        "ftp://www.betano.pt/",
        "file:///tmp/odds.html",
        "javascript:alert(1)",
        "data:text/html,hi",
    ],
)
def test_rejects_http_and_forbidden_schemes(url: str) -> None:
    with pytest.raises(PermanentSourceError, match="must use HTTPS"):
        validate_provider_navigation_url(url, allowed_hostnames=ALLOWED)


@pytest.mark.parametrize(
    "url",
    [
        "https://localhost/",
        "https://localhost.localdomain/path",
        "https://127.0.0.1/",
        "https://[::1]/",
        "https://10.0.0.8/",
        "https://192.168.1.10/",
        "https://172.16.5.5/",
    ],
)
def test_rejects_localhost_loopback_and_private_ips(url: str) -> None:
    hosts = ALLOWED | {
        "localhost",
        "localhost.localdomain",
        "127.0.0.1",
        "::1",
        "10.0.0.8",
        "192.168.1.10",
        "172.16.5.5",
    }
    with pytest.raises(PermanentSourceError, match="forbidden"):
        validate_provider_navigation_url(url, allowed_hostnames=hosts)


def test_rejects_embedded_credentials() -> None:
    with pytest.raises(PermanentSourceError, match="credentials"):
        validate_provider_navigation_url(
            "https://user:pass@www.betano.pt/sport/futebol/",
            allowed_hostnames=ALLOWED,
        )


def test_accepts_allowlisted_https_url() -> None:
    approved = validate_provider_navigation_url(
        "https://www.betano.pt/sport/futebol/",
        allowed_hostnames=ALLOWED,
    )
    assert approved.hostname == "www.betano.pt"
    assert approved.path.startswith("/sport/")


@pytest.mark.parametrize(
    ("title", "body", "expected"),
    [
        ("Security Check", "Please complete the captcha", BrowserBlockReason.CAPTCHA),
        ("Blocked", "Access Denied", BrowserBlockReason.ACCESS_DENIED),
        ("Sign in", "authentication required", BrowserBlockReason.AUTHENTICATION_REQUIRED),
        ("Unavailable", "not available in your region", BrowserBlockReason.REGIONAL_REFUSAL),
        ("Unusual traffic", "bot detected", BrowserBlockReason.ANTI_AUTOMATION),
        ("Odds page", "Football pre-match odds", None),
    ],
)
def test_classify_block_signals(
    title: str,
    body: str,
    expected: BrowserBlockReason | None,
) -> None:
    assert classify_block_signals(title=title, body_text=body) is expected
