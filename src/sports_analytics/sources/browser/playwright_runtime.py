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
from pathlib import Path
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
    BrowserDiagnosticReference,
    BrowserDomCandidate,
    BrowserGrpcWebDiagnostic,
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
_MAX_EPHEMERAL_BODY_HTML_CHARS = 65_536
_MAX_EPHEMERAL_BODY_TEXT_CHARS = 16_384
_MAX_EPHEMERAL_TITLE_CHARS = 512


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
        """Collect page and first-party JSON observations for fixed routes."""


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
    provider_id: str | None = None,
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
        diagnostic_directory: Path | None = None,
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
        diagnostics: list[BrowserDiagnosticReference] = []
        grpc_web_diagnostics: list[BrowserGrpcWebDiagnostic] = []
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
                                try:
                                    body = response.text()  # type: ignore[attr-defined]
                                except Exception as exc:  # noqa: BLE001
                                    _LOGGER.debug(
                                        "ignored response body read: %s",
                                        type(exc).__name__,
                                    )
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
                            if provider_id == "betclic-pt":
                                grpc_outcome = observe_betclic_grpc_response(
                                    response=response,
                                    response_url=response_url,
                                    content_type=(
                                        str(content_type) if content_type is not None else None
                                    ),
                                    content_length=content_length,
                                    maximum_bytes=self._maximum_response_bytes,
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
                                grpc_diagnostic = grpc_outcome.diagnostic
                                if grpc_diagnostic is not None:
                                    grpc_web_diagnostics.append(grpc_diagnostic)
                                    diagnostics.append(
                                        BrowserDiagnosticReference(
                                            capture_kind=grpc_diagnostic.capture_kind,
                                            checksum_sha256=grpc_diagnostic.checksum_sha256,
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
                                byte_size=(
                                    grpc_actual_byte_size
                                    if grpc_actual_byte_size is not None
                                    else (
                                        len(body_text.encode("utf-8"))
                                        if body_text is not None
                                        else content_length
                                    )
                                ),
                                provider_id=provider_id,
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
