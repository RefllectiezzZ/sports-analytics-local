"""Ordinary public cookie-consent dismissal for visible browser acquisition."""

from __future__ import annotations

from typing import Any, Final

from sports_analytics.bookmakers.types import PROVIDER_BETANO_PT, PROVIDER_BETCLIC_PT

#: Reviewed selectors only. No login, CAPTCHA, or account prompts.
_BETANO_SELECTORS: Final[tuple[str, ...]] = (
    "#onetrust-accept-btn-handler",
    "button#onetrust-accept-btn-handler",
    "button:has-text('Aceitar Todos')",
    "button:has-text('Aceitar todos')",
    "button:has-text('Aceitar')",
)

_BETCLIC_SELECTORS: Final[tuple[str, ...]] = (
    "#onetrust-accept-btn-handler",
    "button#popin_tc_privacy_button_2",
    "button:has-text('Aceitar')",
    "button:has-text('Concordo')",
)

_PROVIDER_SELECTORS: Final[dict[str, tuple[str, ...]]] = {
    PROVIDER_BETANO_PT: _BETANO_SELECTORS,
    PROVIDER_BETCLIC_PT: _BETCLIC_SELECTORS,
}


def cookie_consent_selectors_for_provider(provider_id: str) -> tuple[str, ...]:
    """Return reviewed cookie-banner selectors for one provider."""
    return _PROVIDER_SELECTORS.get(provider_id, ())


def dismiss_cookie_consent(page: Any, *, provider_id: str) -> bool:
    """Dismiss an ordinary public cookie banner when it blocks content.

    Returns whether a banner control was clicked. Never handles login,
    registration, CAPTCHA, or account prompts.
    """
    for selector in cookie_consent_selectors_for_provider(provider_id):
        try:
            locator = page.locator(selector)
            count = locator.count()
            if count < 1:
                continue
            locator.first.click(timeout=1_500)
            return True
        except Exception:  # noqa: BLE001 - best-effort public consent only
            continue
    return False
