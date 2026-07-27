"""Playwright Chromium runtime for visible localhost bookmaker acquisition.

Production acquisition uses a locally installed Chromium browser in visible or
best-effort minimized visible mode. Headless production scraping, stealth
plugins, fingerprint rotation, CAPTCHA solving, and credential automation are
forbidden.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from urllib.parse import urlparse
from uuid import uuid4

from sports_analytics.core.exceptions import (
    PermanentSourceError,
    RetryableSourceError,
)
from sports_analytics.sources.browser.contracts import (
    BrowserAcquisitionResult,
    BrowserBlockReason,
    BrowserMode,
    BrowserNetworkMetadata,
    BrowserPageObservation,
    BrowserResponseObservation,
)
from sports_analytics.sources.browser.cookie_consent import dismiss_cookie_consent
from sports_analytics.sources.browser.errors import (
    classify_browser_crash,
    classify_missing_chromium_error,
    classify_playwright_import_error,
    raise_for_classified_error,
)
from sports_analytics.sources.browser.readiness import (
    ReadinessBlockedError,
    classify_readiness_block,
    readiness_predicate_for_provider,
    wait_for_readiness,
)
from sports_analytics.sources.browser.safety import (
    classify_block_signals,
    classify_https_public_url,
    validate_provider_navigation_url,
)

_LOGGER = logging.getLogger(__name__)

_CANDIDATE_KEY_PATTERN = re.compile(
    r"(?i)(event|events|market|markets|odd|odds|price|prices|selection|selections|"
    r"fixture|fixtures|match|matches|quote|quotes)"
)
_OBSERVATION_POLL_MS = 250


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
        deadline_at_utc: datetime | None = None,
        observation_window_ms: int | None = None,
        observation_complete: Callable[[], bool] | None = None,
    ) -> BrowserAcquisitionResult:
        """Collect page and first-party JSON observations for fixed routes."""


@dataclass(frozen=True, slots=True)
class ProviderRoute:
    """Fixed project-owned route identifier and URL."""

    page_route_id: str
    url: str


def sanitized_path_hash(url: str) -> str:
    """Return SHA-256 of the URL path only (no query/fragment/host)."""
    path = urlparse(url).path or "/"
    return hashlib.sha256(path.encode("utf-8")).hexdigest()


def top_level_keys_fingerprint(payload: dict[str, Any]) -> str:
    """Stable hash of sorted top-level JSON keys (avoids diagnostics import cycle)."""
    encoded = json.dumps(sorted(str(key) for key in payload), separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def detect_candidate_keys(payload: dict[str, Any]) -> bool:
    """True when top-level or nested keys suggest event/market/odds payloads."""
    return _scan_candidate_keys(payload, depth=0)


def _scan_candidate_keys(value: Any, *, depth: int) -> bool:
    if depth > 6:
        return False
    if isinstance(value, dict):
        for key, item in value.items():
            if _CANDIDATE_KEY_PATTERN.search(str(key)):
                return True
            if _scan_candidate_keys(item, depth=depth + 1):
                return True
    elif isinstance(value, list) and value:
        return _scan_candidate_keys(value[0], depth=depth + 1)
    return False


def build_network_metadata(
    *,
    response_url: str,
    allowed_hostnames: frozenset[str],
    status_code: int | None,
    content_type: str | None,
    resource_type: str | None,
    observed_at_utc: datetime,
    body_text: str | None = None,
    byte_size: int | None = None,
) -> BrowserNetworkMetadata | None:
    """Build metadata for one HTTPS response, or ``None`` when the URL is rejected.

    Approved hosts may capture JSON bodies. Unapproved public hosts get metadata
    only (no body). Private/loopback/non-HTTPS URLs are not recorded.
    """
    hostname_approved = False
    hostname: str | None = None
    try:
        approved = validate_provider_navigation_url(
            response_url,
            allowed_hostnames=allowed_hostnames,
        )
        hostname_approved = True
        hostname = approved.hostname
    except PermanentSourceError:
        hostname = classify_https_public_url(response_url)
        if hostname is None:
            return None

    path_hash = sanitized_path_hash(response_url)
    fingerprint: str | None = None
    candidate_keys = False
    body_captured = False
    resolved_bytes = byte_size

    if (
        hostname_approved
        and body_text is not None
        and content_type is not None
        and "json" in str(content_type).lower()
    ):
        body_captured = True
        if resolved_bytes is None:
            resolved_bytes = len(body_text.encode("utf-8"))
        try:
            loaded = json.loads(body_text)
        except json.JSONDecodeError:
            loaded = None
        if isinstance(loaded, dict):
            fingerprint = top_level_keys_fingerprint(loaded)
            candidate_keys = detect_candidate_keys(loaded)

    return BrowserNetworkMetadata(
        response_url=response_url,
        hostname=hostname,
        resource_type=resource_type,
        status_code=status_code,
        content_type=str(content_type) if content_type else None,
        byte_size=resolved_bytes,
        sanitized_path_hash=path_hash,
        structural_fingerprint=fingerprint,
        hostname_approved=hostname_approved,
        candidate_keys_detected=candidate_keys,
        body_captured=body_captured,
        observed_at_utc=observed_at_utc,
    )


class PlaywrightBrowserSession:
    """Visible Chromium session backed by Playwright when installed locally."""

    def __init__(
        self,
        *,
        navigation_timeout_ms: int = 30_000,
        maximum_response_bytes: int = 2_097_152,
        dwell_after_readiness_ms: int = 1_500,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if navigation_timeout_ms < 1:
            msg = "navigation_timeout_ms must be positive"
            raise PermanentSourceError(msg)
        if dwell_after_readiness_ms < 0:
            msg = "dwell_after_readiness_ms must be non-negative"
            raise PermanentSourceError(msg)
        self._navigation_timeout_ms = navigation_timeout_ms
        self._maximum_response_bytes = maximum_response_bytes
        self._dwell_after_readiness_ms = dwell_after_readiness_ms
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
        deadline_at_utc: datetime | None = None,
        observation_window_ms: int | None = None,
        observation_complete: Callable[[], bool] | None = None,
    ) -> BrowserAcquisitionResult:
        if browser_mode not in {BrowserMode.VISIBLE, BrowserMode.VISIBLE_MINIMIZED}:
            msg = "headless production scraping is forbidden"
            raise PermanentSourceError(msg)
        if observation_window_ms is not None and observation_window_ms < 0:
            msg = "observation_window_ms must be non-negative"
            raise PermanentSourceError(msg)
        try:
            from playwright.sync_api import Error as PlaywrightError
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise_for_classified_error(classify_playwright_import_error(exc))

        pages: list[BrowserPageObservation] = []
        responses: list[BrowserResponseObservation] = []
        network_metadata: list[BrowserNetworkMetadata] = []
        warnings: list[str] = []
        block_reason: BrowserBlockReason | None = None
        cookie_banner_dismissed = False

        current_route_id = "unknown"
        started_at = self._clock()
        deadline = (
            deadline_at_utc
            if deadline_at_utc is not None
            else started_at + timedelta(seconds=max(1, self._navigation_timeout_ms // 1000))
        )

        try:
            with sync_playwright() as playwright:
                try:
                    browser = playwright.chromium.launch(headless=False)
                except PlaywrightError as exc:
                    message = str(exc)
                    if "executable" in message.lower() or "chromium" in message.lower():
                        raise_for_classified_error(classify_missing_chromium_error(message))
                    raise_for_classified_error(classify_browser_crash(message))
                context = browser.new_context(locale="pt-PT")
                try:
                    if browser_mode is BrowserMode.VISIBLE_MINIMIZED:
                        try:
                            context.new_page()  # ensure a page exists for window state
                        except PlaywrightError:
                            warnings.append("unable to prepare minimized visible window")
                    page = context.new_page()
                    readiness_predicate = readiness_predicate_for_provider(provider_id)

                    def _on_response(response: object) -> None:
                        nonlocal current_route_id
                        try:
                            if self._clock() > deadline:
                                return
                            response_url = str(getattr(response, "url", ""))
                            headers = getattr(response, "headers", {}) or {}
                            content_type = None
                            content_length: int | None = None
                            if isinstance(headers, dict):
                                content_type = headers.get("content-type") or headers.get(
                                    "Content-Type"
                                )
                                raw_length = headers.get("content-length") or headers.get(
                                    "Content-Length"
                                )
                                if raw_length is not None:
                                    try:
                                        content_length = int(raw_length)
                                    except (TypeError, ValueError):
                                        content_length = None
                            raw_status = getattr(response, "status", None)
                            try:
                                parsed_status = int(raw_status) if raw_status is not None else None
                            except (TypeError, ValueError):
                                parsed_status = None
                            status_code = (
                                parsed_status
                                if parsed_status is not None and 100 <= parsed_status <= 599
                                else None
                            )
                            resource_type = None
                            request = getattr(response, "request", None)
                            if request is not None:
                                resource_type = getattr(request, "resource_type", None)
                                if resource_type is not None:
                                    resource_type = str(resource_type)

                            hostname_approved = False
                            try:
                                validate_provider_navigation_url(
                                    response_url,
                                    allowed_hostnames=allowed_hostnames,
                                )
                                hostname_approved = True
                            except PermanentSourceError:
                                if classify_https_public_url(response_url) is None:
                                    return

                            body_text: str | None = None
                            if (
                                hostname_approved
                                and content_type is not None
                                and "json" in str(content_type).lower()
                                and status_code is not None
                            ):
                                try:
                                    body = response.text()  # type: ignore[attr-defined]
                                except Exception as exc:  # noqa: BLE001
                                    _LOGGER.debug("ignored response body read: %s", exc)
                                    body = None
                                if body is not None:
                                    encoded_size = len(body.encode("utf-8"))
                                    if encoded_size > self._maximum_response_bytes:
                                        warnings.append("json-response-truncated")
                                    else:
                                        body_text = body
                                        route_id = current_route_id
                                        responses.append(
                                            BrowserResponseObservation(
                                                provider_id=provider_id,
                                                page_route_id=route_id,
                                                response_url=response_url,
                                                observed_at_utc=self._clock(),
                                                content_type=(
                                                    str(content_type) if content_type else None
                                                ),
                                                body_text=body,
                                                status_code=status_code,
                                                warnings=(),
                                            )
                                        )

                            meta = build_network_metadata(
                                response_url=response_url,
                                allowed_hostnames=allowed_hostnames,
                                status_code=status_code,
                                content_type=(str(content_type) if content_type else None),
                                resource_type=resource_type,
                                observed_at_utc=self._clock(),
                                body_text=body_text,
                                byte_size=(
                                    len(body_text.encode("utf-8"))
                                    if body_text is not None
                                    else content_length
                                ),
                            )
                            if meta is not None:
                                network_metadata.append(meta)
                        except PermanentSourceError:
                            return
                        except Exception as exc:  # noqa: BLE001 - observation best-effort
                            _LOGGER.debug("ignored response observation error: %s", exc)

                    page.on("response", _on_response)

                    for page_route_id, url in start_urls:
                        if self._clock() > deadline:
                            msg = (
                                "browser acquisition exceeded duration deadline "
                                f"for provider {provider_id}"
                            )
                            raise RetryableSourceError(msg)
                        current_route_id = page_route_id
                        approved = validate_provider_navigation_url(
                            url,
                            allowed_hostnames=allowed_hostnames,
                        )
                        try:
                            remaining_ms = max(
                                1,
                                int((deadline - self._clock()).total_seconds() * 1000),
                            )
                            nav_timeout = min(self._navigation_timeout_ms, remaining_ms)
                            page.goto(
                                approved.url,
                                wait_until="domcontentloaded",
                                timeout=nav_timeout,
                            )
                            # Ordinary public cookie consent before readiness wait.
                            if dismiss_cookie_consent(page, provider_id=provider_id):
                                cookie_banner_dismissed = True
                            if self._clock() > deadline:
                                msg = (
                                    "browser acquisition exceeded duration deadline "
                                    f"before readiness for provider {provider_id}"
                                )
                                raise RetryableSourceError(msg)
                            readiness_timeout_ms = max(
                                1,
                                int((deadline - self._clock()).total_seconds() * 1000),
                            )
                            wait_for_readiness(
                                page,
                                predicate=readiness_predicate,
                                page_route_id=page_route_id,
                                timeout_ms=readiness_timeout_ms,
                            )
                            self._observe_after_readiness(
                                page,
                                deadline=deadline,
                                observation_window_ms=observation_window_ms,
                                observation_complete=observation_complete,
                                network_metadata=network_metadata,
                            )
                        except ReadinessBlockedError as exc:
                            block_reason = exc.block_reason
                            pages.append(
                                BrowserPageObservation(
                                    provider_id=provider_id,
                                    page_route_id=page_route_id,
                                    final_url=page.url,
                                    observed_at_utc=self._clock(),
                                    title=page.title() or None,
                                    sanitized_dom_fragment=None,
                                    block_reason=block_reason,
                                    warnings=(),
                                )
                            )
                            break
                        except RetryableSourceError:
                            raise
                        except PlaywrightError as exc:
                            message = f"incomplete page load for {page_route_id}: {exc}"
                            raise RetryableSourceError(message) from exc
                        final_url = page.url
                        validate_provider_navigation_url(
                            final_url,
                            allowed_hostnames=allowed_hostnames,
                        )
                        title = page.title()
                        try:
                            body_html = page.inner_html("body")
                        except PlaywrightError:
                            body_html = None
                        try:
                            body_text = page.inner_text("body")
                        except PlaywrightError:
                            body_text = None
                        detected = classify_readiness_block(page) or classify_block_signals(
                            title=title,
                            body_text=body_text,
                        )
                        fragment = None
                        if body_html is not None:
                            fragment = body_html[:8_192]
                        elif body_text is not None:
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
                    # Bounded cleanup may briefly exceed the acquisition deadline.
                    context.close()
                    browser.close()
        except RetryableSourceError:
            raise
        except PermanentSourceError:
            raise
        except Exception as exc:
            raise_for_classified_error(classify_browser_crash(f"browser process failure: {exc}"))

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
            cookie_banner_dismissed=cookie_banner_dismissed,
            network_metadata=tuple(network_metadata),
        )

    def _observe_after_readiness(
        self,
        page: Any,
        *,
        deadline: datetime,
        observation_window_ms: int | None,
        observation_complete: Callable[[], bool] | None,
        network_metadata: list[BrowserNetworkMetadata],
    ) -> None:
        """Dwell or poll for network observations within the remaining deadline."""
        remaining_ms = max(
            0,
            int((deadline - self._clock()).total_seconds() * 1000),
        )
        if remaining_ms <= 0:
            return
        if observation_window_ms is None:
            dwell_ms = min(self._dwell_after_readiness_ms, remaining_ms)
            if dwell_ms > 0:
                page.wait_for_timeout(dwell_ms)
            return

        window_end = min(
            deadline,
            self._clock() + timedelta(milliseconds=observation_window_ms),
        )
        while self._clock() < window_end:
            if any(
                item.hostname_approved and item.candidate_keys_detected for item in network_metadata
            ):
                return
            if observation_complete is not None and observation_complete():
                return
            slice_ms = max(
                1,
                min(
                    _OBSERVATION_POLL_MS,
                    int((window_end - self._clock()).total_seconds() * 1000),
                ),
            )
            if self._clock() >= window_end:
                return
            page.wait_for_timeout(slice_ms)


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
        deadline_at_utc: datetime | None = None,
        observation_window_ms: int | None = None,
        observation_complete: Callable[[], bool] | None = None,
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
                "deadline_at_utc": deadline_at_utc,
                "observation_window_ms": observation_window_ms,
                "observation_complete": observation_complete,
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
