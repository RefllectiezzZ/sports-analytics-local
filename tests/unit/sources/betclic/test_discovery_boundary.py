"""Betclic browser-observed offering boundary and envelope tests."""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path

import pytest

from sports_analytics.core.exceptions import PermanentSourceError
from sports_analytics.sources.betclic.discovery import approve_betclic_response_url
from sports_analytics.sources.betclic.grpc_web import (
    MAX_GRPC_WEB_RESPONSE_BYTES,
    GrpcWebEnvelopeError,
    inspect_grpc_web_envelope,
    store_content_addressed_grpc_evidence,
)
from sports_analytics.sources.bookmaker_extraction.registry import get_verified_extraction_profile
from sports_analytics.sources.browser.safety import validate_provider_navigation_url

POPULAR = (
    "https://offering.begmedia.com/web/offering.access.api/"
    "offering.access.api.MatchService/GetPopularV2"
)


def _frame(flags: int, payload: bytes) -> bytes:
    return bytes([flags]) + len(payload).to_bytes(4, "big") + payload


def test_exact_response_host_and_path_boundary() -> None:
    approved = approve_betclic_response_url(POPULAR + "?public=ignored")
    assert approved.hostname == "offering.begmedia.com"
    assert approved.metadata_only is False
    live = approve_betclic_response_url(
        "https://offering.begmedia.com/web/offering.access.api/"
        "offering.access.api.MatchService/GetLiveCount"
    )
    assert live.metadata_only is True


def test_offering_host_cannot_be_used_as_a_navigation_target() -> None:
    with pytest.raises(PermanentSourceError):
        validate_provider_navigation_url(
            POPULAR,
            allowed_hostnames=frozenset({"www.betclic.pt"}),
        )


@pytest.mark.parametrize(
    "url",
    [
        "http://offering.begmedia.com/web/offering.access.api/"
        "offering.access.api.MatchService/GetPopularV2",
        "https://other.begmedia.com/web/offering.access.api/"
        "offering.access.api.MatchService/GetPopularV2",
        "https://sub.offering.begmedia.com/web/offering.access.api/"
        "offering.access.api.MatchService/GetPopularV2",
        "https://offering.begmedia.com.:443/web/offering.access.api/"
        "offering.access.api.MatchService/GetPopularV2",
        "https://offering.begmedia.com:444/web/offering.access.api/"
        "offering.access.api.MatchService/GetPopularV2",
        "https://offering.begmedia.com/web/offering.access.api/"
        "offering.access.api.MatchService/Other",
        "https://offering.begmedia.com/web/offering.access.api/"
        "offering.access.api.MatchService/GetPopularV2/extra",
        "https://user:pass@offering.begmedia.com/web/offering.access.api/"
        "offering.access.api.MatchService/GetPopularV2",
    ],
)
def test_unapproved_response_boundaries_are_rejected(url: str) -> None:
    with pytest.raises(PermanentSourceError):
        approve_betclic_response_url(url)


def test_binary_data_and_trailer_frames() -> None:
    body = _frame(0, b"\x08\x01") + _frame(0x80, b"grpc-status: 0\r\n")
    inspected = inspect_grpc_web_envelope(body, content_type="application/grpc-web")
    assert inspected.framing == "binary"
    assert inspected.data_frame_count == 1
    assert inspected.trailer_frame_count == 1
    assert inspected.grpc_status == "0"
    assert inspected.total_framed_payload_bytes == 18


def test_one_binary_data_frame_without_trailer() -> None:
    inspected = inspect_grpc_web_envelope(
        _frame(0, b"fake"),
        content_type="application/grpc-web",
    )
    assert inspected.data_frame_count == 1
    assert inspected.trailer_frame_count == 0


def test_valid_trailer_only_envelope() -> None:
    inspected = inspect_grpc_web_envelope(
        _frame(0x80, b"grpc-status: 0\r\n"),
        content_type="application/grpc-web",
    )
    assert inspected.data_frame_count == 0
    assert inspected.trailer_frame_count == 1
    assert inspected.grpc_status == "0"


def test_grpc_web_text_base64() -> None:
    encoded = base64.b64encode(_frame(0, b"fake"))
    inspected = inspect_grpc_web_envelope(
        encoded,
        content_type="application/grpc-web-text",
    )
    assert inspected.framing == "text"
    assert inspected.data_frame_count == 1


@pytest.mark.parametrize(
    "body",
    [
        b"\x00\x00",
        bytes([0]) + (9).to_bytes(4, "big") + b"x",
        _frame(0x02, b"x"),
        _frame(0x01, b"x"),
    ],
)
def test_malformed_truncated_or_unsupported_frames_fail_closed(body: bytes) -> None:
    with pytest.raises(GrpcWebEnvelopeError):
        inspect_grpc_web_envelope(body, content_type="application/grpc-web")


