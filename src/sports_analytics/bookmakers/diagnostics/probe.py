"""Bounded browser-observed structural probe for bookmaker providers."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sports_analytics.bookmakers.diagnostics.fingerprint import structural_fingerprint
from sports_analytics.bookmakers.diagnostics.paths import resolve_diagnostic_directory
from sports_analytics.bookmakers.diagnostics.persistence import publish_diagnostic_json
from sports_analytics.bookmakers.diagnostics.redaction import sanitize_sample_payload
from sports_analytics.bookmakers.types import PROVIDER_BETANO_PT, PROVIDER_BETCLIC_PT
from sports_analytics.core.exceptions import ConfigurationError
from sports_analytics.sources.betano.catalog import BETANO_CATALOG
from sports_analytics.sources.betclic.catalog import BETCLIC_CATALOG
from sports_analytics.sources.bookmaker_catalog import SUPPORTED_BOOKMAKER_SPORTS
from sports_analytics.sources.browser.contracts import (
    BrowserAcquisitionResult,
    BrowserBlockReason,
    BrowserMode,
    BrowserNetworkMetadata,
    BrowserPageObservation,
    BrowserResponseObservation,
)
from sports_analytics.sources.browser.playwright_runtime import (
    BrowserSession,
    PlaywrightBrowserSession,
)
from sports_analytics.sources.browser.readiness import (
    ReadinessBlockedError,
    readiness_predicate_for_provider,
    wait_for_readiness,
)
from sports_analytics.sources.browser.safety import (
    classify_block_signals,
    validate_provider_navigation_url,
)

_PROVIDER_CATALOGS = {
    PROVIDER_BETANO_PT: BETANO_CATALOG,
    PROVIDER_BETCLIC_PT: BETCLIC_CATALOG,
}


@dataclass(frozen=True, slots=True)
class ProbeResponseEvidence:
    """Sanitized first-party JSON response observation."""

    provider: str
    route_id: str
    hostname: str
    http_status: int
    content_type: str | None
    byte_size: int
    structural_fingerprint: str
    candidate_paths: tuple[str, ...]
    sanitized_sample: dict[str, Any]
    readiness_classification: str


@dataclass(frozen=True, slots=True)
class ProbePageEvidence:
    """Sanitized DOM/page observation when JSON is unavailable."""

    provider: str
    route_id: str
    hostname: str
    block_classification: str | None
    dom_fingerprint: str | None
    candidate_paths: tuple[str, ...]
    sanitized_sample: dict[str, Any]
    dom_candidates: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """Outcome of one bounded visible probe cycle."""

    provider: str
    sport: str
    duration_seconds: float
    blocked: bool
    block_reason: str | None
    responses: tuple[ProbeResponseEvidence, ...]
    pages: tuple[ProbePageEvidence, ...]
    diagnostic_relative_path: str
    network_metadata: tuple[dict[str, Any], ...] = ()
    classifications: tuple[str, ...] = ()
    grpc_web_diagnostics: tuple[dict[str, Any], ...] = ()


def probe_bookmaker(
    *,
    provider_id: str,
    sport: str,
    duration_seconds: int = 30,
    diagnostic_directory: str | Path | None = None,
    session: BrowserSession | None = None,
    clock: Callable[[], datetime] | None = None,
    browser_mode: BrowserMode = BrowserMode.HEADLESS,
) -> ProbeResult:
    """Collect sanitized structural evidence from one provider sport route."""
    if provider_id not in _PROVIDER_CATALOGS:
        msg = f"unsupported probe provider: {provider_id}"
        raise ConfigurationError(msg)
    if sport not in SUPPORTED_BOOKMAKER_SPORTS:
        msg = f"unsupported probe sport: {sport}"
        raise ConfigurationError(msg)
    if duration_seconds < 1 or duration_seconds > 300:
        msg = "duration_seconds must be between 1 and 300"
        raise ConfigurationError(msg)
    catalog = _PROVIDER_CATALOGS[provider_id]
    routes = catalog.sport_routes.get(sport)
    if not routes:
        msg = f"provider {provider_id} has no fixed route for sport {sport}"
        raise ConfigurationError(msg)
    output_dir = resolve_diagnostic_directory(diagnostic_directory)
    started = time.monotonic()
    # Progressing clock — never freeze to a single timestamp for deadline math.
    if clock is not None:
        clock_fn: Callable[[], datetime] = clock
    else:
        clock_fn = lambda: datetime.now(tz=UTC)  # noqa: E731
    started_at = clock_fn()
    deadline = started_at + timedelta(seconds=duration_seconds)
    # Bounded by deadline inside acquire; full duration window for network dwell.
    observation_window_ms = duration_seconds * 1000
    browser_session = session or PlaywrightBrowserSession(clock=clock_fn)

    def observation_complete() -> bool:
        # Duration exhaustion is enforced by the observation window / deadline.
        # Candidate-key completion is evaluated against live metadata inside acquire;
        # this callback remains available for injected sessions / tests.
        return clock_fn() >= deadline

    acquisition = browser_session.acquire(
        provider_id=provider_id,
        sport=sport,
        acquisition_cycle_id=f"probe-{provider_id}-{sport}",
        allowed_hostnames=catalog.allowed_hostnames,
        start_urls=routes,
        observed_at_utc=started_at,
        browser_mode=browser_mode,
        deadline_at_utc=deadline,
        observation_window_ms=observation_window_ms,
        observation_complete=observation_complete,
        diagnostic_directory=output_dir,
    )
    # Report actual elapsed duration; do not clamp with min().
    elapsed = time.monotonic() - started
    responses = tuple(_response_evidence(item) for item in acquisition.responses)
    pages = tuple(_page_evidence(item) for item in acquisition.pages)
    network_metadata = tuple(
        _network_metadata_payload(item) for item in acquisition.network_metadata
    )
    grpc_web_diagnostics = tuple(
        _grpc_web_diagnostic_payload(item) for item in acquisition.grpc_web_diagnostics
    )
    result = ProbeResult(
        provider=provider_id,
        sport=sport,
        duration_seconds=elapsed,
        blocked=acquisition.block_reason is not None,
        block_reason=None if acquisition.block_reason is None else acquisition.block_reason.value,
        responses=responses,
        pages=pages,
        diagnostic_relative_path=_write_probe_artifact(
            output_dir,
            provider_id=provider_id,
            sport=sport,
            acquisition=acquisition,
            responses=responses,
            pages=pages,
            network_metadata=network_metadata,
            duration_seconds=elapsed,
        ),
        network_metadata=network_metadata,
        classifications=_probe_classifications(
            provider_id=provider_id,
            blocked=acquisition.block_reason is not None,
            pages=pages,
            network_metadata=network_metadata,
        ),
        grpc_web_diagnostics=grpc_web_diagnostics,
    )
    return result


def collect_probe_from_acquisition(
    *,
    provider_id: str,
    sport: str,
    acquisition: BrowserAcquisitionResult,
    duration_seconds: float,
    diagnostic_directory: str | Path | None = None,
) -> ProbeResult:
    """Build a probe result from an existing browser acquisition (tests)."""
    output_dir = resolve_diagnostic_directory(diagnostic_directory)
    responses = tuple(_response_evidence(item) for item in acquisition.responses)
    pages = tuple(_page_evidence(item) for item in acquisition.pages)
    network_metadata = tuple(
        _network_metadata_payload(item) for item in acquisition.network_metadata
    )
    grpc_web_diagnostics = tuple(
        _grpc_web_diagnostic_payload(item) for item in acquisition.grpc_web_diagnostics
    )
    classifications = _probe_classifications(
        provider_id=provider_id,
        blocked=acquisition.block_reason is not None,
        pages=pages,
        network_metadata=network_metadata,
    )
    return ProbeResult(
        provider=provider_id,
        sport=sport,
        duration_seconds=duration_seconds,
        blocked=acquisition.block_reason is not None,
        block_reason=None if acquisition.block_reason is None else acquisition.block_reason.value,
        responses=responses,
        pages=pages,
        diagnostic_relative_path=_write_probe_artifact(
            output_dir,
            provider_id=provider_id,
            sport=sport,
            acquisition=acquisition,
            responses=responses,
            pages=pages,
            network_metadata=network_metadata,
            duration_seconds=duration_seconds,
        ),
        network_metadata=network_metadata,
        classifications=classifications,
        grpc_web_diagnostics=grpc_web_diagnostics,
    )


def handle_cookie_consent(page: Any, *, provider_id: str) -> bool:
    """Dismiss ordinary cookie banners when they block public content."""
    from sports_analytics.sources.browser.cookie_consent import dismiss_cookie_consent

    return dismiss_cookie_consent(page, provider_id=provider_id)


def _response_evidence(item: BrowserResponseObservation) -> ProbeResponseEvidence:
    parsed: dict[str, Any] | None = None
    try:
        loaded = json.loads(item.body_text)
        if isinstance(loaded, dict):
            parsed = loaded
    except json.JSONDecodeError:
        parsed = None
    sample = {"non_json": True}
    fingerprint = structural_fingerprint(sample)
    candidate_paths: tuple[str, ...] = ()
    if parsed is not None:
        sample = sanitize_sample_payload(parsed)
        fingerprint = structural_fingerprint(parsed)
        candidate_paths = tuple(sorted(_candidate_paths(parsed)))
    return ProbeResponseEvidence(
        provider=item.provider_id,
        route_id=item.page_route_id,
        hostname=item.hostname or "",
        http_status=item.status_code,
        content_type=item.content_type,
        byte_size=len(item.body_text.encode("utf-8")),
        structural_fingerprint=fingerprint,
        candidate_paths=candidate_paths,
        sanitized_sample=sample,
        readiness_classification="json-observed",
    )


def _page_evidence(item: BrowserPageObservation) -> ProbePageEvidence:
    dom_candidates = item.structural_candidates
    dom_sample: dict[str, Any] = {"dom_candidates": []}
    if dom_candidates:
        dom_sample["dom_candidates"] = [candidate.as_dict() for candidate in dom_candidates]
    block = None if item.block_reason is None else item.block_reason.value
    return ProbePageEvidence(
        provider=item.provider_id,
        route_id=item.page_route_id,
        hostname=item.hostname,
        block_classification=block,
        dom_fingerprint=None if not dom_candidates else structural_fingerprint(dom_sample),
        candidate_paths=tuple(
            sorted({f"dom.{candidate.tag}" for candidate in dom_candidates if candidate.tag})
        ),
        sanitized_sample=dom_sample,
        dom_candidates=tuple(candidate.as_dict() for candidate in dom_candidates),
    )


def _network_metadata_payload(item: BrowserNetworkMetadata) -> dict[str, Any]:
    return {
        "provider_id": item.provider_id,
        "sport": item.sport,
        "acquisition_cycle_id": item.acquisition_cycle_id,
        "page_route_id": item.page_route_id,
        "source_event_id": item.source_event_id,
        "request_method": item.request_method,
        "transport_type": (None if item.transport_type is None else item.transport_type.value),
        "hostname": item.hostname,
        "resource_type": item.resource_type,
        "status_code": item.status_code,
        "content_type": item.content_type,
        "byte_size": item.byte_size,
        "declared_content_length": item.declared_content_length,
        "actual_captured_byte_length": item.actual_captured_byte_length,
        "redirect_classification": item.redirect_classification.value,
        "body_capture_state": item.body_capture_state.value,
        "contributing_capture_checksum": item.contributing_capture_checksum,
        "sanitized_path_hash": item.sanitized_path_hash,
        "structural_fingerprint": item.structural_fingerprint,
        "hostname_approved": item.hostname_approved,
        "candidate_keys_detected": item.candidate_keys_detected,
        "event_candidate_count": item.event_candidate_count,
        "market_candidate_count": item.market_candidate_count,
        "selection_candidate_count": item.selection_candidate_count,
        "body_captured": item.body_captured,
        "grpc_web_envelope_recognized": item.grpc_web_envelope_recognized,
        "grpc_web_failure_code": item.grpc_web_failure_code,
        "grpc_web_body_read": item.grpc_web_body_read,
        "grpc_web_evidence_stored": item.grpc_web_evidence_stored,
        "grpc_web_malformed_or_truncated": item.grpc_web_malformed_or_truncated,
        "observed_at_utc": item.observed_at_utc.isoformat(),
        "approved_route_id": item.approved_route_id,
        "approved_path_template": item.approved_path_template,
    }


def _grpc_web_diagnostic_payload(item: Any) -> dict[str, Any]:
    return {
        "capture_kind": item.capture_kind,
        "checksum": item.checksum_sha256,
        "relative_path": item.relative_path,
        "byte_count": item.byte_count,
        "framing": item.framing,
        "data_frame_count": item.data_frame_count,
        "trailer_frame_count": item.trailer_frame_count,
        "compression_flag_present": item.compression_flag_present,
        "total_framed_payload_bytes": item.total_framed_payload_bytes,
        "malformed_or_truncated": item.malformed_or_truncated,
        "grpc_status": item.grpc_status,
    }


def _candidate_paths(value: Any, *, prefix: str = "") -> list[str]:
    paths: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            paths.append(path)
            paths.extend(_candidate_paths(item, prefix=path))
    elif isinstance(value, list) and value:
        paths.extend(_candidate_paths(value[0], prefix=f"{prefix}[]"))
    return paths[:40]


def _probe_classifications(
    *,
    provider_id: str,
    blocked: bool,
    pages: tuple[ProbePageEvidence, ...],
    network_metadata: tuple[dict[str, Any], ...],
) -> tuple[str, ...]:
    if provider_id != PROVIDER_BETCLIC_PT:
        return ()
    if blocked:
        return ("betclic-blocked",)
    classifications: set[str] = set()
    grpc_observed = any(
        item.get("approved_route_id") == "betclic-match-service-get-popular-v2"
        for item in network_metadata
    )
    if grpc_observed:
        classifications.update(
            {
                "betclic-offering-grpc-observed",
                "betclic-offering-schema-unverified",
            }
        )
        if any(bool(item.get("grpc_web_envelope_recognized")) for item in network_metadata):
            classifications.add("betclic-offering-envelope-recognized")
    if any(page.dom_candidates for page in pages):
        classifications.add("betclic-event-dom-candidates-observed")
    if not classifications:
        classifications.add("betclic-no-event-evidence")
    return tuple(sorted(classifications))


def _write_probe_artifact(
    output_dir: Path,
    *,
    provider_id: str,
    sport: str,
    acquisition: BrowserAcquisitionResult,
    responses: tuple[ProbeResponseEvidence, ...],
    pages: tuple[ProbePageEvidence, ...],
    network_metadata: tuple[dict[str, Any], ...],
    duration_seconds: float,
) -> str:
    filename = f"probe-{provider_id}-{sport}-{int(time.time())}.json"
    path = output_dir / filename
    payload = {
        "provider": provider_id,
        "sport": sport,
        "duration_seconds": duration_seconds,
        "blocked": acquisition.block_reason is not None,
        "block_reason": (
            None if acquisition.block_reason is None else acquisition.block_reason.value
        ),
        "responses": [asdict(item) for item in responses],
        "pages": [asdict(item) for item in pages],
        "network_metadata": list(network_metadata),
        "transport_summary": _transport_summary(network_metadata),
        "grpc_web_diagnostics": [
            _grpc_web_diagnostic_payload(item) for item in acquisition.grpc_web_diagnostics
        ],
        "classifications": list(
            _probe_classifications(
                provider_id=provider_id,
                blocked=acquisition.block_reason is not None,
                pages=pages,
                network_metadata=network_metadata,
            )
        ),
    }
    try:
        publish_diagnostic_json(path, payload)
    except BaseException:
        _remove_new_grpc_evidence(output_dir, acquisition=acquisition)
        raise
    return filename


def _transport_summary(
    network_metadata: tuple[dict[str, Any], ...],
) -> dict[str, Any]:
    """Aggregate safe structural counts without provider-controlled values."""

    def _counts(key: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in network_metadata:
            value = item.get(key)
            if value is None:
                continue
            normalized = str(value)
            counts[normalized] = counts.get(normalized, 0) + 1
        return dict(sorted(counts.items()))

    approved_hosts: dict[str, int] = {}
    for item in network_metadata:
        if not item.get("hostname_approved"):
            continue
        hostname = item.get("hostname")
        if hostname is not None:
            key = str(hostname)
            approved_hosts[key] = approved_hosts.get(key, 0) + 1
    return {
        "response_metadata_count": len(network_metadata),
        "resource_type_counts": _counts("resource_type"),
        "transport_type_counts": _counts("transport_type"),
        "status_code_counts": _counts("status_code"),
        "body_capture_state_counts": _counts("body_capture_state"),
        "approved_host_counts": dict(sorted(approved_hosts.items())),
        "captured_body_count": sum(bool(item.get("body_captured")) for item in network_metadata),
        "truncated_or_budget_rejected_count": sum(
            item.get("body_capture_state")
            in {
                "declared-size-rejected",
                "actual-size-rejected",
                "total-budget-rejected",
            }
            for item in network_metadata
        ),
        "event_candidate_count": sum(
            int(item.get("event_candidate_count") or 0) for item in network_metadata
        ),
        "market_candidate_count": sum(
            int(item.get("market_candidate_count") or 0) for item in network_metadata
        ),
        "selection_candidate_count": sum(
            int(item.get("selection_candidate_count") or 0) for item in network_metadata
        ),
    }


def _remove_new_grpc_evidence(
    output_dir: Path,
    *,
    acquisition: BrowserAcquisitionResult,
) -> None:
    root = output_dir.resolve()
    for item in acquisition.grpc_web_diagnostics:
        if not item.newly_created:
            continue
        candidate = (root / item.relative_path).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            continue
        candidate.unlink(missing_ok=True)


def classify_probe_block(
    *,
    provider_id: str,
    page: Any,
    page_route_id: str,
) -> BrowserBlockReason | None:
    """Classify readiness blocks during probe navigation."""
    try:
        wait_for_readiness(
            page,
            predicate=readiness_predicate_for_provider(provider_id),
            page_route_id=page_route_id,
        )
    except ReadinessBlockedError as exc:
        return exc.block_reason
    title = page.title()
    body = page.inner_text("body")
    return classify_block_signals(title=title, body_text=body)


def validate_probe_url(url: str, *, allowed_hostnames: frozenset[str]) -> str:
    """Validate one probe navigation URL."""
    approved = validate_provider_navigation_url(url, allowed_hostnames=allowed_hostnames)
    return approved.hostname
