"""Bookmaker diagnostic harness tests."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from sports_analytics.bookmakers.diagnostics.acceptance import build_acceptance_report
from sports_analytics.bookmakers.diagnostics.fingerprint import structural_fingerprint
from sports_analytics.bookmakers.diagnostics.paths import resolve_diagnostic_directory
from sports_analytics.bookmakers.diagnostics.probe import collect_probe_from_acquisition
from sports_analytics.bookmakers.diagnostics.redaction import redact_text
from sports_analytics.bookmakers.diagnostics.smoke import (
    SmokeResult,
    evaluate_fake_session_smoke,
    smoke_bookmaker,
)
from sports_analytics.bookmakers.multiples import (
    RequestedMultipleLegSpec,
    compare_provider_multiples,
)
from sports_analytics.bookmakers.types import BookmakerIngestionResult
from sports_analytics.core.exceptions import ConfigurationError
from sports_analytics.sources.browser.contracts import (
    BrowserAcquisitionResult,
    BrowserMode,
    BrowserResponseObservation,
)
from sports_analytics.sources.browser.playwright_runtime import (
    build_structural_page_observation,
)
from tests.unit.bookmakers.verified_quote_helpers import (
    catalogue_for,
    empty_catalogue,
    verified_quote,
)

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
PRIVATE_SMOKE_VALUES = (
    "Synthetic Event Alpha",
    "Synthetic Team Beta",
    "FAKE_ACCOUNT_NAME",
    "FAKE_SECRET_TOKEN_VALUE",
    "https://private.example.test/path?token=fake",
)


def _ingestion_result(
    *,
    status: str,
    responses: int,
    recognized: int,
    events: int,
    quotes: int,
    snapshot_id: str | None = None,
    block_reason: str | None = None,
) -> BookmakerIngestionResult:
    return BookmakerIngestionResult(
        provider_id="betano-pt",
        sport="football",
        acquisition_cycle_id="smoke-betano-pt-football-1",
        adapter_version="betano-browser-v1",
        status=status,
        observed_at_utc="2026-07-26T12:00:00Z",
        snapshot_id=snapshot_id,
        snapshot_reused=False,
        block_reason=block_reason,
        failure_classification="fixed-test-classification",
        events_observed=events,
        valid_quotes_observed=quotes,
        unresolved_events=0,
        rejected_markets=0,
        warnings=PRIVATE_SMOKE_VALUES,
        drift_codes=("fixed-test-drift",),
        response_observation_count=responses,
        recognized_profile_response_count=recognized,
    )


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


def test_collect_probe_from_acquisition_writes_gitignored_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    acquisition = BrowserAcquisitionResult(
        provider_id="betano-pt",
        sport="football",
        acquisition_cycle_id="probe-test",
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
                body_html='<div class="odds-value">1.95</div>',
                body_text="Futebol",
            ),
        ),
        responses=(
            BrowserResponseObservation(
                provider_id="betano-pt",
                page_route_id="football-prematch",
                response_url="https://www.betano.pt/api/events",
                observed_at_utc=NOW,
                content_type="application/json",
                body_text=json.dumps(
                    {
                        "events": [
                            {
                                "id": "synthetic-id-123",
                                "name": "Synthetic Team Alpha",
                                "imageUrl": "https://private.example.test/team.png",
                            }
                        ],
                        "source_uri": "https://private.example.test/feed?token=fake",
                    }
                ),
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
    artifact = output / result.diagnostic_relative_path
    assert artifact.is_file()
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    assert "C:\\" not in json.dumps(payload)
    assert payload["responses"][0]["structural_fingerprint"]
    assert payload["responses"][0]["sanitized_sample"] == {
        "root_kind": "object",
        "top_level_key_count": 2,
        "sample_truncated": False,
    }
    encoded = json.dumps(payload, sort_keys=True)
    assert "Synthetic Team Alpha" not in encoded
    assert "synthetic-id-123" not in encoded
    assert "private.example.test" not in encoded


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
    assert result.acceptance_summary["second_cycle_proof"] == "deterministic-reuse"


def test_fake_session_smoke_refresh_proof() -> None:
    result = evaluate_fake_session_smoke(
        provider_id="betano-pt",
        sport="football",
        events_extracted=2,
        markets_with_odds=2,
        profile_verified=True,
        profile_id="test-profile",
        snapshot_reused_second_cycle=False,
    )
    assert result.succeeded is True
    assert result.acceptance_summary["second_cycle_proof"] == "refresh"
    assert result.cycles[1].snapshot_reused is False
    assert result.cycles[1].snapshot_id != result.cycles[0].snapshot_id


def test_fake_session_smoke_requires_per_event_quotes() -> None:
    result = evaluate_fake_session_smoke(
        provider_id="betano-pt",
        sport="football",
        events_extracted=2,
        markets_with_odds=2,
        valid_quote_count=1,
        profile_verified=True,
        profile_id="test-profile",
    )
    assert result.succeeded is False


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


def test_repeated_smoke_runs_use_distinct_acquisition_cycle_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    service = MagicMock()
    service.ingest.return_value = _ingestion_result(
        status="unavailable",
        responses=0,
        recognized=0,
        events=0,
        quotes=0,
    )
    output = tmp_path / "diagnostics"
    kwargs = {
        "provider_id": "betano-pt",
        "sport": "football",
        "duration_seconds": 5,
        "diagnostic_directory": output,
        "database_path": tmp_path / "operational.sqlite3",
        "raw_directory": tmp_path / "raw",
        "snapshots_directory": tmp_path / "snapshots",
        "session": MagicMock(),
        "extraction_profile": SimpleNamespace(
            profile_id="betano-pt-football-topeventsv2-v1",
            verified=True,
        ),
        "clock": lambda: NOW,
        "service": service,
    }

    smoke_bookmaker(**kwargs)
    smoke_bookmaker(**kwargs)

    first_id = service.ingest.call_args_list[0].kwargs["acquisition_cycle_id"]
    second_id = service.ingest.call_args_list[1].kwargs["acquisition_cycle_id"]

    assert first_id != second_id
    assert len(first_id) == 18
    assert len(second_id) == 18
    assert first_id.startswith("s")
    assert second_id.startswith("s")
    assert first_id.endswith("1")
    assert second_id.endswith("1")
    assert all(character in "0123456789abcdef" for character in first_id[1:17])
    assert all(character in "0123456789abcdef" for character in second_id[1:17])


def _assert_unknown_failure_telemetry_is_consistently_null(
    *,
    result: SmokeResult,
    persisted: dict[str, object],
) -> None:
    assert result.failure_telemetry is None
    acceptance_summary = result.acceptance_summary
    assert acceptance_summary["failure_telemetry"] is None
    assert persisted["failure_telemetry"] is None
    persisted_summary = persisted["acceptance_summary"]
    assert isinstance(persisted_summary, dict)
    assert persisted_summary["failure_telemetry"] is None


def test_no_verified_profile_keeps_unknown_failure_telemetry_null(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    output = tmp_path / "diagnostics"
    result = smoke_bookmaker(
        provider_id="betclic-pt",
        sport="football",
        duration_seconds=5,
        diagnostic_directory=output,
    )
    persisted = json.loads((output / result.diagnostic_relative_path).read_text(encoding="utf-8"))
    assert result.failure_reason == "no-verified-extraction-profile"
    _assert_unknown_failure_telemetry_is_consistently_null(
        result=result,
        persisted=persisted,
    )


def test_duration_deadline_keeps_unknown_failure_telemetry_null(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    times = iter((NOW, NOW.replace(second=2)))
    service = MagicMock()
    output = tmp_path / "diagnostics"
    result = smoke_bookmaker(
        provider_id="betano-pt",
        sport="football",
        duration_seconds=1,
        diagnostic_directory=output,
        database_path=tmp_path / "operational.sqlite3",
        raw_directory=tmp_path / "raw",
        snapshots_directory=tmp_path / "snapshots",
        session=MagicMock(),
        extraction_profile=SimpleNamespace(
            profile_id="betano-pt-football-topeventsv2-v1",
            verified=True,
        ),
        clock=lambda: next(times),
        service=service,
    )
    persisted = json.loads((output / result.diagnostic_relative_path).read_text(encoding="utf-8"))
    assert result.failure_reason == "duration-deadline-exceeded"
    service.ingest.assert_not_called()
    _assert_unknown_failure_telemetry_is_consistently_null(
        result=result,
        persisted=persisted,
    )


def test_unproven_second_cycle_keeps_failure_telemetry_null() -> None:
    result = evaluate_fake_session_smoke(
        provider_id="betano-pt",
        sport="football",
        events_extracted=2,
        markets_with_odds=2,
        profile_verified=True,
        profile_id="test-profile",
        snapshot_reused_second_cycle=None,
    )
    assert result.succeeded is False
    assert result.failure_reason == "second-cycle-refresh-or-reuse-unproven"
    assert result.failure_telemetry is None


@pytest.mark.parametrize(
    ("ingestion", "expected_stage"),
    [
        (
            _ingestion_result(
                status="unavailable",
                responses=0,
                recognized=0,
                events=0,
                quotes=0,
            ),
            "no-provider-response",
        ),
        (
            _ingestion_result(
                status="drift-detected",
                responses=1,
                recognized=0,
                events=0,
                quotes=0,
            ),
            "no-recognized-profile-response",
        ),
        (
            _ingestion_result(
                status="drift-detected",
                responses=1,
                recognized=1,
                events=0,
                quotes=0,
            ),
            "recognized-response-zero-events",
        ),
        (
            _ingestion_result(
                status="failed",
                responses=1,
                recognized=1,
                events=2,
                quotes=0,
            ),
            "parsed-events-zero-supported-quotes",
        ),
        (
            _ingestion_result(
                status="blocked",
                responses=1,
                recognized=0,
                events=0,
                quotes=0,
                block_reason="captcha",
            ),
            "blocked-acquisition",
        ),
        (
            _ingestion_result(
                status="failed",
                responses=1,
                recognized=1,
                events=2,
                quotes=3,
            ),
            "snapshot-admission-failure",
        ),
    ],
)
def test_failed_smoke_persists_only_fixed_stage_telemetry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ingestion: BookmakerIngestionResult,
    expected_stage: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    service = MagicMock()
    service.ingest.return_value = ingestion
    output = tmp_path / "diagnostics"
    result = smoke_bookmaker(
        provider_id="betano-pt",
        sport="football",
        duration_seconds=5,
        diagnostic_directory=output,
        database_path=tmp_path / "operational.sqlite3",
        raw_directory=tmp_path / "raw",
        snapshots_directory=tmp_path / "snapshots",
        session=MagicMock(),
        extraction_profile=SimpleNamespace(
            profile_id="betano-pt-football-topeventsv2-v1",
            verified=True,
        ),
        clock=lambda: NOW,
        service=service,
    )
    assert result.succeeded is False
    assert result.failure_telemetry is not None
    assert result.failure_telemetry.failure_stage == expected_stage
    assert result.failure_telemetry.response_observation_count == (
        ingestion.response_observation_count
    )
    assert result.failure_telemetry.recognized_profile_response_count == (
        ingestion.recognized_profile_response_count
    )
    persisted = json.loads((output / result.diagnostic_relative_path).read_text(encoding="utf-8"))
    assert persisted["failure_telemetry"]["failure_stage"] == expected_stage
    assert result.acceptance_summary["failure_telemetry"]["failure_stage"] == expected_stage
    assert persisted["acceptance_summary"]["failure_telemetry"]["failure_stage"] == expected_stage
    serialized = json.dumps(
        {"result": asdict(result), "persisted": persisted},
        sort_keys=True,
    )
    for private in PRIVATE_SMOKE_VALUES:
        assert private not in serialized


def test_smoke_exception_text_is_replaced_by_fixed_failure_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    service = MagicMock()
    service.ingest.side_effect = RuntimeError(
        "https://private.example.test/path?token=fake FAKE_SECRET_TOKEN_VALUE"
    )
    output = tmp_path / "diagnostics"
    result = smoke_bookmaker(
        provider_id="betano-pt",
        sport="football",
        duration_seconds=5,
        diagnostic_directory=output,
        database_path=tmp_path / "operational.sqlite3",
        raw_directory=tmp_path / "raw",
        snapshots_directory=tmp_path / "snapshots",
        session=MagicMock(),
        extraction_profile=SimpleNamespace(
            profile_id="betano-pt-football-topeventsv2-v1",
            verified=True,
        ),
        clock=lambda: NOW,
        service=service,
    )
    assert result.failure_reason == "acquisition-failed"
    assert result.failure_telemetry is None
    persisted_text = (output / result.diagnostic_relative_path).read_text(encoding="utf-8")
    assert "private.example.test" not in persisted_text
    assert "FAKE_SECRET_TOKEN_VALUE" not in persisted_text


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
        betano_catalogue=catalogue_for(over25, betano_b),
        betclic_catalogue=empty_catalogue(provider_id="betclic-pt"),
    )
    assert comparison.betano_eligible is False
