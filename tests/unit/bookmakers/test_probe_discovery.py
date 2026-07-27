"""Probe clock, observation kwargs, and DOM discovery tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sports_analytics.bookmakers.diagnostics.dom_discovery import (
    discover_dom_candidates,
    sanitize_class_token,
)
from sports_analytics.bookmakers.diagnostics.probe import (
    collect_probe_from_acquisition,
    probe_bookmaker,
)
from sports_analytics.sources.browser.contracts import (
    BrowserAcquisitionResult,
    BrowserMode,
    BrowserNetworkMetadata,
    BrowserPageObservation,
    BrowserResponseObservation,
)
from sports_analytics.sources.browser.playwright_runtime import (
    RecordingBrowserSession,
    sanitized_path_hash,
)

NOW = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)


def test_sanitize_class_token_strips_hex_and_long_numeric() -> None:
    assert sanitize_class_token("event-card") == "event-card"
    assert sanitize_class_token("a1b2c3d4e5f67890") is None
    assert sanitize_class_token("12345") is None


def test_discover_dom_candidates_finds_market_nodes() -> None:
    html = """
    <div class="event-row css-a1b2c3d4">
      <span data-testid="market-odds" aria-label="Home odds">1.85</span>
      <button class="price-button 99887766">Bet</button>
    </div>
    """
    candidates = discover_dom_candidates(html)
    assert candidates
    assert any(item.data_testid == "market-odds" for item in candidates)
    assert all("a1b2c3d4" not in item.class_tokens for item in candidates)
    assert all(len(item.short_text or "") <= 80 for item in candidates)


def test_probe_passes_observation_window_and_advancing_clock() -> None:
    class AdvancingClock:
        def __init__(self) -> None:
            self.now = NOW
            self.calls = 0

        def __call__(self) -> datetime:
            self.calls += 1
            # Advance slightly on each call so deadline math progresses.
            current = self.now
            self.now = self.now + timedelta(milliseconds=50)
            return current

    acquisition = BrowserAcquisitionResult(
        provider_id="betclic-pt",
        sport="football",
        acquisition_cycle_id="probe-test",
        observed_at_utc=NOW,
        browser_mode=BrowserMode.VISIBLE,
        pages=(),
        responses=(),
        diagnostics=(),
        block_reason=None,
        warnings=(),
        network_metadata=(
            BrowserNetworkMetadata(
                response_url="https://cdn.example.com/pixel",
                hostname="cdn.example.com",
                resource_type="image",
                status_code=200,
                content_type="image/png",
                byte_size=12,
                sanitized_path_hash=sanitized_path_hash("https://cdn.example.com/pixel"),
                structural_fingerprint=None,
                hostname_approved=False,
                candidate_keys_detected=False,
                body_captured=False,
                observed_at_utc=NOW,
            ),
        ),
    )
    session = RecordingBrowserSession(acquisition)
    clock = AdvancingClock()
    output = Path.cwd() / "storage" / "local" / "bookmaker-diagnostics-probe-test"
    output.mkdir(parents=True, exist_ok=True)
    result = probe_bookmaker(
        provider_id="betclic-pt",
        sport="football",
        duration_seconds=12,
        diagnostic_directory=output,
        session=session,
        clock=clock,
    )
    assert clock.calls >= 1
    assert session.calls[0]["observation_window_ms"] == 12_000
    assert session.calls[0]["deadline_at_utc"] is not None
    assert session.calls[0]["observation_complete"] is not None
    assert result.network_metadata
    assert result.network_metadata[0]["body_captured"] is False
    artifact = output / result.diagnostic_relative_path
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    assert "network_metadata" in payload
    assert payload["network_metadata"][0]["hostname_approved"] is False


def test_collect_probe_expands_dom_preview_and_candidates() -> None:
    html = (
        '<section class="event-list">'
        + ("x" * 500)
        + '<div data-qa="price-cell" class="odds-value">2.10</div></section>'
    )
    acquisition = BrowserAcquisitionResult(
        provider_id="betano-pt",
        sport="football",
        acquisition_cycle_id="probe-dom",
        observed_at_utc=NOW,
        browser_mode=BrowserMode.VISIBLE,
        pages=(
            BrowserPageObservation(
                provider_id="betano-pt",
                page_route_id="football-prematch",
                final_url="https://www.betano.pt/sport/futebol/",
                observed_at_utc=NOW,
                title="Futebol",
                sanitized_dom_fragment=html,
                block_reason=None,
                warnings=(),
            ),
        ),
        responses=(
            BrowserResponseObservation(
                provider_id="betano-pt",
                page_route_id="football-prematch",
                response_url="https://www.betano.pt/api/events",
                observed_at_utc=NOW,
                content_type="application/json",
                body_text='{"events":[{"id":"1"}]}',
                status_code=200,
                warnings=(),
            ),
        ),
        diagnostics=(),
        block_reason=None,
        warnings=(),
    )
    output = Path.cwd() / "storage" / "local" / "bookmaker-diagnostics-probe-dom"
    output.mkdir(parents=True, exist_ok=True)
    result = collect_probe_from_acquisition(
        provider_id="betano-pt",
        sport="football",
        acquisition=acquisition,
        duration_seconds=1.0,
        diagnostic_directory=output,
    )
    assert result.pages[0].dom_candidates
    preview = result.pages[0].sanitized_sample["dom_preview"]
    assert len(preview) > 200
    assert len(preview) <= 2000
