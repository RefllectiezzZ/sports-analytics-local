"""Betano adapter: fixed-route browser acquisition into raw captures and parses."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from sports_analytics.bookmakers.navigation import (
    NavigationPlanExecutor,
    StageBNavigationCapability,
)
from sports_analytics.bookmakers.window import AcquisitionWindow, apply_acquisition_window
from sports_analytics.core.exceptions import ParserError, PermanentSourceError
from sports_analytics.sources.betano.catalog import (
    ADAPTER_VERSION,
    BETANO_CATALOG,
    PARSER_VERSION,
    PROVIDER_ID,
)
from sports_analytics.sources.bookmaker_capture import attach_capture_references
from sports_analytics.sources.bookmaker_catalog import reject_forbidden_job_controls
from sports_analytics.sources.bookmaker_contracts import ProviderAcquisitionBundle
from sports_analytics.sources.bookmaker_extraction.adapter_contract import load_json_payload
from sports_analytics.sources.bookmaker_extraction.betano_topeventsv2 import looks_like_topeventsv2
from sports_analytics.sources.bookmaker_extraction.contracts import ExtractionProfile
from sports_analytics.sources.bookmaker_extraction.pipeline import apply_extraction_profile
from sports_analytics.sources.bookmaker_extraction.registry import (
    get_stage_b_navigation_capability,
    get_verified_extraction_profile,
)
from sports_analytics.sources.bookmaker_extraction.stage_b import (
    execute_optional_stage_b,
    merge_stage_a_and_b_results,
)
from sports_analytics.sources.browser.contracts import BrowserAcquisitionResult, BrowserMode
from sports_analytics.sources.browser.playwright_runtime import (
    BrowserSession,
    PlaywrightBrowserSession,
)
from sports_analytics.sources.raw_capture import BookmakerRawCapture, BookmakerRawCaptureStore


def acquire_betano_current_odds(
    *,
    sport: str,
    acquisition_cycle_id: str,
    observed_at_utc: datetime,
    raw_directory: Path,
    browser_mode: BrowserMode = BrowserMode.HEADLESS,
    session: BrowserSession | None = None,
    maximum_capture_bytes: int = 2_097_152,
    extraction_profile: ExtractionProfile | None = None,
    deadline_at_utc: datetime | None = None,
    acquisition_window: AcquisitionWindow | None = None,
    stage_b_capability: StageBNavigationCapability | None = None,
    stage_b_executor: NavigationPlanExecutor | None = None,
) -> tuple[BrowserAcquisitionResult, ProviderAcquisitionBundle, tuple[BookmakerRawCapture, ...]]:
    """Acquire Betano pre-match fixtures/odds via ordinary browser automation.

    Evidence timestamp policy
    -------------------------
    Each raw capture uses the corresponding ``BrowserResponseObservation.observed_at_utc``.
    The acquisition bundle uses the latest timestamp among responses that contributed a
    recognized ``data.topEventsV2`` payload. When no such response exists, the cycle
    start ``observed_at_utc`` is retained only as a fallback for empty cycles.
    ``scheduled_for_utc`` is never used as a quote observation timestamp.
    """
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
        deadline_at_utc=deadline_at_utc,
    )
    store = BookmakerRawCaptureStore(raw_directory)
    captures: list[BookmakerRawCapture] = []
    recognized_response_times: list[datetime] = []
    for response in result.responses:
        artifact = store.store_text(
            source_name=PROVIDER_ID,
            capture_kind="provider-json",
            content=response.body_text,
            retrieved_at=response.observed_at_utc,
            extension="json",
            maximum_bytes=maximum_capture_bytes,
            # Complete provider URLs never enter raw-capture metadata.
            source_url=None,
        )
        captures.append(artifact)
        try:
            payload = load_json_payload(response.body_text)
        except ParserError:
            continue
        if looks_like_topeventsv2(payload):
            recognized_response_times.append(response.observed_at_utc)
    evidence_observed_at = (
        max(recognized_response_times) if recognized_response_times else observed_at_utc
    )
    profile = (
        extraction_profile
        if extraction_profile is not None
        else get_verified_extraction_profile(PROVIDER_ID, sport)
    )
    bundle = apply_extraction_profile(
        profile=profile,
        browser_result=result,
        captures=tuple(captures),
        adapter_version=ADAPTER_VERSION,
        parser_version=PARSER_VERSION,
        observed_at_utc=evidence_observed_at,
        sport=sport,
    )
    capability = stage_b_capability or get_stage_b_navigation_capability(PROVIDER_ID, sport)
    stage_b_result = execute_optional_stage_b(
        capability=capability,
        stage_a_result=result,
        stage_a_bundle=bundle,
        acquisition_window=acquisition_window,
        executor=stage_b_executor,
    )
    if stage_b_result is not None:
        for response in stage_b_result.responses:
            captures.append(
                store.store_text(
                    source_name=PROVIDER_ID,
                    capture_kind="provider-json",
                    content=response.body_text,
                    retrieved_at=response.observed_at_utc,
                    extension="json",
                    maximum_bytes=maximum_capture_bytes,
                    source_url=None,
                )
            )
            try:
                payload = load_json_payload(response.body_text)
            except ParserError:
                continue
            if looks_like_topeventsv2(payload):
                recognized_response_times.append(response.observed_at_utc)
        result = merge_stage_a_and_b_results(result, stage_b_result)
        evidence_observed_at = (
            max(recognized_response_times) if recognized_response_times else observed_at_utc
        )
        bundle = apply_extraction_profile(
            profile=profile,
            browser_result=result,
            captures=tuple(captures),
            adapter_version=ADAPTER_VERSION,
            parser_version=PARSER_VERSION,
            observed_at_utc=evidence_observed_at,
            sport=sport,
        )
    if acquisition_window is not None:
        bundle = apply_acquisition_window(bundle, acquisition_window)
        assert isinstance(bundle, ProviderAcquisitionBundle)
    bundle = attach_capture_references(bundle, tuple(captures))
    assert isinstance(bundle, ProviderAcquisitionBundle)
    return result, bundle, tuple(captures)


def reject_arbitrary_betano_controls(payload: dict[str, object]) -> None:
    """Reject forbidden payload controls for Betano jobs."""
    reject_forbidden_job_controls(payload)
    if payload.get("provider_id") not in (None, PROVIDER_ID):
        msg = f"provider_id must be {PROVIDER_ID}"
        raise PermanentSourceError(msg)
