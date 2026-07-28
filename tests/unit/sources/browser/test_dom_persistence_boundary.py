"""End-to-end structural-only DOM persistence boundary tests."""

from __future__ import annotations

import json
from dataclasses import asdict, fields
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sports_analytics.bookmakers.diagnostics.probe import collect_probe_from_acquisition
from sports_analytics.core.exceptions import PermanentSourceError
from sports_analytics.sources.betano.adapter import acquire_betano_current_odds
from sports_analytics.sources.betclic.adapter import acquire_betclic_current_odds
from sports_analytics.sources.bookmaker_capture import build_capture_manifest
from sports_analytics.sources.bookmaker_extraction.registry import get_verified_extraction_profile
from sports_analytics.sources.browser.contracts import (
    BrowserAcquisitionResult,
    BrowserBlockReason,
    BrowserDomCandidate,
    BrowserMode,
    BrowserPageObservation,
    BrowserResponseObservation,
)
from sports_analytics.sources.browser.playwright_runtime import (
    RecordingBrowserSession,
    build_structural_page_observation,
)

NOW = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
BETANO_FIXTURE = (
    Path(__file__).resolve().parents[3]
    / "fixtures"
    / "betano"
    / "topeventsv2_football_sanitized.json"
)
PRIVATE_VALUES = (
    "FAKE_PRIVATE_JAVASCRIPT",
    "FAKE_HYDRATION_PERSON",
    "FAKE_PERSONAL_NAME",
    "FAKE_ACCOUNT_NAME",
    "fake.private@example.test",
    "+351-000-000-000",
    "FAKE PRIVATE ADDRESS",
    "FAKE_PAYMENT_4111111111111111",
    "FAKE_SECRET_TOKEN_VALUE",
    "market-John-Doe",
    "John-Doe",
    "event-TeamA-TeamB",
    "TeamA-TeamB",
    "Synthetic Event: TeamA vs TeamB",
    "TeamA",
    "TeamB",
    "price-card-4111111111111111",
    "4111111111111111",
    "market-profile-Alice",
    "profile-Alice",
    "Alice",
    "selection-user-12345",
    "user-12345",
    "price-FAKE_ACCOUNT_NAME",
    "hydration-JohnDoe",
    "JohnDoe",
)


def _synthetic_html() -> str:
    return """
    <main>
      <article class="market-card">
        Public synthetic match
        <div class="market-John-Doe event-TeamA-TeamB price-card-4111111111111111">
          Synthetic Event: TeamA vs TeamB
        </div>
        <div id="market-profile-Alice">Synthetic public market</div>
        <div data-testid="selection-user-12345">Synthetic public selection</div>
        <div data-qa="price-FAKE_ACCOUNT_NAME">Excluded synthetic account marker</div>
        <button class="price-button" data-testid="market-odds">1.95</button>
        <script>const privateData = "FAKE_PRIVATE_JAVASCRIPT";</script>
        <script type="application/json" id="hydration-JohnDoe">
          {
            "person": "FAKE_HYDRATION_PERSON",
            "email": "fake.private@example.test",
            "token": "FAKE_SECRET_TOKEN_VALUE"
          }
        </script>
        <section class="account-panel">FAKE_ACCOUNT_NAME</section>
        <section class="login-panel">FAKE_PERSONAL_NAME</section>
        <section class="registration-panel">+351-000-000-000</section>
        <section class="deposit-panel">FAKE_PAYMENT_4111111111111111</section>
        <section class="withdrawal-panel">FAKE PRIVATE ADDRESS</section>
        <section class="payment-panel">FAKE_PAYMENT_4111111111111111</section>
        <section class="betslip-panel">FAKE_SECRET_TOKEN_VALUE</section>
        <form class="market-form">
          <input value="FAKE_PERSONAL_NAME" />
          <textarea>FAKE PRIVATE ADDRESS</textarea>
        </form>
      </article>
    </main>
    """


def _page(provider_id: str, hostname: str, final_url: str) -> BrowserPageObservation:
    return build_structural_page_observation(
        provider_id=provider_id,
        page_route_id="football-prematch",
        final_url=final_url,
        observed_at_utc=NOW,
        allowed_hostnames=frozenset({hostname}),
        title="FAKE_PERSONAL_NAME",
        body_html=_synthetic_html(),
        body_text="FAKE_ACCOUNT_NAME fake.private@example.test",
    )


