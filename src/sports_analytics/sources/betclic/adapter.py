"""Betclic adapter: fixed-route browser acquisition into raw captures and parses."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from sports_analytics.core.exceptions import PermanentSourceError
from sports_analytics.sources.betclic.catalog import (
    ADAPTER_VERSION,
    BETCLIC_CATALOG,
    PARSER_VERSION,
    PROVIDER_ID,
)
from sports_analytics.sources.bookmaker_catalog import reject_forbidden_job_controls
from sports_analytics.sources.bookmaker_contracts import ProviderAcquisitionBundle
from sports_analytics.sources.bookmaker_extraction.contracts import ExtractionProfile
from sports_analytics.sources.bookmaker_extraction.pipeline import apply_extraction_profile
from sports_analytics.sources.bookmaker_extraction.registry import get_verified_extraction_profile
from sports_analytics.sources.browser.contracts import BrowserAcquisitionResult, BrowserMode
from sports_analytics.sources.browser.playwright_runtime import (
    BrowserSession,
    PlaywrightBrowserSession,
)
from sports_analytics.sources.raw_capture import BookmakerRawCapture, BookmakerRawCaptureStore


def acquire_betclic_current_odds(
    *,
    sport: str,
    acquisition_cycle_id: str,
    observed_at_utc: datetime,
    raw_directory: Path,
    browser_mode: BrowserMode = BrowserMode.VISIBLE,
    session: BrowserSession | None = None,
    maximum_capture_bytes: int = 2_097_152,
    extraction_profile: ExtractionProfile | None = None,
) -> tuple[BrowserAcquisitionResult, ProviderAcquisitionBundle, tuple[BookmakerRawCapture, ...]]:
    """Acquire Betclic pre-match fixtures/odds via ordinary visible browser automation."""
    catalog = BETCLIC_CATALOG
    routes = catalog.routes_for_sport(sport)
    browser = session or PlaywrightBrowserSession()
    result = browser.acquire(
        provider_id=PROVIDER_ID,
        sport=sport,
        acquisition_cycle_id=acquisition_cycle_id,
        allowed_hostnames=catalog.allowed_hostnames,
        start_urls=routes,
        observed_at_utc=observed_at_utc,
        browser_mode=browser_mode,
    )
    store = BookmakerRawCaptureStore(raw_directory)
    captures: list[BookmakerRawCapture] = []
    for response in result.responses:
        artifact = store.store_text(
            source_name=PROVIDER_ID,
            capture_kind="provider-json",
            content=response.body_text,
            retrieved_at=observed_at_utc,
            extension="json",
            maximum_bytes=maximum_capture_bytes,
            source_url=response.response_url,
        )
        captures.append(artifact)
    for page in result.pages:
        if page.sanitized_dom_fragment:
            artifact = store.store_text(
                source_name=PROVIDER_ID,
                capture_kind="dom-fragment",
                content=page.sanitized_dom_fragment,
                retrieved_at=observed_at_utc,
                extension="txt",
                maximum_bytes=maximum_capture_bytes,
                source_url=page.final_url,
            )
            captures.append(artifact)
    profile = (
        extraction_profile
        if extraction_profile is not None
        else get_verified_extraction_profile(PROVIDER_ID)
    )
    bundle = apply_extraction_profile(
        profile=profile,
        browser_result=result,
        captures=tuple(captures),
        adapter_version=ADAPTER_VERSION,
        parser_version=PARSER_VERSION,
        observed_at_utc=observed_at_utc,
        sport=sport,
    )
    return result, bundle, tuple(captures)


def reject_arbitrary_betclic_controls(payload: dict[str, object]) -> None:
    """Reject forbidden payload controls for Betclic jobs."""
    reject_forbidden_job_controls(payload)
    if payload.get("provider_id") not in (None, PROVIDER_ID):
        msg = f"provider_id must be {PROVIDER_ID}"
        raise PermanentSourceError(msg)
