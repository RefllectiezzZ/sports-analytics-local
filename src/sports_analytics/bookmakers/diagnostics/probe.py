"""Visible-browser structural probe for bookmaker providers."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from sports_analytics.bookmakers.diagnostics.fingerprint import structural_fingerprint
from sports_analytics.bookmakers.diagnostics.paths import resolve_diagnostic_directory
from sports_analytics.bookmakers.diagnostics.redaction import redact_text, sanitize_sample_payload
from sports_analytics.bookmakers.types import PROVIDER_BETANO_PT, PROVIDER_BETCLIC_PT
from sports_analytics.core.exceptions import ConfigurationError
from sports_analytics.sources.betano.catalog import BETANO_CATALOG
from sports_analytics.sources.betclic.catalog import BETCLIC_CATALOG
from sports_analytics.sources.bookmaker_catalog import SUPPORTED_BOOKMAKER_SPORTS
from sports_analytics.sources.browser.contracts import (
    BrowserAcquisitionResult,
    BrowserBlockReason,
    BrowserMode,
    BrowserPageObservation,
    BrowserResponseObservation,
)
from sports_analytics.sources.browser.playwright_runtime import PlaywrightBrowserSession
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
    title: str | None
    block_classification: str | None
    dom_fingerprint: str | None
    candidate_paths: tuple[str, ...]
    sanitized_sample: dict[str, Any]


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


def probe_bookmaker(
    *,
    provider_id: str,
    sport: str,
    duration_seconds: int = 30,
    diagnostic_directory: str | Path | None = None,
    session: PlaywrightBrowserSession | None = None,
    clock: Any | None = None,
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
    now = datetime.now(tz=UTC) if clock is None else clock()
    deadline = now + timedelta(seconds=duration_seconds)
    browser_session = session or PlaywrightBrowserSession(clock=(lambda: now))
    acquisition = browser_session.acquire(
        provider_id=provider_id,
        sport=sport,
        acquisition_cycle_id=f"probe-{provider_id}-{sport}",
        allowed_hostnames=catalog.allowed_hostnames,
        start_urls=routes,
        observed_at_utc=now,
        browser_mode=BrowserMode.VISIBLE,
        deadline_at_utc=deadline,
    )
    # Report actual elapsed duration; do not clamp with min().
    elapsed = time.monotonic() - started
    responses = tuple(_response_evidence(item) for item in acquisition.responses)
    pages = tuple(_page_evidence(item) for item in acquisition.pages)
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
            duration_seconds=elapsed,
        ),
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
            duration_seconds=duration_seconds,
        ),
    )


def handle_cookie_consent(page: Any, *, provider_id: str) -> bool:
    """Dismiss ordinary cookie banners when they block public content."""
    from sports_analytics.sources.browser.cookie_consent import dismiss_cookie_consent

    return dismiss_cookie_consent(page, provider_id=provider_id)


def _response_evidence(item: BrowserResponseObservation) -> ProbeResponseEvidence:
    hostname = urlparse(item.response_url).hostname or ""
    parsed: dict[str, Any] | None = None
    try:
        loaded = json.loads(item.body_text)
        if isinstance(loaded, dict):
            parsed = loaded
    except json.JSONDecodeError:
        parsed = None
    sample = {"non_json": True, "preview": redact_text(item.body_text[:200])}
    fingerprint = structural_fingerprint(sample)
    candidate_paths: tuple[str, ...] = ()
    if parsed is not None:
        sample = sanitize_sample_payload(parsed)
        fingerprint = structural_fingerprint(parsed)
        candidate_paths = tuple(sorted(_candidate_paths(parsed)))
    return ProbeResponseEvidence(
        provider=item.provider_id,
        route_id=item.page_route_id,
        hostname=hostname,
        http_status=item.status_code,
        content_type=item.content_type,
        byte_size=len(item.body_text.encode("utf-8")),
        structural_fingerprint=fingerprint,
        candidate_paths=candidate_paths,
        sanitized_sample=sample,
        readiness_classification="json-observed",
    )


def _page_evidence(item: BrowserPageObservation) -> ProbePageEvidence:
    hostname = urlparse(item.final_url).hostname or ""
    dom_fragment = item.sanitized_dom_fragment or ""
    dom_sample = {
        "title": redact_text(item.title or ""),
        "dom_preview": redact_text(dom_fragment[:200]),
    }
    block = None if item.block_reason is None else item.block_reason.value
    return ProbePageEvidence(
        provider=item.provider_id,
        route_id=item.page_route_id,
        hostname=hostname,
        title=redact_text(item.title) if item.title else None,
        block_classification=block,
        dom_fingerprint=None if not dom_fragment else structural_fingerprint(dom_sample),
        candidate_paths=(),
        sanitized_sample=dom_sample,
    )


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


def _write_probe_artifact(
    output_dir: Path,
    *,
    provider_id: str,
    sport: str,
    acquisition: BrowserAcquisitionResult,
    responses: tuple[ProbeResponseEvidence, ...],
    pages: tuple[ProbePageEvidence, ...],
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
    }
    path.write_text(json.dumps(payload, sort_keys=True, indent=2), encoding="utf-8")
    return filename


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