def _assert_private_values_absent(value: object) -> None:
    encoded = json.dumps(value, default=str, sort_keys=True)
    for private in PRIVATE_VALUES:
        assert private not in encoded


def test_raw_page_material_never_crosses_persistable_boundaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.chdir(tmp_path)
    betano_page = _page(
        "betano-pt",
        "www.betano.pt",
        "https://www.betano.pt/sport/futebol/",
    )
    betclic_page = _page(
        "betclic-pt",
        "www.betclic.pt",
        "https://www.betclic.pt/futebol",
    )

    page_field_names = {item.name for item in fields(BrowserPageObservation)}
    assert page_field_names.isdisjoint(
        {"title", "body_html", "body_text", "sanitized_dom_fragment", "dom_preview"}
    )
    assert betano_page.structural_candidates
    assert any(
        item.decimal_odds_text == "1.95" and item.candidate_classification == "decimal-odds"
        for item in betano_page.structural_candidates
    )
    observed_markers = {
        marker for item in betano_page.structural_candidates for marker in item.structural_markers
    }
    assert {"card", "event", "market", "price", "selection"} <= observed_markers
    assert all(len(item.structural_fingerprint) == 64 for item in betano_page.structural_candidates)
    hydration = [
        item
        for item in betano_page.structural_candidates
        if item.candidate_classification == "hydration-structure"
    ]
    assert len(hydration) == 1
    assert hydration[0].hydration_marker == "hydration"
    assert hydration[0].structural_markers == ()
    assert hydration[0].content_shape_fingerprint is not None
    assert all(
        item.tag != "script" for item in betano_page.structural_candidates if item not in hydration
    )
    _assert_private_values_absent(asdict(betano_page))
    _assert_private_values_absent(asdict(betclic_page))

    betano_body = BETANO_FIXTURE.read_text(encoding="utf-8")
    betano_acquisition = BrowserAcquisitionResult(
        provider_id="betano-pt",
        sport="football",
        acquisition_cycle_id="cycle-structural-betano",
        observed_at_utc=NOW,
        browser_mode=BrowserMode.VISIBLE,
        pages=(betano_page,),
        responses=(
            BrowserResponseObservation(
                provider_id="betano-pt",
                page_route_id="football-prematch",
                response_url="https://www.betano.pt/api/synthetic/topevents",
                observed_at_utc=NOW,
                content_type="application/json",
                body_text=betano_body,
                status_code=200,
                warnings=(),
            ),
        ),
        diagnostics=(),
        block_reason=None,
        warnings=(),
    )
    betclic_acquisition = BrowserAcquisitionResult(
        provider_id="betclic-pt",
        sport="football",
        acquisition_cycle_id="cycle-structural-betclic",
        observed_at_utc=NOW,
        browser_mode=BrowserMode.VISIBLE,
        pages=(betclic_page,),
        responses=(),
        diagnostics=(),
        block_reason=None,
        warnings=(),
    )
    _assert_private_values_absent(asdict(betano_acquisition))
    _assert_private_values_absent(asdict(betclic_acquisition))

    probe = collect_probe_from_acquisition(
        provider_id="betano-pt",
        sport="football",
        acquisition=betano_acquisition,
        duration_seconds=1.0,
        diagnostic_directory=tmp_path / "diagnostics",
    )
    probe_path = tmp_path / "diagnostics" / probe.diagnostic_relative_path
    probe_text = probe_path.read_text(encoding="utf-8")
    _assert_private_values_absent(asdict(probe))
    _assert_private_values_absent(json.loads(probe_text))
    assert "dom_preview" not in probe_text
    assert "sanitized_dom_fragment" not in probe_text
    assert "structural_markers" in probe_text
    assert "hydration_marker" in probe_text
    assert "class_tokens" not in probe_text
    assert "structural_id" not in probe_text
    assert "data_testid" not in probe_text
    assert "data_qa" not in probe_text

    _, betano_bundle, betano_captures = acquire_betano_current_odds(
        sport="football",
        acquisition_cycle_id=betano_acquisition.acquisition_cycle_id,
        observed_at_utc=NOW,
        raw_directory=tmp_path / "raw-betano",
        session=RecordingBrowserSession(betano_acquisition),
    )
    _, betclic_bundle, betclic_captures = acquire_betclic_current_odds(
        sport="football",
        acquisition_cycle_id=betclic_acquisition.acquisition_cycle_id,
        observed_at_utc=NOW,
        raw_directory=tmp_path / "raw-betclic",
        session=RecordingBrowserSession(betclic_acquisition),
    )
    assert betano_bundle.events
    assert [item.capture_kind for item in betano_captures] == ["provider-json"]
    assert betclic_captures == ()
    assert betclic_bundle.events == ()

    for raw_root, captures in (
        (tmp_path / "raw-betano", betano_captures),
        (tmp_path / "raw-betclic", betclic_captures),
    ):
        for capture in captures:
            capture_text = (raw_root / capture.relative_path).read_text(encoding="utf-8")
            _assert_private_values_absent(capture_text)
        manifest = build_capture_manifest(
            provider_id=("betano-pt" if raw_root.name == "raw-betano" else "betclic-pt"),
            acquisition_cycle_id="cycle-structural-manifest",
            captures=captures,
        )
        _assert_private_values_absent(json.loads(manifest.manifest_bytes))
        assert b"dom-fragment" not in manifest.manifest_bytes

    _assert_private_values_absent(asdict(betano_bundle))
    _assert_private_values_absent(asdict(betclic_bundle))
    _assert_private_values_absent(caplog.text)
    assert get_verified_extraction_profile("betclic-pt") is None


