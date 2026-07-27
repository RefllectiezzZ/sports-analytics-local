"""Ordinary cookie-consent dismissal fixtures for Betano and Betclic."""

from __future__ import annotations

from sports_analytics.sources.browser.cookie_consent import (
    cookie_consent_selectors_for_provider,
    dismiss_cookie_consent,
)


class _FakeLocator:
    def __init__(self, *, count: int, click_ok: bool = True) -> None:
        self._count = count
        self._click_ok = click_ok
        self.clicked = False

    def count(self) -> int:
        return self._count

    @property
    def first(self) -> _FakeLocator:
        return self

    def click(self, timeout: int = 0) -> None:
        if not self._click_ok:
            raise RuntimeError("click failed")
        self.clicked = True


class _FakePage:
    def __init__(self, matching_selector: str | None) -> None:
        self._matching_selector = matching_selector
        self.locators: dict[str, _FakeLocator] = {}

    def locator(self, selector: str) -> _FakeLocator:
        if selector == self._matching_selector:
            locator = _FakeLocator(count=1)
        else:
            locator = _FakeLocator(count=0)
        self.locators[selector] = locator
        return locator


def test_betano_cookie_selectors_are_reviewed_only() -> None:
    selectors = cookie_consent_selectors_for_provider("betano-pt")
    assert selectors
    assert all("login" not in item.lower() for item in selectors)
    assert all("captcha" not in item.lower() for item in selectors)
    assert "#onetrust-accept-btn-handler" in selectors


def test_betclic_cookie_selectors_are_reviewed_only() -> None:
    selectors = cookie_consent_selectors_for_provider("betclic-pt")
    assert selectors
    assert all("register" not in item.lower() for item in selectors)
    assert "button:has-text('Aceitar')" in selectors


def test_betano_cookie_banner_dismissed() -> None:
    page = _FakePage("#onetrust-accept-btn-handler")
    assert dismiss_cookie_consent(page, provider_id="betano-pt") is True
    assert page.locators["#onetrust-accept-btn-handler"].clicked is True


def test_betclic_cookie_banner_dismissed() -> None:
    page = _FakePage("button:has-text('Aceitar')")
    assert dismiss_cookie_consent(page, provider_id="betclic-pt") is True
    assert page.locators["button:has-text('Aceitar')"].clicked is True


def test_no_banner_returns_false() -> None:
    page = _FakePage(None)
    assert dismiss_cookie_consent(page, provider_id="betano-pt") is False
