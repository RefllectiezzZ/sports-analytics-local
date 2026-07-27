"""Browser observation contracts independent of provider parsing.

Domain and snapshot layers must not import Playwright. Provider parsers consume
these typed observations only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final

from sports_analytics.core.exceptions import PermanentSourceError
from sports_analytics.data.types import validate_identifier, validate_sha256_checksum
from sports_analytics.sports.contracts import require_utc

MAX_DIAGNOSTIC_REFERENCE_LENGTH: Final[int] = 200
MAX_PAGE_ROUTE_ID_LENGTH: Final[int] = 200


class BrowserBlockReason(StrEnum):
    """Explicit classification when a provider blocks ordinary browser access."""

    CAPTCHA = "captcha"
    ACCESS_DENIED = "access-denied"
    AUTHENTICATION_REQUIRED = "authentication-required"
    REGIONAL_REFUSAL = "regional-refusal"
    ANTI_AUTOMATION = "anti-automation"
    UNSUPPORTED_ORIGIN = "unsupported-origin"
    NAVIGATION_REJECTED = "navigation-rejected"
    PAGE_UNAVAILABLE = "page-unavailable"


class BrowserMode(StrEnum):
    """Visible browser launch modes. Headless production scraping is forbidden."""

    VISIBLE = "visible"
    VISIBLE_MINIMIZED = "visible-minimized"


@dataclass(frozen=True, slots=True)
class BrowserDiagnosticReference:
    """Content-addressed reference to a minimized diagnostic capture."""

    capture_kind: str
    checksum_sha256: str
    relative_path: str
    byte_count: int

    def __post_init__(self) -> None:
        validate_identifier(self.capture_kind, field_name="capture_kind")
        object.__setattr__(
            self,
            "checksum_sha256",
            validate_sha256_checksum(self.checksum_sha256),
        )
        if not self.relative_path or self.relative_path.startswith(("/", "\\")):
            msg = "diagnostic relative_path must be a non-absolute relative path"
            raise PermanentSourceError(msg)
        if self.byte_count < 0:
            msg = "diagnostic byte_count must be non-negative"
            raise PermanentSourceError(msg)


@dataclass(frozen=True, slots=True)
class BrowserPageObservation:
    """Sanitized DOM/page observation from an allowlisted public route."""

    provider_id: str
    page_route_id: str
    final_url: str
    observed_at_utc: datetime
    title: str | None
    sanitized_dom_fragment: str | None
    block_reason: BrowserBlockReason | None
    warnings: tuple[str, ...]

    def __post_init__(self) -> None:
        validate_identifier(self.provider_id, field_name="provider_id")
        if not self.page_route_id or len(self.page_route_id) > MAX_PAGE_ROUTE_ID_LENGTH:
            msg = "page_route_id must be a non-empty bounded identifier"
            raise PermanentSourceError(msg)
        if not self.final_url.startswith("https://"):
            msg = "final_url must be HTTPS"
            raise PermanentSourceError(msg)
        object.__setattr__(
            self,
            "observed_at_utc",
            require_utc(self.observed_at_utc, field_name="observed_at_utc"),
        )
        if tuple(sorted(self.warnings)) != self.warnings:
            msg = "warnings must be sorted"
            raise PermanentSourceError(msg)


@dataclass(frozen=True, slots=True)
class BrowserResponseObservation:
    """First-party JSON response observed while loading an allowlisted page."""

    provider_id: str
    page_route_id: str
    response_url: str
    observed_at_utc: datetime
    content_type: str | None
    body_text: str
    status_code: int
    warnings: tuple[str, ...]

    def __post_init__(self) -> None:
        validate_identifier(self.provider_id, field_name="provider_id")
        if not self.response_url.startswith("https://"):
            msg = "response_url must be HTTPS"
            raise PermanentSourceError(msg)
        if self.status_code < 100 or self.status_code > 599:
            msg = "status_code must be an HTTP status"
            raise PermanentSourceError(msg)
        object.__setattr__(
            self,
            "observed_at_utc",
            require_utc(self.observed_at_utc, field_name="observed_at_utc"),
        )
        if tuple(sorted(self.warnings)) != self.warnings:
            msg = "warnings must be sorted"
            raise PermanentSourceError(msg)


@dataclass(frozen=True, slots=True)
class BrowserAcquisitionResult:
    """Complete disposable-browser acquisition outcome for one provider cycle."""

    provider_id: str
    sport: str
    acquisition_cycle_id: str
    observed_at_utc: datetime
    browser_mode: BrowserMode
    pages: tuple[BrowserPageObservation, ...]
    responses: tuple[BrowserResponseObservation, ...]
    diagnostics: tuple[BrowserDiagnosticReference, ...]
    block_reason: BrowserBlockReason | None
    warnings: tuple[str, ...]
    cookie_banner_dismissed: bool = False

    def __post_init__(self) -> None:
        validate_identifier(self.provider_id, field_name="provider_id")
        validate_identifier(self.sport, field_name="sport")
        validate_identifier(self.acquisition_cycle_id, field_name="acquisition_cycle_id")
        object.__setattr__(
            self,
            "observed_at_utc",
            require_utc(self.observed_at_utc, field_name="observed_at_utc"),
        )
        if tuple(sorted(self.warnings)) != self.warnings:
            msg = "warnings must be sorted"
            raise PermanentSourceError(msg)
