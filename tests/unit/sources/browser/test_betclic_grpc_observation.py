"""Safe Betclic gRPC-web response observation tests without a browser or network."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sports_analytics.bookmakers.diagnostics.persistence import UnsafeDiagnosticError
from sports_analytics.bookmakers.diagnostics.probe import collect_probe_from_acquisition
from sports_analytics.sources.browser.contracts import (
    BrowserAcquisitionResult,
    BrowserMode,
    BrowserPageObservation,
)
from sports_analytics.sources.browser.playwright_runtime import (
    build_network_metadata,
    observe_betclic_grpc_response,
)

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
POPULAR = (
    "https://offering.begmedia.com/web/offering.access.api/"
    "offering.access.api.MatchService/GetPopularV2"
)
LIVE_COUNT = (
    "https://offering.begmedia.com/web/offering.access.api/"
    "offering.access.api.MatchService/GetLiveCount"
)
ALLOWED = frozenset({"www.betclic.pt"})


def _frame(payload: bytes) -> bytes:
    return b"\x00" + len(payload).to_bytes(4, "big") + payload


class _Response:
    def __init__(self, body: bytes, *, error: Exception | None = None) -> None:
        self._body = body
        self._error = error
        self.body_calls = 0

    def body(self) -> bytes:
        self.body_calls += 1
        if self._error is not None:
            raise self._error
        return self._body


def _metadata(outcome, *, header_size: int | None = None):
    return build_network_metadata(
        response_url=POPULAR,
        allowed_hostnames=ALLOWED,
        status_code=200,
        content_type="application/grpc-web",
        resource_type="fetch",
        observed_at_utc=NOW,
        byte_size=(
            outcome.actual_byte_size if outcome.actual_byte_size is not None else header_size
        ),
        provider_id="betclic-pt",
        grpc_web_envelope_recognized=outcome.envelope_recognized,
        grpc_web_failure_code=outcome.failure_code,
        grpc_web_body_read=outcome.body_read,
        grpc_web_evidence_stored=outcome.evidence_stored,
        grpc_web_malformed_or_truncated=outcome.malformed_or_truncated,
    )


def test_absent_and_incorrect_content_length_use_actual_body_size(tmp_path: Path) -> None:
    body = _frame(b"PROTOBUF-FAKE")
    for content_length in (None, 1):
        response = _Response(body)
        outcome = observe_betclic_grpc_response(
            response=response,
            response_url=POPULAR,
            content_type="application/grpc-web",
            content_length=content_length,
            maximum_bytes=1024,
            diagnostic_directory=tmp_path,
        )
        assert response.body_calls == 1
        assert outcome.actual_byte_size == len(body)
        assert outcome.envelope_recognized is True
        assert outcome.body_read is True
        assert outcome.evidence_stored is True
        metadata = _metadata(outcome, header_size=content_length)
        assert metadata is not None
        assert metadata.byte_size == len(body)
        assert metadata.grpc_web_body_read is True
        assert metadata.grpc_web_evidence_stored is True


def test_body_inspection_and_storage_failures_preserve_safe_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read_failure = observe_betclic_grpc_response(
        response=_Response(b"", error=RuntimeError("external fake content")),
        response_url=POPULAR,
        content_type="application/grpc-web",
        content_length=None,
        maximum_bytes=1024,
        diagnostic_directory=tmp_path,
    )
    rejected = observe_betclic_grpc_response(
        response=_Response(b"\x00\x00"),
        response_url=POPULAR,
        content_type="application/grpc-web",
        content_length=None,
        maximum_bytes=1024,
        diagnostic_directory=tmp_path,
    )

    def fail_storage(*args, **kwargs):
        del args, kwargs
        raise OSError("external fake path")

    monkeypatch.setattr(
        "sports_analytics.sources.betclic.grpc_web.store_content_addressed_grpc_evidence",
        fail_storage,
    )
    storage_failure = observe_betclic_grpc_response(
        response=_Response(_frame(b"PROTOBUF-FAKE")),
        response_url=POPULAR,
        content_type="application/grpc-web",
        content_length=None,
        maximum_bytes=1024,
        diagnostic_directory=tmp_path,
    )
    assert read_failure.failure_code == "grpc-web-body-read-failed"
    assert read_failure.body_read is False
    assert rejected.failure_code == "truncated-header"
    assert rejected.body_read is True
    assert rejected.malformed_or_truncated is True
    assert storage_failure.failure_code == "grpc-web-evidence-storage-failed"
    assert storage_failure.envelope_recognized is True
    assert storage_failure.body_read is True
    assert storage_failure.evidence_stored is False
    for outcome in (read_failure, rejected, storage_failure):
        metadata = _metadata(outcome)
        assert metadata is not None
        assert metadata.approved_route_id == "betclic-match-service-get-popular-v2"


def test_live_count_and_unapproved_routes_never_read_body(tmp_path: Path) -> None:
    for url in (
        LIVE_COUNT,
        "https://offering.begmedia.com/web/offering.access.api/"
        "offering.access.api.MatchService/Unapproved",
    ):
        response = _Response(_frame(b"PROTOBUF-FAKE"))
        outcome = observe_betclic_grpc_response(
            response=response,
            response_url=url,
            content_type="application/grpc-web",
            content_length=None,
            maximum_bytes=1024,
            diagnostic_directory=tmp_path,
        )
        assert response.body_calls == 0
        assert outcome.diagnostic is None
        assert outcome.body_read is False
        assert outcome.evidence_stored is False
        if url == LIVE_COUNT:
            metadata = build_network_metadata(
                response_url=url,
                allowed_hostnames=ALLOWED,
                status_code=200,
                content_type="application/grpc-web",
                resource_type="fetch",
                observed_at_utc=NOW,
                provider_id="betclic-pt",
            )
            assert metadata is not None
            assert metadata.approved_route_id == "betclic-match-service-get-live-count"
    assert list(tmp_path.iterdir()) == []


def test_inspector_error_keeps_observed_and_schema_unverified_classifications(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    outcome = observe_betclic_grpc_response(
        response=_Response(b"\x00\x00"),
        response_url=POPULAR,
        content_type="application/grpc-web",
        content_length=None,
        maximum_bytes=1024,
        diagnostic_directory=tmp_path / "diagnostics",
    )
    metadata = _metadata(outcome)
    assert metadata is not None
    acquisition = BrowserAcquisitionResult(
        provider_id="betclic-pt",
        sport="football",
        acquisition_cycle_id="cycle-grpc-inspector-rejected",
        observed_at_utc=NOW,
        browser_mode=BrowserMode.VISIBLE,
        pages=(),
        responses=(),
        diagnostics=(),
        block_reason=None,
        warnings=(),
        network_metadata=(metadata,),
    )
    result = collect_probe_from_acquisition(
        provider_id="betclic-pt",
        sport="football",
        acquisition=acquisition,
        duration_seconds=1.0,
        diagnostic_directory="diagnostics",
    )
    assert set(result.classifications) == {
        "betclic-offering-grpc-observed",
        "betclic-offering-schema-unverified",
    }
    assert result.network_metadata[0]["grpc_web_failure_code"] == "truncated-header"
    assert result.network_metadata[0]["grpc_web_body_read"] is True
    assert result.network_metadata[0]["grpc_web_evidence_stored"] is False
    assert result.network_metadata[0]["grpc_web_malformed_or_truncated"] is True


def test_probe_json_contains_reference_and_summary_but_no_body_or_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    diagnostic_root = tmp_path / "diagnostics"
    body = _frame(b"PROTOBUF-FAKE")
    outcome = observe_betclic_grpc_response(
        response=_Response(body),
        response_url=POPULAR,
        content_type="application/grpc-web",
        content_length=None,
        maximum_bytes=1024,
        diagnostic_directory=diagnostic_root,
    )
    assert outcome.diagnostic is not None
    metadata = _metadata(outcome)
    assert metadata is not None
    acquisition = BrowserAcquisitionResult(
        provider_id="betclic-pt",
        sport="football",
        acquisition_cycle_id="cycle-grpc-probe",
        observed_at_utc=NOW,
        browser_mode=BrowserMode.VISIBLE,
        pages=(),
        responses=(),
        diagnostics=(),
        block_reason=None,
        warnings=(),
        network_metadata=(metadata,),
        grpc_web_diagnostics=(outcome.diagnostic,),
    )
    result = collect_probe_from_acquisition(
        provider_id="betclic-pt",
        sport="football",
        acquisition=acquisition,
        duration_seconds=1.0,
        diagnostic_directory="diagnostics",
    )
    payload_text = (diagnostic_root / result.diagnostic_relative_path).read_text(encoding="utf-8")
    payload = json.loads(payload_text)
    diagnostic = payload["grpc_web_diagnostics"][0]
    assert diagnostic["capture_kind"] == "betclic-grpc-web"
    assert diagnostic["checksum"] == outcome.diagnostic.checksum_sha256
    assert diagnostic["relative_path"] == outcome.diagnostic.relative_path
    assert diagnostic["framing"] == "binary"
    assert diagnostic["data_frame_count"] == 1
    assert "PROTOBUF-FAKE" not in payload_text
    assert "https://" not in payload_text
    assert "response_url" not in payload_text
    assert set(result.classifications) >= {
        "betclic-offering-grpc-observed",
        "betclic-offering-envelope-recognized",
        "betclic-offering-schema-unverified",
    }


def test_unrecognized_grpc_web_substring_mime_does_not_read_body(tmp_path: Path) -> None:
    response = _Response(_frame(b"PROTOBUF-FAKE"))
    outcome = observe_betclic_grpc_response(
        response=response,
        response_url=POPULAR,
        content_type="application/not-grpc-web-but-contains-grpc-web",
        content_length=None,
        maximum_bytes=1024,
        diagnostic_directory=tmp_path,
    )
    assert response.body_calls == 0
    assert outcome.body_read is False
    assert outcome.evidence_stored is False


def test_post_storage_diagnostic_failure_removes_only_new_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = _frame(b"PROTOBUF-FAKE")

    def fail_diagnostic(*args, **kwargs):
        del args, kwargs
        raise ValueError("synthetic construction failure")

    monkeypatch.setattr(
        "sports_analytics.sources.browser.playwright_runtime.BrowserGrpcWebDiagnostic",
        fail_diagnostic,
    )
    outcome = observe_betclic_grpc_response(
        response=_Response(body),
        response_url=POPULAR,
        content_type="application/grpc-web",
        content_length=None,
        maximum_bytes=1024,
        diagnostic_directory=tmp_path,
    )
    assert outcome.failure_code == "grpc-web-evidence-storage-failed"
    assert outcome.body_read is True
    assert outcome.evidence_stored is False
    assert not list((tmp_path / "grpc-web").glob("*.grpc-web"))


def test_post_storage_diagnostic_failure_preserves_valid_existing_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = _frame(b"PROTOBUF-FAKE")
    initial = observe_betclic_grpc_response(
        response=_Response(body),
        response_url=POPULAR,
        content_type="application/grpc-web",
        content_length=None,
        maximum_bytes=1024,
        diagnostic_directory=tmp_path,
    )
    assert initial.diagnostic is not None
    raw_path = tmp_path / initial.diagnostic.relative_path
    assert raw_path.is_file()

    def fail_diagnostic(*args, **kwargs):
        del args, kwargs
        raise ValueError("synthetic construction failure")

    monkeypatch.setattr(
        "sports_analytics.sources.browser.playwright_runtime.BrowserGrpcWebDiagnostic",
        fail_diagnostic,
    )
    outcome = observe_betclic_grpc_response(
        response=_Response(body),
        response_url=POPULAR,
        content_type="application/grpc-web",
        content_length=None,
        maximum_bytes=1024,
        diagnostic_directory=tmp_path,
    )
    assert outcome.failure_code == "grpc-web-evidence-storage-failed"
    assert outcome.evidence_stored is False
    assert raw_path.is_file()


def test_rejected_probe_publication_removes_new_raw_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    diagnostic_root = tmp_path / "diagnostics"
    outcome = observe_betclic_grpc_response(
        response=_Response(_frame(b"PROTOBUF-FAKE")),
        response_url=POPULAR,
        content_type="application/grpc-web",
        content_length=None,
        maximum_bytes=1024,
        diagnostic_directory=diagnostic_root,
    )
    assert outcome.diagnostic is not None
    raw_path = diagnostic_root / outcome.diagnostic.relative_path
    assert raw_path.is_file()
    acquisition = BrowserAcquisitionResult(
        provider_id="betclic-pt",
        sport="football",
        acquisition_cycle_id="cycle-grpc-rejected-probe",
        observed_at_utc=NOW,
        browser_mode=BrowserMode.VISIBLE,
        pages=(
            BrowserPageObservation(
                provider_id="betclic-pt",
                page_route_id="football-prematch",
                hostname="www.betclic.pt",
                observed_at_utc=NOW,
                block_reason=None,
            ),
        ),
        responses=(),
        diagnostics=(),
        block_reason=None,
        warnings=(),
        grpc_web_diagnostics=(outcome.diagnostic,),
    )
    monkeypatch.setattr(
        "sports_analytics.bookmakers.diagnostics.probe.publish_diagnostic_json",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            UnsafeDiagnosticError("synthetic-publication-rejection")
        ),
    )
    with pytest.raises(UnsafeDiagnosticError):
        collect_probe_from_acquisition(
            provider_id="betclic-pt",
            sport="football",
            acquisition=acquisition,
            duration_seconds=1.0,
            diagnostic_directory="diagnostics",
        )
    assert not raw_path.exists()
    assert not list(diagnostic_root.glob("*.json"))
    assert not list(diagnostic_root.glob("*.tmp"))