def test_oversized_and_invalid_text_fail_closed() -> None:
    with pytest.raises(GrpcWebEnvelopeError, match="body-size-exceeded"):
        inspect_grpc_web_envelope(
            b"x" * 11,
            content_type="application/grpc-web",
            maximum_bytes=10,
        )
    with pytest.raises(GrpcWebEnvelopeError, match="invalid-base64"):
        inspect_grpc_web_envelope(b"not+base64!", content_type="application/grpc-web-text")
    decoded_too_large = base64.b64encode(_frame(0, b"123456"))
    with pytest.raises(GrpcWebEnvelopeError, match="decoded-size-exceeded"):
        inspect_grpc_web_envelope(
            decoded_too_large,
            content_type="application/grpc-web-text",
            maximum_bytes=10,
        )
    with pytest.raises(GrpcWebEnvelopeError, match="truncated-header"):
        inspect_grpc_web_envelope(
            base64.b64encode(b"\x00\x00"),
            content_type="application/grpc-web-text",
        )


def test_empty_multiple_trailers_and_data_after_trailer_are_rejected() -> None:
    with pytest.raises(GrpcWebEnvelopeError, match="empty-envelope"):
        inspect_grpc_web_envelope(b"", content_type="application/grpc-web")
    trailer = _frame(0x80, b"grpc-status: 0\r\n")
    with pytest.raises(GrpcWebEnvelopeError, match="multiple-trailers"):
        inspect_grpc_web_envelope(trailer + trailer, content_type="application/grpc-web")
    with pytest.raises(GrpcWebEnvelopeError, match="frame-after-trailer"):
        inspect_grpc_web_envelope(
            trailer + _frame(0, b"fake"),
            content_type="application/grpc-web",
        )


@pytest.mark.parametrize(
    ("body", "content_type", "classification", "malformed"),
    [
        (b"", "application/grpc-web", "empty-envelope", True),
        (b"not+base64!", "application/grpc-web-text", "invalid-base64", True),
        (b"\x00\x00", "application/grpc-web", "truncated-header", True),
        (
            bytes([0]) + (9).to_bytes(4, "big") + b"x",
            "application/grpc-web",
            "truncated-or-impossible-frame-size",
            True,
        ),
        (_frame(0x02, b"x"), "application/grpc-web", "invalid-flags", True),
        (
            _frame(0x01, b"x"),
            "application/grpc-web",
            "compression-unsupported",
            False,
        ),
        (
            _frame(0x80, b"grpc-status: 0\r\n") * 2,
            "application/grpc-web",
            "multiple-trailers",
            True,
        ),
        (
            _frame(0x80, b"grpc-status: 0\r\n") + _frame(0, b"x"),
            "application/grpc-web",
            "frame-after-trailer",
            True,
        ),
        (_frame(0x80, b"invalid"), "application/grpc-web", "invalid-trailer", True),
    ],
)
def test_rejected_envelopes_expose_only_safe_transport_summary(
    body: bytes,
    content_type: str,
    classification: str,
    malformed: bool,
) -> None:
    with pytest.raises(GrpcWebEnvelopeError) as caught:
        inspect_grpc_web_envelope(body, content_type=content_type)
    assert caught.value.classification == classification
    assert caught.value.malformed_or_truncated is malformed
    assert str(caught.value) == classification


def test_content_type_must_be_an_exact_recognized_grpc_web_mime() -> None:
    with pytest.raises(GrpcWebEnvelopeError, match="unsupported-content-type"):
        inspect_grpc_web_envelope(
            _frame(0, b"fake"),
            content_type="application/not-grpc-web-but-contains-grpc-web",
        )


def test_raw_evidence_is_content_addressed_and_bounded(tmp_path: Path) -> None:
    body = _frame(0, b"fake")
    stored = store_content_addressed_grpc_evidence(
        body,
        directory=tmp_path,
    )
    assert stored.byte_count == len(body)
    assert stored.absolute_path.read_bytes() == body
    assert stored.absolute_path == tmp_path / f"{stored.checksum_sha256}.grpc-web"


def test_existing_raw_evidence_is_verified_with_bounded_reads(tmp_path: Path) -> None:
    body = _frame(0, b"fake")
    checksum = hashlib.sha256(body).hexdigest()
    target = tmp_path / f"{checksum}.grpc-web"

    target.write_bytes(b"x" * (MAX_GRPC_WEB_RESPONSE_BYTES + 1))
    with pytest.raises(PermanentSourceError, match="exceeds"):
        store_content_addressed_grpc_evidence(body, directory=tmp_path)

    target.write_bytes(b"x")
    with pytest.raises(PermanentSourceError, match="unexpected size"):
        store_content_addressed_grpc_evidence(body, directory=tmp_path)

    target.write_bytes(b"x" * len(body))
    with pytest.raises(PermanentSourceError, match="checksum"):
        store_content_addressed_grpc_evidence(body, directory=tmp_path)

    target.write_bytes(body)
    stored = store_content_addressed_grpc_evidence(body, directory=tmp_path)
    assert stored.newly_created is False
    assert stored.absolute_path == target


def test_betclic_verified_profile_remains_disabled() -> None:
    assert get_verified_extraction_profile("betclic-pt", "football") is None
