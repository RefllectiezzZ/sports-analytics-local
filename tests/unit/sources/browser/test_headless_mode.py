"""Ordinary headless Chromium launch presentation tests."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from sports_analytics.core.settings import BookmakersSettings
from sports_analytics.sources.browser.contracts import BrowserMode
from sports_analytics.sources.browser.playwright_runtime import PlaywrightBrowserSession


class _Page:
    def on(self, *_args: object) -> None:
        return None


class _Context:
    def __init__(self) -> None:
        self.page = _Page()

    def new_page(self) -> _Page:
        return self.page

    def close(self) -> None:
        return None


class _Browser:
    def __init__(self) -> None:
        self.context_kwargs: dict[str, object] = {}

    def new_context(self, **kwargs: object) -> _Context:
        self.context_kwargs = kwargs
        return _Context()

    def close(self) -> None:
        return None


class _PlaywrightContext:
    def __init__(self, launch_calls: list[dict[str, object]]) -> None:
        browser = _Browser()

        def launch(**kwargs: object) -> _Browser:
            launch_calls.append(kwargs)
            return browser

        self.playwright = SimpleNamespace(chromium=SimpleNamespace(launch=launch))

    def __enter__(self) -> object:
        return self.playwright

    def __exit__(self, *_args: object) -> None:
        return None


def test_bookmaker_settings_default_to_headless() -> None:
    settings = BookmakersSettings()
    assert settings.browser_mode == "headless"
    assert settings.maximum_response_bytes == 2_097_152
    assert settings.maximum_total_capture_bytes == 16_777_216
    assert settings.event_detail_concurrency == 1
    assert settings.minimum_event_detail_interval_ms == 1_000
    assert settings.navigation_timeout_ms == 30_000
    assert settings.explicit_retry_limit == 0
    assert BookmakersSettings(browser_mode="visible").browser_mode == "visible"


def test_bookmaker_capture_settings_reject_invalid_budget_and_retry() -> None:
    with pytest.raises(ValueError, match="cover"):
        BookmakersSettings(
            maximum_response_bytes=2_048,
            maximum_total_capture_bytes=1_024,
        )
    with pytest.raises(ValueError):
        BookmakersSettings(explicit_retry_limit=1)  # type: ignore[arg-type]


def test_launch_headless_flag_only_tracks_presentation(monkeypatch) -> None:
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        "playwright.sync_api.sync_playwright",
        lambda: _PlaywrightContext(calls),
    )
    session = PlaywrightBrowserSession(clock=lambda: datetime(2026, 7, 28, 12, 0, tzinfo=UTC))
    for mode in (BrowserMode.HEADLESS, BrowserMode.VISIBLE, BrowserMode.VISIBLE_MINIMIZED):
        session.acquire(
            provider_id="betano-pt",
            sport="football",
            acquisition_cycle_id=f"cycle-{mode.value}",
            allowed_hostnames=frozenset({"www.betano.pt"}),
            start_urls=(),
            observed_at_utc=datetime(2026, 7, 28, 12, 0, tzinfo=UTC),
            browser_mode=mode,
        )
    assert calls == [{"headless": True}, {"headless": False}, {"headless": False}]
