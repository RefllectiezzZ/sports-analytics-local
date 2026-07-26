"""Provider-specific browser readiness predicates with bounded waits."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from sports_analytics.core.exceptions import RetryableSourceError
from sports_analytics.sources.browser.contracts import BrowserBlockReason
from sports_analytics.sources.browser.safety import classify_block_signals

DEFAULT_READINESS_TIMEOUT_MS: int = 15_000
DEFAULT_READINESS_POLL_MS: int = 250


class ReadinessPage(Protocol):
    """Minimal page surface required for readiness checks."""

    def title(self) -> str: ...

    def inner_text(self, selector: str) -> str: ...

    def wait_for_timeout(self, timeout: float) -> None: ...


ReadinessPredicate = Callable[[ReadinessPage], bool]


def wait_for_readiness(
    page: ReadinessPage,
    *,
    predicate: ReadinessPredicate,
    timeout_ms: int = DEFAULT_READINESS_TIMEOUT_MS,
    poll_ms: int = DEFAULT_READINESS_POLL_MS,
    page_route_id: str,
) -> None:
    """Wait until ``predicate`` returns true or raise a retryable timeout."""
    if timeout_ms < 1 or poll_ms < 1:
        msg = "readiness timeout and poll intervals must be positive"
        raise RetryableSourceError(msg)
    elapsed = 0
    while elapsed < timeout_ms:
        if predicate(page):
            return
        page.wait_for_timeout(poll_ms)
        elapsed += poll_ms
    msg = f"readiness timeout for route {page_route_id} after {timeout_ms}ms"
    raise RetryableSourceError(msg)


def betano_readiness_predicate(page: ReadinessPage) -> bool:
    """Betano: page title must not be the Cloudflare splash and body must exist."""
    title = (page.title() or "").strip().lower()
    if "splash screen" in title:
        return False
    try:
        body = page.inner_text("body")
    except Exception:  # noqa: BLE001
        return False
    if not body or not body.strip():
        return False
    blocked = classify_block_signals(title=page.title(), body_text=body)
    return blocked is None


def betclic_readiness_predicate(page: ReadinessPage) -> bool:
    """Betclic: reject obvious 403/forbidden pages and require non-empty body."""
    title = (page.title() or "").strip().lower()
    if "403" in title or "forbidden" in title:
        return False
    try:
        body = page.inner_text("body")
    except Exception:  # noqa: BLE001
        return False
    if not body or not body.strip():
        return False
    blocked = classify_block_signals(title=page.title(), body_text=body)
    return blocked is None


def readiness_predicate_for_provider(provider_id: str) -> ReadinessPredicate:
    """Return the project-owned readiness predicate for ``provider_id``."""
    if provider_id == "betano-pt":
        return betano_readiness_predicate
    if provider_id == "betclic-pt":
        return betclic_readiness_predicate
    msg = f"no readiness predicate for provider {provider_id}"
    raise RetryableSourceError(msg)


def classify_readiness_block(page: Any) -> BrowserBlockReason | None:
    """Classify block signals after readiness wait completes."""
    try:
        title = page.title()
        body = page.inner_text("body")
    except Exception:  # noqa: BLE001
        return BrowserBlockReason.PAGE_UNAVAILABLE
    return classify_block_signals(title=title, body_text=body)
