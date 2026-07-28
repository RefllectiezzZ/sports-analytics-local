"""Browser observation contracts independent of provider parsing.

Domain and snapshot layers must not import Playwright. Provider parsers consume
these typed observations only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Final

from sports_analytics.core.exceptions import PermanentSourceError
from sports_analytics.data.types import validate_identifier, validate_sha256_checksum
from sports_analytics.sports.contracts import require_utc

MAX_DIAGNOSTIC_REFERENCE_LENGTH: Final[int] = 200
MAX_PAGE_ROUTE_ID_LENGTH: Final[int] = 200
MAX_DOM_CHILD_COUNT: Final[int] = 10_000
SAFE_DOM_TAGS: Final[frozenset[str]] = frozenset(
    {
        "a",
        "article",
        "button",
        "div",
        "li",
        "main",
        "script",
        "section",
        "span",
        "table",
        "tbody",
        "td",
        "tr",
        "ul",
    }
)
SAFE_DOM_CANDIDATE_CLASSIFICATIONS: Final[frozenset[str]] = frozenset(
    {"decimal-odds", "hydration-structure", "structural-interest"}
)
DOM_STRUCTURAL_MARKERS: Final[frozenset[str]] = frozenset(
    {
        "card",
        "event",
        "fixture",
        "handicap",
        "market",
        "match",
        "odds",
        "outcome",
        "price",
        "quote",
        "selection",
    }
)
DOM_HYDRATION_MARKERS: Final[frozenset[str]] = frozenset(
    {"hydration", "ng-state", "transfer-state"}
)
_DECIMAL_ODDS: Final[re.Pattern[str]] = re.compile(
    r"^(?:1(?:[.,]\d{1,3})|[2-9]\d?(?:[.,]\d{1,3}))$"
)


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
class BrowserDomCandidate:
    """Persistable DOM candidate containing structural facts only."""

    tag: str
    structural_markers: tuple[str, ...]
    hydration_marker: str | None
    child_count: int
    candidate_classification: str
    decimal_odds_text: str | None
    structural_fingerprint: str
    ancestor_structural_fingerprint: str
    content_shape_fingerprint: str | None = None

    def __post_init__(self) -> None:
        if self.tag not in SAFE_DOM_TAGS:
            msg = "DOM candidate tag is not in the structural allowlist"
            raise PermanentSourceError(msg)
        if (
            not isinstance(self.structural_markers, tuple)
            or tuple(sorted(set(self.structural_markers))) != self.structural_markers
            or any(marker not in DOM_STRUCTURAL_MARKERS for marker in self.structural_markers)
        ):
            msg = "DOM structural markers must be sorted, unique, and allowlisted"
            raise PermanentSourceError(msg)
        if self.hydration_marker is not None and self.hydration_marker not in DOM_HYDRATION_MARKERS:
            msg = "DOM hydration marker must be allowlisted"
            raise PermanentSourceError(msg)
        if self.child_count < 0 or self.child_count > MAX_DOM_CHILD_COUNT:
            msg = "DOM candidate child_count is outside the structural bound"
            raise PermanentSourceError(msg)
        if self.candidate_classification not in SAFE_DOM_CANDIDATE_CLASSIFICATIONS:
            msg = "DOM candidate classification is not recognized"
            raise PermanentSourceError(msg)
        if self.candidate_classification == "hydration-structure" and (
            self.tag != "script"
            or self.hydration_marker is None
            or self.content_shape_fingerprint is None
            or self.structural_markers
        ):
            msg = "hydration candidate requires a reviewed script shape"
            raise PermanentSourceError(msg)
        if self.candidate_classification != "hydration-structure" and (
            self.tag == "script"
            or self.content_shape_fingerprint is not None
            or self.hydration_marker is not None
        ):
            msg = "script shape metadata is restricted to hydration candidates"
            raise PermanentSourceError(msg)
        if self.candidate_classification == "structural-interest" and not self.structural_markers:
            msg = "structural-interest candidate requires a canonical marker"
            raise PermanentSourceError(msg)
        if self.decimal_odds_text is not None and (
            self.candidate_classification != "decimal-odds"
            or _DECIMAL_ODDS.fullmatch(self.decimal_odds_text) is None
        ):
            msg = "DOM candidate decimal odds must match the reviewed grammar"
            raise PermanentSourceError(msg)
        if self.candidate_classification == "decimal-odds" and self.decimal_odds_text is None:
            msg = "decimal-odds candidate requires a numeric value"
            raise PermanentSourceError(msg)
        for field_name, value in (
            ("structural_fingerprint", self.structural_fingerprint),
            ("ancestor_structural_fingerprint", self.ancestor_structural_fingerprint),
            ("content_shape_fingerprint", self.content_shape_fingerprint),
        ):
            if value is not None:
                object.__setattr__(
                    self,
                    field_name,
                    validate_sha256_checksum(value),
                )

    def as_dict(self) -> dict[str, Any]:
        """Return the canonical structural-only JSON representation."""
        return {
            "tag": self.tag,
            "structural_markers": list(self.structural_markers),
            "hydration_marker": self.hydration_marker,
            "child_count": self.child_count,
            "candidate_classification": self.candidate_classification,
            "decimal_odds_text": self.decimal_odds_text,
            "structural_fingerprint": self.structural_fingerprint,
            "ancestor_structural_fingerprint": self.ancestor_structural_fingerprint,
            "content_shape_fingerprint": self.content_shape_fingerprint,
        }


@dataclass(frozen=True, slots=True)
class BrowserPageObservation:
    """Structural-only page observation from an allowlisted public route."""

    provider_id: str
    page_route_id: str
    hostname: str
    observed_at_utc: datetime
    block_reason: BrowserBlockReason | None
    structural_candidates: tuple[BrowserDomCandidate, ...] = ()

    def __post_init__(self) -> None:
        validate_identifier(self.provider_id, field_name="provider_id")
        if not self.page_route_id or len(self.page_route_id) > MAX_PAGE_ROUTE_ID_LENGTH:
            msg = "page_route_id must be a non-empty bounded identifier"
            raise PermanentSourceError(msg)
        if (
            not self.hostname
            or len(self.hostname) > 253
            or re.fullmatch(r"(?i)[a-z0-9.-]+", self.hostname) is None
        ):
            msg = "page hostname must be a bounded bare hostname"
            raise PermanentSourceError(msg)
        object.__setattr__(
            self,
            "observed_at_utc",
            require_utc(self.observed_at_utc, field_name="observed_at_utc"),
        )
        if (
            not isinstance(self.structural_candidates, tuple)
            or len(self.structural_candidates) > 40
            or any(not isinstance(item, BrowserDomCandidate) for item in self.structural_candidates)
        ):
            msg = "page structural candidates must be typed and bounded"
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
class BrowserNetworkMetadata:
    """Persistable sanitized network observation; complete URLs are impossible.

    ``body_captured`` means an approved JSON body was retained in the in-memory
    response observation. The gRPC-web booleans separately state whether raw
    response bytes were read and whether content-addressed evidence was stored.
    """

    hostname: str | None
    resource_type: str | None
    status_code: int | None
    content_type: str | None
    byte_size: int | None
    sanitized_path_hash: str
    structural_fingerprint: str | None
    hostname_approved: bool
    candidate_keys_detected: bool
    body_captured: bool
    observed_at_utc: datetime
    approved_route_id: str | None = None
    approved_path_template: str | None = None
    grpc_web_envelope_recognized: bool = False
    grpc_web_failure_code: str | None = None
    grpc_web_body_read: bool = False
    grpc_web_evidence_stored: bool = False
    grpc_web_malformed_or_truncated: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "sanitized_path_hash",
            validate_sha256_checksum(self.sanitized_path_hash),
        )
        if self.structural_fingerprint is not None:
            object.__setattr__(
                self,
                "structural_fingerprint",
                validate_sha256_checksum(self.structural_fingerprint),
            )
        if self.status_code is not None and (self.status_code < 100 or self.status_code > 599):
            msg = "status_code must be an HTTP status"
            raise PermanentSourceError(msg)
        if self.byte_size is not None and self.byte_size < 0:
            msg = "byte_size must be non-negative"
            raise PermanentSourceError(msg)
        object.__setattr__(
            self,
            "observed_at_utc",
            require_utc(self.observed_at_utc, field_name="observed_at_utc"),
        )
        if self.body_captured and not self.hostname_approved:
            msg = "body capture requires an approved hostname"
            raise PermanentSourceError(msg)
        if self.approved_path_template is not None:
            if (
                not self.approved_path_template.startswith("/")
                or "?" in self.approved_path_template
                or "#" in self.approved_path_template
            ):
                msg = "approved_path_template must be a static absolute path"
                raise PermanentSourceError(msg)
        if (self.approved_route_id is None) != (self.approved_path_template is None):
            msg = "approved route ID and path template must be provided together"
            raise PermanentSourceError(msg)
        if self.grpc_web_failure_code is not None:
            validate_identifier(
                self.grpc_web_failure_code,
                field_name="grpc_web_failure_code",
            )
        if self.grpc_web_envelope_recognized and not self.grpc_web_body_read:
            msg = "recognized gRPC-web envelope requires a successful body read"
            raise PermanentSourceError(msg)
        if self.grpc_web_evidence_stored and not (
            self.grpc_web_body_read and self.grpc_web_envelope_recognized
        ):
            msg = "stored gRPC-web evidence requires a read and recognized envelope"
            raise PermanentSourceError(msg)
        if self.grpc_web_malformed_or_truncated and (
            not self.grpc_web_body_read or self.grpc_web_envelope_recognized
        ):
            msg = "malformed gRPC-web metadata requires a rejected body that was read"
            raise PermanentSourceError(msg)


@dataclass(frozen=True, slots=True)
class BrowserGrpcWebDiagnostic:
    """Sanitized reference and transport-only inspection summary."""

    capture_kind: str
    checksum_sha256: str
    relative_path: str
    byte_count: int
    framing: str
    data_frame_count: int
    trailer_frame_count: int
    compression_flag_present: bool
    total_framed_payload_bytes: int
    malformed_or_truncated: bool
    grpc_status: str | None
    newly_created: bool = False

    def __post_init__(self) -> None:
        validate_identifier(self.capture_kind, field_name="capture_kind")
        object.__setattr__(
            self,
            "checksum_sha256",
            validate_sha256_checksum(self.checksum_sha256),
        )
        if not self.relative_path or self.relative_path.startswith(("/", "\\")):
            msg = "gRPC-web diagnostic path must be relative"
            raise PermanentSourceError(msg)
        if self.byte_count < 1:
            msg = "gRPC-web diagnostic byte_count must be positive"
            raise PermanentSourceError(msg)
        if self.framing not in {"binary", "text"}:
            msg = "gRPC-web diagnostic framing must be binary or text"
            raise PermanentSourceError(msg)
        if (
            min(
                self.data_frame_count,
                self.trailer_frame_count,
                self.total_framed_payload_bytes,
            )
            < 0
        ):
            msg = "gRPC-web diagnostic counts must be non-negative"
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
    network_metadata: tuple[BrowserNetworkMetadata, ...] = ()
    grpc_web_diagnostics: tuple[BrowserGrpcWebDiagnostic, ...] = ()

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
