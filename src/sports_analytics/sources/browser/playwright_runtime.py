"""Playwright Chromium runtime for visible localhost bookmaker acquisition.

Production acquisition uses a locally installed Chromium browser in visible or
best-effort minimized visible mode. Headless production scraping, stealth
plugins, fingerprint rotation, CAPTCHA solving, and credential automation are
forbidden.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

from sports_analytics.core.exceptions import (
    PermanentSourceError,
    RetryableSourceError,
)
from sports_analytics.sources.browser.contracts import (
    BrowserAcquisitionResult,
    BrowserBlockReason,
    BrowserMode,
    BrowserPageObservation,
    BrowserResponseObservation,
)
from sports_analytics.sources.browser.safety import (
    classify_block_signals,
    validate_provider_navigation_url,
)

_LOGGER = logging.getLogger(__name__)


class BrowserSession(Protocol):
    """Injectable browser session abstraction for tests and runtime."""

    def acquire(
        self,
        *,
        provider_id: str,
        sport: str,
        acquisition_cycle_id: str,
        allowed_hostnames: frozenset[str],
        start_urls: Sequence[tuple[str, str]],
        observed_at_utc: datetime,
        browser_mode: BrowserMode,
    ) -> BrowserAcquisitionResult:
        """Collect page and first-party JSON observations for fixed routes."""


@dataclass(frozen=True, slots=True)
class ProviderRoute:
    """Fixed project-owned route identifier and URL."""

    page_route_id: str
    url: str


class PlaywrightBrowserSession:
    """Visible Chromium session backed by Playwright when installed locally."""

    def __init__(
        self,
        *,
        navigation_timeout_ms: int = 30_000,
        maximum_response_bytes: int = 2_097_152,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if navigation_timeout_ms < 1:
            msg = "navigation_timeout_ms must be positive"
            raise PermanentSourceError(msg)
        self._navigation_timeout_ms = navigation_timeout_ms
        self._maximum_response_bytes = maximum_response_bytes
        self._clock = clock or (lambda: datetime.now(UTC))

    def acquire(
        self,
        *,
        provider_id: str,
        sport: str,
        acquisition_cycle_id: str,
        allowed_hostnames: frozenset[str],
        start_urls: Sequence[tuple[str, str]],
        observed_at_utc: datetime,
        browser_mode: BrowserMode,
    ) -> BrowserAcquisitionResult:
        if browser_mode not in {BrowserMode.VISIBLE, BrowserMode.VISIBLE_MINIMIZED}:
            msg = "headless production scraping is forbidden"
            raise PermanentSourceError(msg)
        try:
            from playwright.sync_api import Error as PlaywrightError
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            msg = (
                "Playwright is not installed. Install the package and run "
                "`python -m playwright install chromium` locally."
            )
            raise PermanentSourceError(msg) from exc

        pages: list[BrowserPageObservation] = []
        responses: list[BrowserResponseObservation] = []
        warnings: list[str] = []
        block_reason: BrowserBlockReason | None = None

        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=False)
                context = browser.new_context(locale="pt-PT")
                try:
                    if browser_mode is BrowserMode.VISIBLE_MINIMIZED:
                        try:
                            context.new_page()  # ensure a page exists for window state
                        except PlaywrightError:
                            warnings.append("unable to prepare minimized visible window")
                    page = context.new_page()

                    def _on_response(response: object) -> None:
                        try:
                            response_url = str(getattr(response, "url", ""))
                            validate_provider_navigation_url(
                                response_url,
                                allowed_hostnames=allowed_hostnames,
                            )
                            headers = getattr(response, "headers", {}) or {}
                            content_type = None
                            if isinstance(headers, dict):
                                content_type = headers.get("content-type") or headers.get(
                                    "Content-Type"
                                )
                            if content_type is None or "json" not in str(content_type).lower():
                                return
                            body = response.text()  # type: ignore[attr-defined]
                            if len(body.encode("utf-8")) > self._maximum_response_bytes:
                                warnings.append("json-response-truncated")
                                return
                            status_code = int(getattr(response, "status", 0))
                            route_id = start_urls[0][0] if start_urls else "unknown"
                            responses.append(
                                BrowserResponseObservation(
                                    provider_id=provider_id,
                                    page_route_id=route_id,
                                    response_url=response_url,
                                    observed_at_utc=self._clock(),
                                    content_type=str(content_type) if content_type else None,
                                    body_text=body,
                                    status_code=status_code,
                                    warnings=(),
                                )
                            )
                        except PermanentSourceError:
                            # Ignore third-party or disallowed response origins.
                            return
                        except Exception as exc:  # noqa: BLE001 - observation best-effort
                            _LOGGER.debug("ignored response observation error: %s", exc)

                    page.on("response", _on_response)

                    for page_route_id, url in start_urls:
                        approved = validate_provider_navigation_url(
                            url,
                            allowed_hostnames=allowed_hostnames,
                        )
                        try:
                            page.goto(
                                approved.url,
                                wait_until="domcontentloaded",
                                timeout=self._navigation_timeout_ms,
                            )
                        except PlaywrightError as exc:
                            raise RetryableSourceError(
                                f"incomplete page load for {page_route_id}: {exc}"
                            ) from exc
                        final_url = page.url
                        validate_provider_navigation_url(
                            final_url,
                            allowed_hostnames=allowed_hostnames,
                        )
                        title = page.title()
                        try:
                            body_text = page.inner_text("body")
                        except PlaywrightError:
                            body_text = None
                        detected = classify_block_signals(title=title, body_text=body_text)
                        fragment = None
                        if body_text is not None:
                            fragment = body_text[:8_192]
                        pages.append(
                            BrowserPageObservation(
                                provider_id=provider_id,
                                page_route_id=page_route_id,
                                final_url=final_url,
                                observed_at_utc=self._clock(),
                                title=title or None,
                                sanitized_dom_fragment=fragment,
                                block_reason=detected,
                                warnings=(),
                            )
                        )
                        if detected is not None:
                            block_reason = detected
                            break
                finally:
                    context.close()
                    browser.close()
        except RetryableSourceError:
            raise
        except PermanentSourceError:
            raise
        except Exception as exc:
            msg = f"browser process failure: {exc}"
            raise RetryableSourceError(msg) from exc

        return BrowserAcquisitionResult(
            provider_id=provider_id,
            sport=sport,
            acquisition_cycle_id=acquisition_cycle_id,
            observed_at_utc=observed_at_utc,
            browser_mode=browser_mode,
            pages=tuple(pages),
            responses=tuple(responses),
            diagnostics=(),
            block_reason=block_reason,
            warnings=tuple(sorted(set(warnings))),
        )


class RecordingBrowserSession:
    """Deterministic fake browser session for offline tests."""

    def __init__(self, result: BrowserAcquisitionResult) -> None:
        self._result = result
        self.calls: list[dict[str, object]] = []

    def acquire(
        self,
        *,
        provider_id: str,
        sport: str,
        acquisition_cycle_id: str,
        allowed_hostnames: frozenset[str],
        start_urls: Sequence[tuple[str, str]],
        observed_at_utc: datetime,
        browser_mode: BrowserMode,
    ) -> BrowserAcquisitionResult:
        for _, url in start_urls:
            validate_provider_navigation_url(url, allowed_hostnames=allowed_hostnames)
        self.calls.append(
            {
                "provider_id": provider_id,
                "sport": sport,
                "acquisition_cycle_id": acquisition_cycle_id,
                "browser_mode": browser_mode,
                "start_urls": list(start_urls),
            }
        )
        return self._result


def new_acquisition_cycle_id() -> str:
    """Return a deterministic-friendly cycle identifier prefix plus UUID."""
    return f"cycle-{uuid4()}"


def browser_session_context(
    session: BrowserSession,
) -> AbstractContextManager[BrowserSession]:
    """Return a trivial context manager around an injected session."""

    from contextlib import nullcontext

    return nullcontext(session)
