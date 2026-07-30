"""Incremental gRPC-Web framing tests using only invented transport bytes."""

from __future__ import annotations

import base64
from datetime import UTC, datetime

import pytest

from sports_analytics.sources.browser.grpc_web_stream import (
    GrpcWebFrameKind,
    GrpcWebFraming,
    IncrementalGrpcWebDecoder,
    IncrementalGrpcWebError,
    IncrementalGrpcWebLimits,
    framing_for_content_type,
)

NOW = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)


def _frame(payload: bytes, *, trailer: bool = False) -> bytes:
    return bytes([0x80 if trailer else 0x00]) + len(payload).to_bytes(4, "big") + payload


def _decoder(**overrides: int) -> IncrementalGrpcWebDecoder:
    return IncrementalGrpcWebDecoder(
        framing=GrpcWebFraming.BINARY,
        limits=IncrementalGrpcWebLimits(**overrides),
    )


@pytest.mark.parametrize("split", range(1, 10))
def test_binary_frame_survives_every_header_and_payload_split(split: int) -> None:
    framed = _frame(b"\x08\x01\x12\x03abc")
    decoder = _decoder()
    first = decoder.feed(
        framed[:split],
        observed_at_utc=NOW,
        source_capture_reference="reviewed-route",
    )
    second = decoder.feed(
        framed[split:],
        observed_at_utc=NOW,
        source_capture_reference="reviewed-route",
    )
    frames = first + second
    assert len(frames) == 1
    assert frames[0].payload == b"\x08\x01\x12\x03abc"
    assert frames[0].framed_bytes == framed
    assert frames[0].kind is GrpcWebFrameKind.DATA
    decoder.finalize()


def test_several_frames_and_exact_chunk_boundary() -> None:
    data = _frame(b"\x08\x01")
    trailer = _frame(b"grpc-status: 0\r\n", trailer=True)
    decoder = _decoder()
    frames = decoder.feed(
        data + trailer,
        observed_at_utc=NOW,
        source_capture_reference="reviewed-route",
    )
    assert [item.kind for item in frames] == [
        GrpcWebFrameKind.DATA,
        GrpcWebFrameKind.TRAILER,
    ]
    assert frames[1].grpc_status == "0"
    assert decoder.trailer_seen is True
    decoder.finalize()


@pytest.mark.parametrize(
    ("suffix", "classification"),
    [
        (b"\x00\x00", "streaming-trailing-partial-header"),
        (
            b"\x00\x00\x00\x00\x04\x08",
            "streaming-trailing-partial-payload",
        ),
    ],
)
def test_finalize_rejects_trailing_partial_units(
    suffix: bytes,
    classification: str,
) -> None:
    decoder = _decoder()
    assert (
        decoder.feed(
            suffix,
            observed_at_utc=NOW,
            source_capture_reference="reviewed-route",
        )
        == ()
    )
    with pytest.raises(IncrementalGrpcWebError, match=classification) as captured:
        decoder.finalize()
    assert captured.value.truncated is True


def test_text_mode_preserves_quartets_and_frame_boundaries() -> None:
    framed = _frame(b"\x08\x01") + _frame(b"grpc-status: 0\r\n", trailer=True)
    encoded = base64.b64encode(framed)
    decoder = IncrementalGrpcWebDecoder(framing=GrpcWebFraming.TEXT)
    frames = ()
    for byte in encoded:
        frames += decoder.feed(
            bytes([byte]),
            observed_at_utc=NOW,
            source_capture_reference="reviewed-route",
        )
    assert len(frames) == 2
    assert frames[0].framed_bytes == _frame(b"\x08\x01")
    assert frames[1].grpc_status == "0"
    decoder.finalize()
    assert (
        framing_for_content_type(
            "application/grpc-web-text",
            allow_text=True,
        )
        is GrpcWebFraming.TEXT
    )
    with pytest.raises(IncrementalGrpcWebError, match="text-framing-unreviewed"):
        framing_for_content_type("application/grpc-web-text")


@pytest.mark.parametrize(
    ("payload", "classification"),
    [
        (b"@@==", "streaming-invalid-base64"),
        (b"AA=A", "streaming-invalid-base64-padding"),
    ],
)
def test_text_mode_rejects_invalid_alphabet_and_padding(
    payload: bytes,
    classification: str,
) -> None:
    decoder = IncrementalGrpcWebDecoder(framing=GrpcWebFraming.TEXT)
    with pytest.raises(IncrementalGrpcWebError, match=classification):
        decoder.feed(
            payload,
            observed_at_utc=NOW,
            source_capture_reference="reviewed-route",
        )


def test_frame_payload_total_chunk_and_frame_limits_fail_closed() -> None:
    with pytest.raises(IncrementalGrpcWebError, match="frame-payload-limit"):
        _decoder(maximum_frame_payload_bytes=2).feed(
            _frame(b"abc"),
            observed_at_utc=NOW,
            source_capture_reference="reviewed-route",
        )

    total = _decoder(
        maximum_buffered_bytes=8,
        maximum_frame_payload_bytes=8,
        maximum_incomplete_trailing_bytes=8,
    )
    with pytest.raises(IncrementalGrpcWebError, match="response-byte-limit"):
        total.feed(
            _frame(b"abcd"),
            observed_at_utc=NOW,
            source_capture_reference="reviewed-route",
        )

    chunks = _decoder(maximum_data_chunks=1)
    chunks.feed(b"\x00", observed_at_utc=NOW, source_capture_reference="reviewed-route")
    with pytest.raises(IncrementalGrpcWebError, match="chunk-limit"):
        chunks.feed(b"\x00", observed_at_utc=NOW, source_capture_reference="reviewed-route")

    frames = _decoder(maximum_frames=1)
    with pytest.raises(IncrementalGrpcWebError, match="frame-count-limit"):
        frames.feed(
            _frame(b"\x08\x01") + _frame(b"\x08\x02"),
            observed_at_utc=NOW,
            source_capture_reference="reviewed-route",
        )


def test_compressed_unknown_flags_and_frame_after_trailer_are_rejected() -> None:
    for framed, classification in (
        (b"\x01\x00\x00\x00\x01x", "compressed-frame-unsupported"),
        (b"\x02\x00\x00\x00\x00", "unsupported-frame-flags"),
        (
            _frame(b"grpc-status: 0\r\n", trailer=True) + _frame(b"\x08\x01"),
            "frame-after-trailer",
        ),
    ):
        with pytest.raises(IncrementalGrpcWebError, match=classification):
            _decoder().feed(
                framed,
                observed_at_utc=NOW,
                source_capture_reference="reviewed-route",
            )
