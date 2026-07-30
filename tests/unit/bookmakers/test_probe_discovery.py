"""Probe clock, observation kwargs, and DOM discovery tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sports_analytics.bookmakers.diagnostics.dom_discovery import (
    canonicalize_structural_markers,
    discover_dom_candidates,
)
from sports_analytics.bookmakers.diagnostics.probe import (
    collect_probe_from_acquisition,
    probe_bookmaker,
)
from sports_analytics.sources.browser.contracts import (
    BrowserAcquisitionResult,
    BrowserMode,
    BrowserNetworkMetadata,
    BrowserResponseObservation,
)
from sports_analytics.sources.browser.playwright_runtime import (
    RecordingBrowserSession,
    build_structural_page_observation,
    sanitized_path_hash,
)

NOW = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)


def test_attribute_values_reduce_to_fixed_canonical_markers() -> None:
    assert canonicalize_structural_markers("event-card") == ("card", "event")
    assert canonicalize_structural_markers("market-John-Doe") == ("market",)
    assert canonicalize_structural_markers("a1b2c3d4e5f67890", "12345") == ()


def test_discover_dom_candidates_finds_market_nodes() -> None:
    html = """
    <div class="event-row css-a1b2c3d4">
      <span data-testid="market-odds" aria-label="Home odds">1.85</span>
      <button class="price-button 99887766">Bet</button>
    </div>
    """
    candidates = discover_dom_candidates(html)
    assert candidates
    assert any({"market", "odds"} <= set(item.structural_markers) for item in candidates)
    assert all("a1b2c3d4" not in item.structural_markers for item in candidates)
    assert all(
        item.decimal_odds_text is None or len(item.decimal_odds_text) <= 6 for item in candidates
    )
    assert all(not hasattr(item, "short_text") for item in candidates)
    assert all(not hasattr(item, "aria_label") for item in candidates)


def test_decimal_odds_discovers_bounded_parent_and_excludes_scripts_and_account() -> None:
    html = """
    <script data-testid="event-data">const fake = "1.90";</script>
    <section class="account-panel"><span class="odds">2.20</span></section>
    <article class="match-card">
      <div><button aria-label="Synthetic public price">1.95</button></div>
    </article>
    """
    candidates = discover_dom_candidates(html)
    assert any(item.tag == "button" and item.decimal_odds_text == "1.95" for item in candidates)
    assert any(
        item.tag == "article" and {"card", "match"} <= set(item.structural_markers)
        for item in candidates
    )
    assert all(item.tag != "script" for item in candidates)
    assert all(item.decimal_odds_text != "2.20" for item in candidates)
    assert all(len(item.ancestor_structural_fingerprint) == 64 for item in candidates)


def test_structured_public_hydration_is_structural_only() -> None:
    candidates = discover_dom_candidates(
        '<script type="application/json" id="ng-transfer-state">'
        '{"publicEvents":[{"fake":true}]}</script>'
        '<script type="application/json">{"generic":true}</script>'
    )
    scripts = [item for item in candidates if item.tag == "script"]
    assert len(scripts) == 1
    assert scripts[0].candidate_classification == "hydration-structure"
    assert scripts[0].hydration_marker == "ng-state"
    assert scripts[0].structural_markers == ()
    assert scripts[0].content_shape_fingerprint is not None
    assert scripts[0].decimal_odds_text is None


def test_excluded_descendant_text_never_propagates_to_public_parent() -> None:
    excluded_values = (
        "Private account details",
        "fake.person@example.test",
        "FAKE_ACCOUNT_NAME",
        "FAKE_PRIVATE_VALUE",
    )
    candidates = discover_dom_candidates(
        """
        <article class="market-card">
          Public Match
          <section class="account-panel">
            Private account details
            <span>fake.person@example.test</span>
            <input value="FAKE_PRIVATE_VALUE" />
            <div>FAKE_ACCOUNT_NAME</div>
          </section>
          <button class="price-button">1.95</button>
        </article>
        """
    )
    assert candidates
    serialized = json.dumps([candidate.as_dict() for candidate in candidates])
    for excluded in excluded_values:
        assert excluded not in serialized


def test_probe_passes_observation_window_and_advancing_clock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)

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
    output = tmp_path / "diagnostics"
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
    assert payload["transport_summary"]["response_metadata_count"] == 1
    assert payload["transport_summary"]["resource_type_counts"] == {"image": 1}
    assert payload["transport_summary"]["captured_body_count"] == 0
    assert payload["transport_summary"]["approved_host_counts"] == []


def test_approved_hostname_counts_use_safe_records_not_mapping_keys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    acquisition = BrowserAcquisitionResult(
        provider_id="betclic-pt",
        sport="football",
        acquisition_cycle_id="probe-approved-host",
        observed_at_utc=NOW,
        browser_mode=BrowserMode.HEADLESS,
        pages=(),
        responses=(),
        diagnostics=(),
        block_reason=None,
        warnings=(),
        network_metadata=(
            BrowserNetworkMetadata(
                hostname="www.betclic.pt",
                resource_type="document",
                status_code=200,
                content_type="text/html",
                byte_size=12,
                sanitized_path_hash="a" * 64,
                structural_fingerprint=None,
                hostname_approved=True,
                candidate_keys_detected=False,
                body_captured=False,
                observed_at_utc=NOW,
            ),
        ),
    )
    result = collect_probe_from_acquisition(
        provider_id="betclic-pt",
        sport="football",
        acquisition=acquisition,
        duration_seconds=1.0,
        diagnostic_directory="diagnostics",
    )
    payload = json.loads(
        (tmp_path / "diagnostics" / result.diagnostic_relative_path).read_text(encoding="utf-8")
    )
    assert payload["transport_summary"]["approved_host_counts"] == [
        {"hostname": "www.betclic.pt", "count": 1}
    ]


def test_collect_probe_persists_structural_candidates_without_dom_preview(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
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
            build_structural_page_observation(
                provider_id="betano-pt",
                page_route_id="football-prematch",
                final_url="https://www.betano.pt/sport/futebol/",
                observed_at_utc=NOW,
                allowed_hostnames=frozenset({"www.betano.pt"}),
                title="Futebol",
                body_html=html,
                body_text="synthetic public body",
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
    output = tmp_path / "diagnostics"
    result = collect_probe_from_acquisition(
        provider_id="betano-pt",
        sport="football",
        acquisition=acquisition,
        duration_seconds=1.0,
        diagnostic_directory=output,
    )
    assert result.pages[0].dom_candidates
    assert "dom_preview" not in result.pages[0].sanitized_sample
    assert result.pages[0].sanitized_sample == {
        "dom_candidates": list(result.pages[0].dom_candidates)
    }