def test_block_signals_are_classified_before_persistable_page_boundary() -> None:
    page = build_structural_page_observation(
        provider_id="betano-pt",
        page_route_id="football-prematch",
        final_url="https://www.betano.pt/sport/futebol/",
        observed_at_utc=NOW,
        allowed_hostnames=frozenset({"www.betano.pt"}),
        title="Access denied for FAKE_PERSONAL_NAME",
        body_html="<main>FAKE_ACCOUNT_NAME</main>",
        body_text="Please complete the captcha for fake.private@example.test",
    )
    assert page.block_reason in {
        BrowserBlockReason.ACCESS_DENIED,
        BrowserBlockReason.CAPTCHA,
    }
    _assert_private_values_absent(asdict(page))


def test_structural_contract_rejects_arbitrary_identifier_text() -> None:
    candidate_kwargs = {
        "tag": "div",
        "structural_markers": ("market",),
        "hydration_marker": None,
        "child_count": 0,
        "candidate_classification": "structural-interest",
        "decimal_odds_text": None,
        "structural_fingerprint": "a" * 64,
        "ancestor_structural_fingerprint": "b" * 64,
    }
    invalid_overrides = (
        {"structural_markers": ("FAKE_PERSONAL_NAME",)},
        {"structural_markers": ("market", "card")},
        {"structural_markers": ("market", "market")},
        {"hydration_marker": "hydration-JohnDoe"},
        {"hydration_marker": "hydration"},
        {"tag": "script"},
    )
    for overrides in invalid_overrides:
        with pytest.raises(PermanentSourceError) as exc_info:
            BrowserDomCandidate(**(candidate_kwargs | overrides))
        _assert_private_values_absent(str(exc_info.value))


def test_hydration_contract_is_script_only_and_allowlisted() -> None:
    candidate = BrowserDomCandidate(
        tag="script",
        structural_markers=(),
        hydration_marker="hydration",
        child_count=0,
        candidate_classification="hydration-structure",
        decimal_odds_text=None,
        structural_fingerprint="a" * 64,
        ancestor_structural_fingerprint="b" * 64,
        content_shape_fingerprint="c" * 64,
    )
    assert candidate.hydration_marker == "hydration"
    with pytest.raises(PermanentSourceError) as non_script_error:
        BrowserDomCandidate(**(asdict(candidate) | {"tag": "div"}))
    _assert_private_values_absent(str(non_script_error.value))
    with pytest.raises(PermanentSourceError) as marker_error:
        BrowserDomCandidate(**(asdict(candidate) | {"hydration_marker": "hydration-JohnDoe"}))
    _assert_private_values_absent(str(marker_error.value))
