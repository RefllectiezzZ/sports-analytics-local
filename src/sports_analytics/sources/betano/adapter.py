"""Betano adapter: fixed-route browser acquisition into raw captures and parses."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from sports_analytics.core.exceptions import PermanentSourceError
from sports_analytics.sources.betano.catalog import ADAPTER_VERSION, BETANO_CATALOG, PROVIDER_ID
from sports_analytics.sources.betano.parser import parse_betano_acquisition
from sports_analytics.sources.bookmaker_catalog import reject_forbidden_job_controls
from sports_analytics.sources.bookmaker_contracts import ProviderAcquisitionBundle
from sports_analytics.sources.browser.contracts import BrowserAcquisitionResult, BrowserMode
from sports_analytics.sources.browser.playwright_runtime import (
    BrowserSession,
    PlaywrightBrowserSession,
)
from sports_analytics.sources.raw_capture import BookmakerRawCaptureStore


def acquire_betano_current_odds(
    *,
    sport: str,
    acquisition_cycle_id: str,
    observed_at_utc: datetime,
    raw_directory: Path,
    browser_mode: BrowserMode = BrowserMode.VISIBLE,
    session: BrowserSession | None = None,
    maximum_capture_bytes: int = 2_097_152,
) -> tuple[BrowserAcquisitionResult, ProviderAcquisitionBundle, tuple[str, ...]]:
    """Acquire Betano pre-match fixtures/odds via ordinary visible browser automation."""
    catalog = BETANO_CATALOG
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
    capture_paths: list[str] = []
    for index, response in enumerate(result.responses):
        artifact = store.store_text(
            source_name=PROVIDER_ID,
            capture_kind="provider-json",
            content=response.body_text,
            retrieved_at=observed_at_utc,
            extension="json",
            maximum_bytes=maximum_capture_bytes,
            source_url=response.response_url,
        )
        capture_paths.append(artifact.relative_path)
        del index
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
            capture_paths.append(artifact.relative_path)
    bundle = parse_betano_acquisition(result, adapter_version=ADAPTER_VERSION)
    return result, bundle, tuple(capture_paths)


def reject_arbitrary_betano_controls(payload: dict[str, object]) -> None:
    """Reject forbidden payload controls for Betano jobs."""
    reject_forbidden_job_controls(payload)
    if payload.get("provider_id") not in (None, PROVIDER_ID):
        msg = f"provider_id must be {PROVIDER_ID}"
        raise PermanentSourceError(msg)
