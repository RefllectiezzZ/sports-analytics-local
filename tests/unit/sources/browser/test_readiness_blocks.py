"""Readiness block detection during polling."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from sports_analytics.sources.browser.contracts import BrowserBlockReason
from sports_analytics.sources.browser.readiness import (
    ReadinessBlockedError,
    classify_readiness_block,
    wait_for_readiness,
)


@dataclass
class FakePage:
    title_value: str
    body_value: str
    waits: int = 0

    def title(self) -> str:
        return self.title_value

    def inner_text(self, selector: str) -> str:
        del selector
        return self.body_value

    def wait_for_timeout(self, timeout: float) -> None:
        del timeout
        self.waits += 1


def test_cloudflare_splash_blocks_immediately() -> None:
    page = FakePage("Just a moment...", "Checking your browser")
    with pytest.raises(ReadinessBlockedError) as exc:
        wait_for_readiness(
            page,
            predicate=lambda _: False,
            timeout_ms=15_000,
            poll_ms=250,
            page_route_id="betano-home",
        )
    assert exc.value.block_reason is BrowserBlockReason.ANTI_AUTOMATION
    assert page.waits == 0


def test_captcha_blocks_immediately() -> None:
    page = FakePage("Security Check", "Please complete the captcha challenge")
    with pytest.raises(ReadinessBlockedError):
        wait_for_readiness(
            page,
            predicate=lambda _: False,
            timeout_ms=15_000,
            poll_ms=250,
            page_route_id="betclic-home",
        )
    assert page.waits == 0


def test_access_denied_blocks_immediately() -> None:
    page = FakePage("403 Forbidden", "Access denied")
    blocked = classify_readiness_block(page)
    assert blocked is BrowserBlockReason.ACCESS_DENIED


def test_regional_refusal_blocks_immediately() -> None:
    page = FakePage("Unavailable", "This service is not available in your region")
    blocked = classify_readiness_block(page)
    assert blocked is BrowserBlockReason.REGIONAL_REFUSAL


def test_genuine_loading_times_out() -> None:
    page = FakePage("Loading", "")
    with pytest.raises(Exception, match="readiness timeout"):
        wait_for_readiness(
            page,
            predicate=lambda _: False,
            timeout_ms=500,
            poll_ms=250,
            page_route_id="slow-route",
        )
    assert page.waits >= 1


def test_successful_readiness_returns_without_timeout() -> None:
    page = FakePage("Betano Sports", "Football odds and fixtures")
    wait_for_readiness(
        page,
        predicate=lambda item: "Football" in item.inner_text("body"),
        timeout_ms=1_000,
        poll_ms=100,
        page_route_id="ok-route",
    )
