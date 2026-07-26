"""Browser acquisition error classification for bookmaker adapters."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from sports_analytics.core.exceptions import (
    PermanentSourceError,
    RetryableSourceError,
)
from sports_analytics.sources.browser.contracts import BrowserBlockReason


class BrowserErrorKind(StrEnum):
    """How a browser-layer failure should be treated."""

    PERMANENT_CONFIGURATION = "permanent-configuration"
    PERMANENT_LOCAL_CONFIGURATION = "permanent-local-configuration"
    RETRYABLE_TRANSPORT = "retryable-transport"
    BLOCKED = "blocked"
    SCHEMA_CONTRADICTION = "schema-contradiction"


@dataclass(frozen=True, slots=True)
class ClassifiedBrowserError:
    """Typed browser failure with retry and admission semantics."""

    kind: BrowserErrorKind
    message: str
    block_reason: BrowserBlockReason | None = None
    detail_code: str | None = None


def classify_playwright_import_error(exc: ImportError) -> ClassifiedBrowserError:
    """Missing Playwright Python package is a permanent configuration error."""
    return ClassifiedBrowserError(
        kind=BrowserErrorKind.PERMANENT_CONFIGURATION,
        message=(
            "Playwright is not installed. Install the package and run "
            "`python -m playwright install chromium` locally."
        ),
        detail_code="playwright-package-missing",
    )


def classify_missing_chromium_error(message: str) -> ClassifiedBrowserError:
    """Missing Chromium executable is a permanent local configuration error."""
    return ClassifiedBrowserError(
        kind=BrowserErrorKind.PERMANENT_LOCAL_CONFIGURATION,
        message=message,
        detail_code="chromium-executable-missing",
    )


def classify_navigation_timeout(message: str) -> ClassifiedBrowserError:
    """Temporary navigation timeout is retryable."""
    return ClassifiedBrowserError(
        kind=BrowserErrorKind.RETRYABLE_TRANSPORT,
        message=message,
        detail_code="navigation-timeout",
    )


def classify_browser_crash(message: str) -> ClassifiedBrowserError:
    """Browser process crash after valid setup is retryable."""
    return ClassifiedBrowserError(
        kind=BrowserErrorKind.RETRYABLE_TRANSPORT,
        message=message,
        detail_code="browser-process-crash",
    )


def classify_blocked(block_reason: BrowserBlockReason) -> ClassifiedBrowserError:
    """CAPTCHA, access denied, or regional refusal is blocked."""
    return ClassifiedBrowserError(
        kind=BrowserErrorKind.BLOCKED,
        message=f"provider blocked: {block_reason.value}",
        block_reason=block_reason,
        detail_code=block_reason.value,
    )


def classify_schema_contradiction(message: str) -> ClassifiedBrowserError:
    """Schema contradiction is permanent for the current adapter version."""
    return ClassifiedBrowserError(
        kind=BrowserErrorKind.SCHEMA_CONTRADICTION,
        message=message,
        detail_code="schema-contradiction",
    )


def raise_for_classified_error(error: ClassifiedBrowserError) -> None:
    """Raise the appropriate source exception for a classified browser error."""
    if error.kind in {
        BrowserErrorKind.PERMANENT_CONFIGURATION,
        BrowserErrorKind.PERMANENT_LOCAL_CONFIGURATION,
        BrowserErrorKind.SCHEMA_CONTRADICTION,
    }:
        raise PermanentSourceError(error.message)
    if error.kind is BrowserErrorKind.RETRYABLE_TRANSPORT:
        raise RetryableSourceError(error.message)
    if error.kind is BrowserErrorKind.BLOCKED:
        raise PermanentSourceError(error.message)
