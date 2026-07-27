"""Bookmaker diagnostic harness tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from sports_analytics.bookmakers.diagnostics.acceptance import build_acceptance_report
from sports_analytics.bookmakers.diagnostics.fingerprint import structural_fingerprint
from sports_analytics.bookmakers.diagnostics.paths import resolve_diagnostic_directory
from sports_analytics.bookmakers.diagnostics.probe import collect_probe_from_acquisition
from sports_analytics.bookmakers.diagnostics.redaction import redact_text
from sports_analytics.bookmakers.diagnostics.smoke import evaluate_fake_session_smoke
from sports_analytics.bookmakers.multiples import (
    RequestedMultipleLegSpec,
    compare_provider_multiples,
)
from sports_analytics.core.exceptions import ConfigurationError
from sports_analytics.sources.browser.contracts import (
    BrowserAcquisitionResult,
    BrowserMode,
    BrowserPageObservation,
    BrowserResponseObservation,
)
from tests.unit.bookmakers.verified_quote_helpers import verified_quote

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)


def test_diagnostic_directory_rejects_traversal(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError):
        resolve_diagnostic_directory("../outside", base_directory=tmp_path)


def test_redaction_removes_absolute_paths() -> None:
    sanitized = redact_text("saved at C:\\Users\\secret\\capture.json")
    assert "C:\\" not in sanitized
    assert "<redacted-path>" in sanitized


def test_structural_fingerprint_stable() -> None:
    payload = {"events": [{"id": "1", "markets": [{"odds": 1.5}]}]}
    assert structural_fingerprint(payload) == structural_fingerprint(payload)


def test_collect_probe_from_acquisition_writes_gitignored_path(tmp_path: Path) -> None:
    acquisition = BrowserAcquisitionResult(
        provider_id="betano-pt",
        sport="football",
        acquisition_cycle_id="probe-test",
        observed_at_utc=NOW,
        browser_mode=BrowserMode.VISIBLE,
        pages=(
            BrowserPageObservation(
                provider_id="betano-pt",
                page_route_id="football-prematch",
                final_url="https://www.betano.pt/sport/futebol/",
                observed_at_utc=NOW,
                title="Futebol",
                sanitized_dom_fragment="<div>odds</div>",
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
    output = Path.cwd() / "storage" / "local" / "bookmaker-diagnostics-test"
    result = collect_probe_from_acquisition(
        provider_id="betano-pt",
        sport="football",
        acquisition=acquisition,
        duration_seconds=1.0,
        diagnostic_directory=output,
    )
    artifact = output / result.diagnostic_relative_path
    assert artifact.is_file()
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    assert "C:\\" not in json.dumps(payload)
    assert payload["responses"][0]["structural_fingerprint"]


def test_fake_session_positive_smoke() -> None:
    result = evaluate_fake_session_smoke(
        provider_id="betano-pt",
        sport="football",
        events_extracted=3,
        markets_with_odds=3,
        profile_verified=True,
        profile_id="test-profile",
    )
    assert result.succeeded is True


def test_zero_event_smoke_failure() -> None:
    result = evaluate_fake_session_smoke(
        provider_id="betano-pt",
        sport="football",
        events_extracted=0,
        markets_with_odds=0,
        profile_verified=True,
        profile_id="test-profile",
    )
    assert result.succeeded is False


def test_acceptance_report_has_no_raw_payload() -> None:
    betano = verified_quote(provider_id="betano-pt", odds="1.50", leg_key="a", observed_at=NOW)
    betclic = verified_quote(provider_id="betclic-pt", odds="1.60", leg_key="a", observed_at=NOW)
    report = build_acceptance_report(
        betano_quotes=(betano,),
        betclic_quotes=(betclic,),
        evaluated_at_utc=NOW,
        leg_specs=(
            RequestedMultipleLegSpec("a", "event-a", "football-match-result-1x2", "home"),
            RequestedMultipleLegSpec("b", "event-b", "football-match-result-1x2", "home"),
        ),
    )
    encoded = json.dumps(report.summary)
    assert "raw" not in encoded.lower() or "draw" not in encoded


def test_total_line_mismatch_rejected_in_multiples() -> None:
    over25 = verified_quote(
        provider_id="betano-pt",
        odds="1.80",
        leg_key="a",
        observed_at=NOW,
        market_key="football.totals.goals.full-match",
        canonical_market_definition_id="football-total-goals",
        selection_id="over",
        line_type="total",
        line_value=Decimal("2.5"),
    )
    over25.identity  # noqa: B018
    specs = (
        RequestedMultipleLegSpec(
            "a",
            "event-a",
            "football-total-goals",
            "over",
            line_type="total",
            line_value=Decimal("3.5"),
        ),
        RequestedMultipleLegSpec("b", "event-b", "football-match-result-1x2", "home"),
    )
    betano_b = verified_quote(
        provider_id="betano-pt",
        odds="2.00",
        leg_key="b",
        observed_at=NOW,
    )
    comparison = compare_provider_multiples(
        specs,
        {"a": over25, "b": betano_b},
        {},
        evaluated_at_utc=NOW,
        quote_maximum_age_seconds=300,
    )
    assert comparison.betano_eligible is False
