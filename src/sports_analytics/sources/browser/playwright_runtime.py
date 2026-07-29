"""Playwright Chromium runtime for ordinary localhost bookmaker acquisition.

Production acquisition uses a fresh locally installed Chromium context in
headless, visible, or best-effort minimized visible mode. Stealth plugins,
fingerprint rotation, CAPTCHA solving, and credential automation are forbidden.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import re
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse, urlsplit
from uuid import uuid4

from sports_analytics.core.exceptions import (
    PermanentSourceError,
    RetryableSourceError,
)
from sports_analytics.sources.browser.contracts import (
    BrowserAcquisitionResult,
    BrowserBlockReason,
    BrowserBodyCaptureState,
    BrowserDiagnosticReference,
    BrowserDomCandidate,
    BrowserGrpcWebDiagnostic,
    BrowserMode,
    BrowserNetworkMetadata,
    BrowserPageObservation,
    BrowserRedirectClassification,
    BrowserResponseObservation,
    BrowserTransportType,
)
from sports_analytics.sources.browser.cookie_consent import dismiss_cookie_consent
from sports_analytics.sources.browser.errors import (
    classify_browser_crash,
    classify_missing_chromium_error,
    classify_playwright_import_error,
    raise_for_classified_error,
)
from sports_analytics.sources.browser.limits import BrowserAcquisitionLimits
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
_MAX_EPHEMERAL_BODY_HTML_CHARS = 65_536
_MAX_EPHEMERAL_BODY_TEXT_CHARS = 16_384
_MAX_EPHEMERAL_TITLE_CHARS = 512
_BODY_ELIGIBLE_RESOURCE_TYPES = frozenset({"fetch", "xhr"})


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
        diagnostic_directory: Path | None = None,
    ) -> BrowserAcquisitionResult:
        """Collect browser-received, bounded observations for fixed routes."""


@dataclass(frozen=True, slots=True)
class ProviderRoute:
    """Fixed project-owned route identifier and URL."""

    page_route_id: str
    url: str


def build_structural_page_observation(
    *,
    provider_id: str,
    page_route_id: str,
    final_url: str,
    observed_at_utc: datetime,
    allowed_hostnames: frozenset[str],
    title: str | None,
    body_html: str | None,
    body_text: str | None,
    block_reason: BrowserBlockReason | None = None,
) -> BrowserPageObservation:
    """Convert ephemeral page material into a structural-only observation."""
    approved = validate_provider_navigation_url(
        final_url,
        allowed_hostnames=allowed_hostnames,
    )
    detected = block_reason or classify_block_signals(
        title=None if title is None else title[:_MAX_EPHEMERAL_TITLE_CHARS],
        body_text=(None if body_text is None else body_text[:_MAX_EPHEMERAL_BODY_TEXT_CHARS]),
    )
    candidates: tuple[BrowserDomCandidate, ...]
    if body_html is None:
        candidates = ()
    else:
        from sports_analytics.bookmakers.diagnostics.dom_discovery import (
            discover_dom_candidates,
        )

        candidates = discover_dom_candidates(body_html[:_MAX_EPHEMERAL_BODY_HTML_CHARS])
    return BrowserPageObservation(
        provider_id=provider_id,
        page_route_id=page_route_id,
        hostname=approved.hostname,
        observed_at_utc=observed_at_utc,
        block_reason=detected,
        structural_candidates=candidates,
    )


def sanitized_path_hash(url: str) -> str:
    """Return SHA-256 of the URL path only (no query/fragment/host)."""
    path = urlparse(url).path or "/"
    return hashlib.sha256(path.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class SafeWebSocketEndpoint:
    """Sanitized metadata for an approved public WSS connection."""

    hostname: str
    sanitized_path_hash: str


def classify_safe_websocket_url(
    url: str,
    *,
    allowed_hostnames: frozenset[str],
) -> SafeWebSocketEndpoint:
    """Validate one WSS URL without retaining its query, fragment, or full text."""
    try:
        split = urlsplit(url)
        # Accessing port performs urllib's strict numeric/range validation.
        port = split.port
    except ValueError as exc:
        msg = "WebSocket URL is malformed"
        raise PermanentSourceError(msg) from exc
    if split.scheme != "wss":
        msg = "WebSocket metadata requires wss"
        raise PermanentSourceError(msg)
    if port not in {None, 443}:
        msg = "WebSocket URL port is not approved"
        raise PermanentSourceError(msg)
    if split.username is not None or split.password is not None:
        msg = "WebSocket URL credentials are forbidden"
        raise PermanentSourceError(msg)
    hostname = split.hostname
    if hostname is None:
        msg = "WebSocket URL hostname is required"
        raise PermanentSourceError(msg)
    try:
        hostname.encode("ascii", errors="strict")
    except UnicodeEncodeError as exc:
        msg = "WebSocket URL hostname must be ASCII"
        raise PermanentSourceError(msg) from exc
    normalized = hostname.casefold().rstrip(".")
    approved_hosts = {item.casefold().rstrip(".") for item in allowed_hostnames}
    if normalized not in approved_hosts:
        msg = "WebSocket URL hostname is not approved"
        raise PermanentSourceError(msg)
    if normalized in {"localhost"} or normalized.endswith(".localhost"):
        msg = "WebSocket URL localhost targets are forbidden"
        raise PermanentSourceError(msg)
    try:
        address = ipaddress.ip_address(normalized.strip("[]"))
    except ValueError:
        address = None
    if address is not None and (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_unspecified
    ):
        msg = "WebSocket URL private targets are forbidden"
        raise PermanentSourceError(msg)
    path = split.path or "/"
    return SafeWebSocketEndpoint(
        hostname=normalized,
        sanitized_path_hash=hashlib.sha256(path.encode("utf-8")).hexdigest(),
    )


def build_websocket_metadata(
    *,
    socket_url: str,
    allowed_hostnames: frozenset[str],
    observed_at_utc: datetime,
    provider_id: str,
    sport: str,
    acquisition_cycle_id: str,
    page_route_id: str,
) -> BrowserNetworkMetadata:
    """Build metadata-only evidence for an approved WSS connection."""
    safe = classify_safe_websocket_url(
        socket_url,
        allowed_hostnames=allowed_hostnames,
    )
    return BrowserNetworkMetadata(
        hostname=safe.hostname,
        resource_type="websocket",
        status_code=None,
        content_type=None,
        byte_size=None,
        sanitized_path_hash=safe.sanitized_path_hash,
        structural_fingerprint=None,
        hostname_approved=True,
        candidate_keys_detected=False,
        body_captured=False,
        observed_at_utc=observed_at_utc,
        provider_id=provider_id,
        sport=sport,
        acquisition_cycle_id=acquisition_cycle_id,
        page_route_id=page_route_id,
        request_method="GET",
        transport_type=BrowserTransportType.WEBSOCKET,
        body_capture_state=BrowserBodyCaptureState.METADATA_ONLY,
    )


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


def structural_candidate_counts(payload: object) -> tuple[int, int, int]:
    """Count structural key occurrences without retaining provider values."""
    counts = {"event": 0, "market": 0, "selection": 0}

    def _walk(value: object, *, depth: int) -> None:
        if depth > 6 or sum(counts.values()) >= 1_000_000:
            return
        if isinstance(value, dict):
            for key, item in value.items():
                normalized = str(key).casefold()
                for kind in counts:
                    if kind in normalized:
                        counts[kind] += 1
                _walk(item, depth=depth + 1)
        elif isinstance(value, list):
            for item in value[:100]:
                _walk(item, depth=depth + 1)

    _walk(payload, depth=0)
    return counts["event"], counts["market"], counts["selection"]


def classify_browser_transport(
    *,
    resource_type: str | None,
    content_type: str | None,
) -> BrowserTransportType:
    """Classify safe transport metadata without inspecting URL keywords."""
    resource = (resource_type or "").strip().casefold()
    media_type = (content_type or "").split(";", 1)[0].strip().casefold()
    if "grpc-web" in media_type:
        return BrowserTransportType.GRPC_WEB
    if resource == "document":
        return BrowserTransportType.DOCUMENT
    if resource == "fetch":
        return BrowserTransportType.FETCH
    if resource == "xhr":
        return BrowserTransportType.XHR
    if resource == "websocket":
        return BrowserTransportType.WEBSOCKET
    if resource == "eventsource":
        return BrowserTransportType.EVENTSOURCE
    if resource == "script" or "javascript" in media_type:
        return BrowserTransportType.SCRIPT_CONFIGURATION
    return BrowserTransportType.OTHER_APPROVED


def approved_json_payload_for_profile(
    *,
    provider_id: str,
    resource_type: str | None,
    payload: object,
) -> bool:
    """Gate retained JSON bodies by reviewed structure, not URL wording."""
    if (resource_type or "").casefold() not in _BODY_ELIGIBLE_RESOURCE_TYPES:
        return False
    if provider_id == "betano-pt":
        from sports_analytics.sources.bookmaker_extraction.betano_topeventsv2 import (
            looks_like_topeventsv2,
        )

        return isinstance(payload, dict) and looks_like_topeventsv2(payload)
    # Betclic has no verified JSON/protobuf extraction profile yet.
    return False


def is_configuration_json(payload: object) -> bool:
    """Recognize configuration shape without treating it as odds evidence."""
    if not isinstance(payload, dict):
        return False
    top_level = {str(key).casefold() for key in payload}
    return bool(
        top_level
        & {
            "appconfig",
            "config",
            "configuration",
            "environment",
            "release",
            "settings",
            "tactics",
        }
    )


def build_network_metadata(
    *,
    response_url: str,
    allowed_hostnames: frozenset[str],
    status_code: int | None,
    content_type: str | None,
    resource_type: str | None,
    observed_at_utc: datetime,
    body_text: str | None = None,
    structural_body_text: str | None = None,
    byte_size: int | None = None,
    provider_id: str | None = None,
    sport: str | None = None,
    acquisition_cycle_id: str | None = None,
    page_route_id: str | None = None,
    source_event_id: str | None = None,
    request_method: str | None = None,
    transport_type: BrowserTransportType | None = None,
    declared_content_length: int | None = None,
    actual_captured_byte_length: int | None = None,
    redirect_classification: BrowserRedirectClassification = BrowserRedirectClassification.NONE,
    body_capture_state: BrowserBodyCaptureState | None = None,
    grpc_web_envelope_recognized: bool = False,
    grpc_web_failure_code: str | None = None,
    grpc_web_body_read: bool = False,
    grpc_web_evidence_stored: bool = False,
    grpc_web_malformed_or_truncated: bool = False,
) -> BrowserNetworkMetadata | None:
    """Build metadata for one HTTPS response, or ``None`` when the URL is rejected.

    Approved hosts may capture JSON bodies. Unapproved public hosts get metadata
    only (no body). Private/loopback/non-HTTPS URLs are not recorded.
    """
    hostname_approved = False
    hostname: str | None = None
    approved_route_id: str | None = None
    approved_path_template: str | None = None
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
        if provider_id == "betclic-pt":
            from sports_analytics.sources.betclic.discovery import approve_betclic_response_url

            try:
                approved_response = approve_betclic_response_url(response_url)
            except PermanentSourceError:
                pass
            else:
                hostname_approved = True
                approved_route_id = approved_response.route_id
                approved_path_template = approved_response.path_template

    path_hash = sanitized_path_hash(response_url)
    fingerprint: str | None = None
    candidate_keys = False
    body_captured = False
    resolved_bytes = byte_size
    event_candidates = 0
    market_candidates = 0
    selection_candidates = 0

    inspected_text = structural_body_text if structural_body_text is not None else body_text
    if (
        hostname_approved
        and inspected_text is not None
        and content_type is not None
        and "json" in str(content_type).lower()
    ):
        body_captured = body_text is not None
        if resolved_bytes is None:
            resolved_bytes = len(inspected_text.encode("utf-8"))
        try:
            loaded = json.loads(inspected_text)
        except json.JSONDecodeError:
            loaded = None
        if isinstance(loaded, dict):
            fingerprint = top_level_keys_fingerprint(loaded)
            candidate_keys = detect_candidate_keys(loaded)
            (
                event_candidates,
                market_candidates,
                selection_candidates,
            ) = structural_candidate_counts(loaded)
    resolved_capture_state = body_capture_state
    if resolved_capture_state is None:
        resolved_capture_state = (
            BrowserBodyCaptureState.CAPTURED
            if body_captured
            else BrowserBodyCaptureState.NOT_APPROVED
        )
    checksum = (
        hashlib.sha256(body_text.encode("utf-8")).hexdigest()
        if body_text is not None and body_captured
        else None
    )
    captured_length = actual_captured_byte_length
    if body_text is not None and body_captured and captured_length is None:
        captured_length = len(body_text.encode("utf-8"))

    return BrowserNetworkMetadata(
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
        approved_route_id=approved_route_id,
        approved_path_template=approved_path_template,
        grpc_web_envelope_recognized=grpc_web_envelope_recognized,
        grpc_web_failure_code=grpc_web_failure_code,
        grpc_web_body_read=grpc_web_body_read,
        grpc_web_evidence_stored=grpc_web_evidence_stored,
        grpc_web_malformed_or_truncated=grpc_web_malformed_or_truncated,
        provider_id=provider_id,
        sport=sport,
        acquisition_cycle_id=acquisition_cycle_id,
        page_route_id=page_route_id,
        source_event_id=source_event_id,
        request_method=request_method,
        transport_type=transport_type
        or classify_browser_transport(
            resource_type=resource_type,
            content_type=content_type,
        ),
        declared_content_length=declared_content_length,
        actual_captured_byte_length=captured_length,
        redirect_classification=redirect_classification,
        body_capture_state=resolved_capture_state,
        contributing_capture_checksum=checksum,
        event_candidate_count=event_candidates,
        market_candidate_count=market_candidates,
        selection_candidate_count=selection_candidates,
    )


@dataclass(frozen=True, slots=True)
class BetclicGrpcObservationOutcome:
    """Safe result with explicit body-read and evidence-retention semantics."""

    approved_route_id: str | None
    envelope_recognized: bool
    actual_byte_size: int | None
    failure_code: str | None
    diagnostic: BrowserGrpcWebDiagnostic | None
    body_read: bool
    evidence_stored: bool
    malformed_or_truncated: bool


def observe_betclic_grpc_response(
    *,
    response: object,
    response_url: str,
    content_type: str | None,
    content_length: int | None,
    maximum_bytes: int,
    diagnostic_directory: Path | None,
) -> BetclicGrpcObservationOutcome:
    """Inspect a naturally observed Betclic response without issuing a request.

    Playwright may require one complete ``response.body()`` allocation when the
    server omits Content-Length. The real byte length is checked immediately
    after that read and again by inspection/storage before anything is retained.
    """
    from sports_analytics.sources.betclic.discovery import approve_betclic_response_url
    from sports_analytics.sources.betclic.grpc_web import (
        GrpcWebEnvelopeError,
        inspect_grpc_web_envelope,
        is_recognized_grpc_web_content_type,
        store_content_addressed_grpc_evidence,
    )

    try:
        approved = approve_betclic_response_url(response_url)
    except PermanentSourceError:
        return BetclicGrpcObservationOutcome(None, False, None, None, None, False, False, False)
    if content_type is None or not is_recognized_grpc_web_content_type(content_type):
        return BetclicGrpcObservationOutcome(
            approved.route_id,
            False,
            None,
            None,
            None,
            False,
            False,
            False,
        )
    if approved.metadata_only:
        return BetclicGrpcObservationOutcome(
            approved.route_id,
            False,
            None,
            None,
            None,
            False,
            False,
            False,
        )
    if content_length is not None and content_length > maximum_bytes:
        return BetclicGrpcObservationOutcome(
            approved.route_id,
            False,
            content_length,
            "grpc-web-size-header-rejected",
            None,
            False,
            False,
            False,
        )

    try:
        raw_body = response.body()  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - Playwright/local failure is metadata-only
        return BetclicGrpcObservationOutcome(
            approved.route_id,
            False,
            None,
            "grpc-web-body-read-failed",
            None,
            False,
            False,
            False,
        )
    actual_size = len(raw_body)
    if actual_size > maximum_bytes:
        return BetclicGrpcObservationOutcome(
            approved.route_id,
            False,
            actual_size,
            "body-size-exceeded",
            None,
            True,
            False,
            False,
        )

    try:
        inspection = inspect_grpc_web_envelope(
            raw_body,
            content_type=content_type,
            maximum_bytes=maximum_bytes,
        )
    except GrpcWebEnvelopeError as exc:
        return BetclicGrpcObservationOutcome(
            approved.route_id,
            False,
            actual_size,
            exc.classification,
            None,
            True,
            False,
            exc.malformed_or_truncated,
        )
    except PermanentSourceError:
        return BetclicGrpcObservationOutcome(
            approved.route_id,
            False,
            actual_size,
            "envelope-inspection-failed",
            None,
            True,
            False,
            False,
        )
    if diagnostic_directory is None:
        return BetclicGrpcObservationOutcome(
            approved.route_id,
            True,
            actual_size,
            None,
            None,
            True,
            False,
            False,
        )
    stored = None
    try:
        stored = store_content_addressed_grpc_evidence(
            raw_body,
            directory=diagnostic_directory / "grpc-web",
        )
        relative_path = stored.absolute_path.relative_to(diagnostic_directory).as_posix()
        diagnostic = BrowserGrpcWebDiagnostic(
            capture_kind="betclic-grpc-web",
            checksum_sha256=stored.checksum_sha256,
            relative_path=relative_path,
            byte_count=stored.byte_count,
            framing=inspection.framing,
            data_frame_count=inspection.data_frame_count,
            trailer_frame_count=inspection.trailer_frame_count,
            compression_flag_present=inspection.compression_flag_present,
            total_framed_payload_bytes=inspection.total_framed_payload_bytes,
            malformed_or_truncated=inspection.malformed_or_truncated,
            grpc_status=inspection.grpc_status,
            newly_created=stored.newly_created,
        )
    except Exception:  # noqa: BLE001 - local evidence failure must preserve metadata
        if stored is not None and stored.newly_created:
            try:
                stored.absolute_path.unlink(missing_ok=True)
            except OSError:
                pass
        return BetclicGrpcObservationOutcome(
            approved.route_id,
            True,
            actual_size,
            "grpc-web-evidence-storage-failed",
            None,
            True,
            False,
            False,
        )
    return BetclicGrpcObservationOutcome(
        approved.route_id,
        True,
        actual_size,
        None,
        diagnostic,
        True,
        True,
        False,
    )


class PlaywrightBrowserSession:
    """Disposable Chromium session backed by Playwright when installed locally."""

    def __init__(
        self,
        *,
        navigation_timeout_ms: int = 30_000,
        maximum_response_bytes: int = 2_097_152,
        maximum_total_capture_bytes: int = 16_777_216,
        event_detail_concurrency: int = 1,
        minimum_event_detail_interval_ms: int = 1_000,
        explicit_retry_limit: int = 0,
        dwell_after_readiness_ms: int = 1_500,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if navigation_timeout_ms < 1:
            msg = "navigation_timeout_ms must be positive"
            raise PermanentSourceError(msg)
        if dwell_after_readiness_ms < 0:
            msg = "dwell_after_readiness_ms must be non-negative"
            raise PermanentSourceError(msg)
        if maximum_response_bytes < 1:
            msg = "maximum_response_bytes must be positive"
            raise PermanentSourceError(msg)
        if maximum_total_capture_bytes < maximum_response_bytes:
            msg = "maximum_total_capture_bytes must cover at least one response"
            raise PermanentSourceError(msg)
        self._navigation_timeout_ms = navigation_timeout_ms
        self._maximum_response_bytes = maximum_response_bytes
        self._maximum_total_capture_bytes = maximum_total_capture_bytes
        self._limits = BrowserAcquisitionLimits(
            maximum_response_bytes=maximum_response_bytes,
            maximum_total_capture_bytes=maximum_total_capture_bytes,
            event_detail_concurrency=event_detail_concurrency,
            minimum_event_detail_interval_ms=minimum_event_detail_interval_ms,
            navigation_timeout_ms=navigation_timeout_ms,
            explicit_retry_limit=explicit_retry_limit,
        )
        self._dwell_after_readiness_ms = dwell_after_readiness_ms
        self._clock = clock or (lambda: datetime.now(UTC))

    @property
    def limits(self) -> BrowserAcquisitionLimits:
        """Return the immutable deterministic browser/load policy."""
        return self._limits

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
        diagnostic_directory: Path | None = None,
    ) -> BrowserAcquisitionResult:
        if browser_mode not in {
            BrowserMode.HEADLESS,
            BrowserMode.VISIBLE,
            BrowserMode.VISIBLE_MINIMIZED,
        }:
            msg = "unsupported browser presentation mode"
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
        diagnostics: list[BrowserDiagnosticReference] = []
        grpc_web_diagnostics: list[BrowserGrpcWebDiagnostic] = []
        warnings: list[str] = []
        block_reason: BrowserBlockReason | None = None
        cookie_banner_dismissed = False
        captured_total_bytes = 0

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
                    browser = playwright.chromium.launch(
                        headless=browser_mode is BrowserMode.HEADLESS
                    )
                except PlaywrightError as exc:
                    message = str(exc)
                    if "executable" in message.lower() or "chromium" in message.lower():
                        raise_for_classified_error(classify_missing_chromium_error(message))
                    raise_for_classified_error(classify_browser_crash(message))
                context = browser.new_context(locale="pt-PT", timezone_id="Europe/Lisbon")
                try:
                    if browser_mode is BrowserMode.VISIBLE_MINIMIZED:
                        try:
                            context.new_page()  # ensure a page exists for window state
                        except PlaywrightError:
                            warnings.append("unable to prepare minimized visible window")
                    page = context.new_page()
                    readiness_predicate = readiness_predicate_for_provider(provider_id)

                    def _on_response(response: object) -> None:
                        nonlocal captured_total_bytes, current_route_id
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
                                    if content_length is not None and content_length < 0:
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
                            request_method = None
                            redirect_classification = BrowserRedirectClassification.NONE
                            request = getattr(response, "request", None)
                            if request is not None:
                                resource_type = getattr(request, "resource_type", None)
                                if resource_type is not None:
                                    resource_type = str(resource_type)
                                method = getattr(request, "method", None)
                                if method is not None:
                                    request_method = str(method).upper()
                                if getattr(request, "redirected_from", None) is not None:
                                    redirect_classification = (
                                        BrowserRedirectClassification.REDIRECTED_REQUEST
                                    )
                            request_method = request_method or "GET"
                            transport_type = classify_browser_transport(
                                resource_type=resource_type,
                                content_type=(
                                    str(content_type) if content_type is not None else None
                                ),
                            )

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
                            inspected_body_text: str | None = None
                            body_capture_state = BrowserBodyCaptureState.UNSUPPORTED_CONTENT
                            actual_body_size: int | None = None
                            grpc_web_envelope_recognized = False
                            grpc_web_failure_code: str | None = None
                            grpc_web_body_read = False
                            grpc_web_evidence_stored = False
                            grpc_web_malformed_or_truncated = False
                            grpc_actual_byte_size: int | None = None
                            if (
                                hostname_approved
                                and content_type is not None
                                and "json" in str(content_type).lower()
                                and status_code is not None
                            ):
                                if content_length is not None and (
                                    content_length > self._maximum_response_bytes
                                ):
                                    body_capture_state = (
                                        BrowserBodyCaptureState.DECLARED_SIZE_REJECTED
                                    )
                                    warnings.append("json-response-truncated")
                                elif captured_total_bytes >= self._maximum_total_capture_bytes:
                                    body_capture_state = (
                                        BrowserBodyCaptureState.TOTAL_BUDGET_REJECTED
                                    )
                                    warnings.append("total-capture-budget-exhausted")
                                else:
                                    try:
                                        body = response.text()  # type: ignore[attr-defined]
                                    except Exception as exc:  # noqa: BLE001
                                        _LOGGER.debug(
                                            "ignored response body read: %s",
                                            type(exc).__name__,
                                        )
                                        body = None
                                        body_capture_state = BrowserBodyCaptureState.READ_FAILED
                                    if body is not None:
                                        inspected_body_text = body
                                        encoded_size = len(body.encode("utf-8"))
                                        actual_body_size = encoded_size
                                        if encoded_size > self._maximum_response_bytes:
                                            body_capture_state = (
                                                BrowserBodyCaptureState.ACTUAL_SIZE_REJECTED
                                            )
                                            warnings.append("json-response-truncated")
                                        elif (
                                            captured_total_bytes + encoded_size
                                            > self._maximum_total_capture_bytes
                                        ):
                                            body_capture_state = (
                                                BrowserBodyCaptureState.TOTAL_BUDGET_REJECTED
                                            )
                                            warnings.append("total-capture-budget-exhausted")
                                        else:
                                            try:
                                                parsed_json = json.loads(body)
                                            except json.JSONDecodeError:
                                                parsed_json = None
                                            if is_configuration_json(parsed_json):
                                                transport_type = (
                                                    BrowserTransportType.SCRIPT_CONFIGURATION
                                                )
                                            if approved_json_payload_for_profile(
                                                provider_id=provider_id,
                                                resource_type=resource_type,
                                                payload=parsed_json,
                                            ):
                                                body_capture_state = (
                                                    BrowserBodyCaptureState.CAPTURED
                                                )
                                                body_text = body
                                                captured_total_bytes += encoded_size
                                                route_id = current_route_id
                                                responses.append(
                                                    BrowserResponseObservation(
                                                        provider_id=provider_id,
                                                        sport=sport,
                                                        acquisition_cycle_id=(acquisition_cycle_id),
                                                        page_route_id=route_id,
                                                        response_url=response_url,
                                                        observed_at_utc=self._clock(),
                                                        request_method=request_method,
                                                        transport_type=transport_type,
                                                        hostname=(urlparse(response_url).hostname),
                                                        sanitized_path_hash=(
                                                            sanitized_path_hash(response_url)
                                                        ),
                                                        declared_content_length=(content_length),
                                                        actual_captured_byte_length=(encoded_size),
                                                        redirect_classification=(
                                                            redirect_classification
                                                        ),
                                                        body_capture_state=(body_capture_state),
                                                        content_type=str(content_type),
                                                        body_text=body,
                                                        status_code=status_code,
                                                        warnings=(),
                                                    )
                                                )
                                            else:
                                                body_capture_state = (
                                                    BrowserBodyCaptureState.NOT_APPROVED
                                                )
                            if provider_id == "betclic-pt":
                                remaining_capture_bytes = (
                                    self._maximum_total_capture_bytes - captured_total_bytes
                                )
                                if remaining_capture_bytes <= 0:
                                    grpc_web_failure_code = "total-capture-budget-rejected"
                                    body_capture_state = (
                                        BrowserBodyCaptureState.TOTAL_BUDGET_REJECTED
                                    )
                                else:
                                    grpc_outcome = observe_betclic_grpc_response(
                                        response=response,
                                        response_url=response_url,
                                        content_type=(
                                            str(content_type) if content_type is not None else None
                                        ),
                                        content_length=content_length,
                                        maximum_bytes=min(
                                            self._maximum_response_bytes,
                                            remaining_capture_bytes,
                                        ),
                                        diagnostic_directory=diagnostic_directory,
                                    )
                                    grpc_web_envelope_recognized = grpc_outcome.envelope_recognized
                                    grpc_web_failure_code = grpc_outcome.failure_code
                                    grpc_web_body_read = grpc_outcome.body_read
                                    grpc_web_evidence_stored = grpc_outcome.evidence_stored
                                    grpc_web_malformed_or_truncated = (
                                        grpc_outcome.malformed_or_truncated
                                    )
                                    grpc_actual_byte_size = grpc_outcome.actual_byte_size
                                    if (
                                        grpc_outcome.body_read
                                        and grpc_outcome.actual_byte_size is not None
                                        and grpc_outcome.actual_byte_size <= remaining_capture_bytes
                                    ):
                                        captured_total_bytes += grpc_outcome.actual_byte_size
                                        body_capture_state = BrowserBodyCaptureState.METADATA_ONLY
                                    elif (
                                        grpc_outcome.actual_byte_size is not None
                                        and grpc_outcome.actual_byte_size > remaining_capture_bytes
                                    ):
                                        body_capture_state = (
                                            BrowserBodyCaptureState.TOTAL_BUDGET_REJECTED
                                        )
                                    grpc_diagnostic = grpc_outcome.diagnostic
                                    if grpc_diagnostic is not None:
                                        grpc_web_diagnostics.append(grpc_diagnostic)
                                        diagnostics.append(
                                            BrowserDiagnosticReference(
                                                capture_kind=grpc_diagnostic.capture_kind,
                                                checksum_sha256=(grpc_diagnostic.checksum_sha256),
                                                relative_path=grpc_diagnostic.relative_path,
                                                byte_count=grpc_diagnostic.byte_count,
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
                                structural_body_text=inspected_body_text,
                                byte_size=(
                                    grpc_actual_byte_size
                                    if grpc_actual_byte_size is not None
                                    else (
                                        len(body_text.encode("utf-8"))
                                        if body_text is not None
                                        else (
                                            actual_body_size
                                            if actual_body_size is not None
                                            else content_length
                                        )
                                    )
                                ),
                                provider_id=provider_id,
                                sport=sport,
                                acquisition_cycle_id=acquisition_cycle_id,
                                page_route_id=current_route_id,
                                request_method=request_method,
                                transport_type=transport_type,
                                declared_content_length=content_length,
                                actual_captured_byte_length=(
                                    len(body_text.encode("utf-8"))
                                    if body_text is not None
                                    else None
                                ),
                                redirect_classification=redirect_classification,
                                body_capture_state=body_capture_state,
                                grpc_web_envelope_recognized=grpc_web_envelope_recognized,
                                grpc_web_failure_code=grpc_web_failure_code,
                                grpc_web_body_read=grpc_web_body_read,
                                grpc_web_evidence_stored=grpc_web_evidence_stored,
                                grpc_web_malformed_or_truncated=(grpc_web_malformed_or_truncated),
                            )
                            if meta is not None:
                                network_metadata.append(meta)
                        except PermanentSourceError:
                            return
                        except Exception as exc:  # noqa: BLE001 - observation best-effort
                            _LOGGER.debug(
                                "ignored response observation error: %s",
                                type(exc).__name__,
                            )

                    page.on("response", _on_response)

                    def _on_websocket(socket: object) -> None:
                        """Retain connection metadata only; never read frames."""
                        try:
                            socket_url = str(getattr(socket, "url", ""))
                            meta = build_websocket_metadata(
                                socket_url=socket_url,
                                allowed_hostnames=allowed_hostnames,
                                observed_at_utc=self._clock(),
                                provider_id=provider_id,
                                sport=sport,
                                acquisition_cycle_id=acquisition_cycle_id,
                                page_route_id=current_route_id,
                            )
                            network_metadata.append(meta)
                        except PermanentSourceError:
                            return

                    page.on("websocket", _on_websocket)

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
                                build_structural_page_observation(
                                    provider_id=provider_id,
                                    page_route_id=page_route_id,
                                    final_url=page.url,
                                    observed_at_utc=self._clock(),
                                    allowed_hostnames=allowed_hostnames,
                                    title=None,
                                    body_html=None,
                                    body_text=None,
                                    block_reason=block_reason,
                                )
                            )
                            break
                        except RetryableSourceError:
                            raise
                        except PlaywrightError as exc:
                            message = (
                                f"incomplete page load for {page_route_id}: {type(exc).__name__}"
                            )
                            raise RetryableSourceError(message) from exc
                        final_url = page.url
                        title = page.title()
                        try:
                            body_html = page.inner_html("body")
                        except PlaywrightError:
                            body_html = None
                        try:
                            body_text = page.inner_text("body")
                        except PlaywrightError:
                            body_text = None
                        detected = classify_readiness_block(page)
                        page_observation = build_structural_page_observation(
                            provider_id=provider_id,
                            page_route_id=page_route_id,
                            final_url=final_url,
                            observed_at_utc=self._clock(),
                            allowed_hostnames=allowed_hostnames,
                            title=title,
                            body_html=body_html,
                            body_text=body_text,
                            block_reason=detected,
                        )
                        pages.append(page_observation)
                        detected = page_observation.block_reason
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
            raise_for_classified_error(
                classify_browser_crash(f"browser process failure: {type(exc).__name__}")
            )

        return BrowserAcquisitionResult(
            provider_id=provider_id,
            sport=sport,
            acquisition_cycle_id=acquisition_cycle_id,
            observed_at_utc=observed_at_utc,
            browser_mode=browser_mode,
            pages=tuple(pages),
            responses=tuple(responses),
            diagnostics=tuple(diagnostics),
            block_reason=block_reason,
            warnings=tuple(sorted(set(warnings))),
            cookie_banner_dismissed=cookie_banner_dismissed,
            network_metadata=tuple(network_metadata),
            grpc_web_diagnostics=tuple(grpc_web_diagnostics),
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
        diagnostic_directory: Path | None = None,
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
                "diagnostic_directory": diagnostic_directory,
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
