"""Bounded provider-neutral incremental gRPC-Web transport framing."""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from sports_analytics.core.exceptions import PermanentSourceError
from sports_analytics.sports.contracts import require_utc

_FRAME_HEADER_BYTES = 5
_TRAILER_NAME = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_BASE64_ALPHABET = frozenset(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=")


class GrpcWebFraming(StrEnum):
    """Explicitly reviewed gRPC-Web wire encodings."""

    BINARY = "binary"
    TEXT = "text"


class GrpcWebFrameKind(StrEnum):
    """Transport-level gRPC-Web frame kinds."""

    DATA = "data"
    TRAILER = "trailer"


class IncrementalGrpcWebError(PermanentSourceError):
    """Safe bounded rejection from incremental transport decoding."""

    def __init__(self, classification: str, *, truncated: bool = False) -> None:
        super().__init__(classification)
        self.classification = classification
        self.truncated = truncated


@dataclass(frozen=True, slots=True)
class IncrementalGrpcWebLimits:
    """Exact conservative bounds for one incrementally observed response."""

    maximum_buffered_bytes: int = 1_048_576
    maximum_data_chunks: int = 256
    maximum_frames: int = 64
    maximum_frame_payload_bytes: int = 524_288
    maximum_incomplete_trailing_bytes: int = 65_536

    def __post_init__(self) -> None:
        values = (
            self.maximum_buffered_bytes,
            self.maximum_data_chunks,
            self.maximum_frames,
            self.maximum_frame_payload_bytes,
            self.maximum_incomplete_trailing_bytes,
        )
        if any(isinstance(value, bool) or value < 1 for value in values):
            msg = "incremental gRPC-Web limits must be positive integers"
            raise PermanentSourceError(msg)
        if self.maximum_frame_payload_bytes > self.maximum_buffered_bytes:
            msg = "frame payload limit must fit inside the response byte limit"
            raise PermanentSourceError(msg)
        if self.maximum_incomplete_trailing_bytes > self.maximum_buffered_bytes:
            msg = "trailing-byte limit must fit inside the response byte limit"
            raise PermanentSourceError(msg)


@dataclass(frozen=True, slots=True)
class IncrementalGrpcWebFrame:
    """One complete transport frame; payload and framed bytes stay ephemeral."""

    frame_index: int
    kind: GrpcWebFrameKind
    payload: bytes = field(repr=False, compare=False)
    framed_bytes: bytes = field(repr=False, compare=False)
    payload_checksum_sha256: str
    payload_length: int
    compressed: bool
    grpc_status: str | None
    observed_at_utc: datetime
    source_capture_reference: str

    def __post_init__(self) -> None:
        if self.frame_index < 0:
            msg = "frame index must be non-negative"
            raise PermanentSourceError(msg)
        if self.payload_length != len(self.payload):
            msg = "frame payload length does not match retained bytes"
            raise PermanentSourceError(msg)
        if hashlib.sha256(self.payload).hexdigest() != self.payload_checksum_sha256:
            msg = "frame payload checksum does not match retained bytes"
            raise PermanentSourceError(msg)
        if len(self.framed_bytes) != _FRAME_HEADER_BYTES + self.payload_length:
            msg = "framed byte length is inconsistent"
            raise PermanentSourceError(msg)
        object.__setattr__(
            self,
            "observed_at_utc",
            require_utc(self.observed_at_utc, field_name="observed_at_utc"),
        )
        if not self.source_capture_reference:
            msg = "source capture reference is required"
            raise PermanentSourceError(msg)


class IncrementalGrpcWebDecoder:
    """Decode complete frames across arbitrary browser-delivered chunk boundaries."""

    def __init__(
        self,
        *,
        framing: GrpcWebFraming,
        limits: IncrementalGrpcWebLimits | None = None,
    ) -> None:
        self._framing = framing
        self._limits = limits or IncrementalGrpcWebLimits()
        self._binary_buffer = bytearray()
        self._text_buffer = bytearray()
        self._decoded_byte_count = 0
        self._encoded_byte_count = 0
        self._chunk_count = 0
        self._frame_count = 0
        self._trailer_seen = False
        self._text_padding_seen = False
        self._finalized = False

    @property
    def chunk_count(self) -> int:
        return self._chunk_count

    @property
    def frame_count(self) -> int:
        return self._frame_count

    @property
    def decoded_byte_count(self) -> int:
        return self._decoded_byte_count

    @property
    def trailer_seen(self) -> bool:
        return self._trailer_seen

    def feed(
        self,
        chunk: bytes,
        *,
        observed_at_utc: datetime,
        source_capture_reference: str,
    ) -> tuple[IncrementalGrpcWebFrame, ...]:
        """Consume one bounded network chunk and return newly complete frames."""
        if self._finalized:
            raise IncrementalGrpcWebError("streaming-data-after-finalize")
        self._chunk_count += 1
        if self._chunk_count > self._limits.maximum_data_chunks:
            raise IncrementalGrpcWebError("streaming-chunk-limit-exceeded")
        if self._framing is GrpcWebFraming.TEXT:
            decoded = self._decode_text_chunk(chunk)
        else:
            decoded = chunk
            self._encoded_byte_count += len(chunk)
        if self._decoded_byte_count + len(decoded) > self._limits.maximum_buffered_bytes:
            raise IncrementalGrpcWebError("streaming-response-byte-limit-exceeded")
        self._decoded_byte_count += len(decoded)
        self._binary_buffer.extend(decoded)
        return self._drain_frames(
            observed_at_utc=observed_at_utc,
            source_capture_reference=source_capture_reference,
        )

    def finalize(self) -> None:
        """Reject incomplete text units, headers, or payloads deterministically."""
        if self._finalized:
            return
        self._finalized = True
        if self._text_buffer:
            raise IncrementalGrpcWebError(
                "streaming-incomplete-base64-quartet",
                truncated=True,
            )
        if self._binary_buffer:
            if len(self._binary_buffer) > self._limits.maximum_incomplete_trailing_bytes:
                raise IncrementalGrpcWebError(
                    "streaming-incomplete-trailing-byte-limit-exceeded",
                    truncated=True,
                )
            classification = (
                "streaming-trailing-partial-header"
                if len(self._binary_buffer) < _FRAME_HEADER_BYTES
                else "streaming-trailing-partial-payload"
            )
            raise IncrementalGrpcWebError(classification, truncated=True)

    def _decode_text_chunk(self, chunk: bytes) -> bytes:
        self._encoded_byte_count += len(chunk)
        if self._encoded_byte_count > ((self._limits.maximum_buffered_bytes + 2) // 3) * 4 + 4:
            raise IncrementalGrpcWebError("streaming-encoded-byte-limit-exceeded")
        if any(byte not in _BASE64_ALPHABET for byte in chunk):
            raise IncrementalGrpcWebError("streaming-invalid-base64")
        if self._text_padding_seen and chunk:
            raise IncrementalGrpcWebError("streaming-data-after-base64-padding")
        self._text_buffer.extend(chunk)
        quartet_bytes = (len(self._text_buffer) // 4) * 4
        if quartet_bytes == 0:
            return b""
        encoded = bytes(self._text_buffer[:quartet_bytes])
        del self._text_buffer[:quartet_bytes]
        padding_index = encoded.find(b"=")
        if padding_index >= 0:
            if padding_index < len(encoded) - 2 or encoded[padding_index:] not in {b"=", b"=="}:
                raise IncrementalGrpcWebError("streaming-invalid-base64-padding")
            if self._text_buffer:
                raise IncrementalGrpcWebError("streaming-data-after-base64-padding")
            self._text_padding_seen = True
        try:
            return base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            raise IncrementalGrpcWebError("streaming-invalid-base64") from None

    def _drain_frames(
        self,
        *,
        observed_at_utc: datetime,
        source_capture_reference: str,
    ) -> tuple[IncrementalGrpcWebFrame, ...]:
        frames: list[IncrementalGrpcWebFrame] = []
        while len(self._binary_buffer) >= _FRAME_HEADER_BYTES:
            flags = self._binary_buffer[0]
            compressed = bool(flags & 0x01)
            kind = GrpcWebFrameKind.TRAILER if flags & 0x80 else GrpcWebFrameKind.DATA
            allowed_flags = {0x00, 0x01, 0x80}
            if flags not in allowed_flags:
                raise IncrementalGrpcWebError("streaming-unsupported-frame-flags")
            if kind is GrpcWebFrameKind.TRAILER and compressed:
                raise IncrementalGrpcWebError("streaming-compressed-trailer-unsupported")
            if self._trailer_seen:
                raise IncrementalGrpcWebError("streaming-frame-after-trailer")
            payload_length = int.from_bytes(self._binary_buffer[1:5], "big")
            if payload_length > self._limits.maximum_frame_payload_bytes:
                raise IncrementalGrpcWebError("streaming-frame-payload-limit-exceeded")
            framed_length = _FRAME_HEADER_BYTES + payload_length
            if len(self._binary_buffer) < framed_length:
                return tuple(frames)
            if self._frame_count >= self._limits.maximum_frames:
                raise IncrementalGrpcWebError("streaming-frame-count-limit-exceeded")
            framed = bytes(self._binary_buffer[:framed_length])
            del self._binary_buffer[:framed_length]
            payload = framed[_FRAME_HEADER_BYTES:]
            grpc_status = _parse_safe_trailer(payload) if kind is GrpcWebFrameKind.TRAILER else None
            if compressed:
                raise IncrementalGrpcWebError("streaming-compressed-frame-unsupported")
            if kind is GrpcWebFrameKind.TRAILER:
                self._trailer_seen = True
            frames.append(
                IncrementalGrpcWebFrame(
                    frame_index=self._frame_count,
                    kind=kind,
                    payload=payload,
                    framed_bytes=framed,
                    payload_checksum_sha256=hashlib.sha256(payload).hexdigest(),
                    payload_length=payload_length,
                    compressed=compressed,
                    grpc_status=grpc_status,
                    observed_at_utc=observed_at_utc,
                    source_capture_reference=source_capture_reference,
                )
            )
            self._frame_count += 1
        return tuple(frames)


def framing_for_content_type(
    content_type: str,
    *,
    allow_text: bool = False,
) -> GrpcWebFraming:
    """Return reviewed framing; text remains disabled until live review."""
    media_type = content_type.casefold().split(";", 1)[0].strip()
    if media_type in {"application/grpc-web", "application/grpc-web+proto"}:
        return GrpcWebFraming.BINARY
    if media_type in {"application/grpc-web-text", "application/grpc-web-text+proto"}:
        if allow_text:
            return GrpcWebFraming.TEXT
        raise IncrementalGrpcWebError("streaming-text-framing-unreviewed")
    raise IncrementalGrpcWebError("streaming-unsupported-content-type")


def _parse_safe_trailer(payload: bytes) -> str | None:
    if not payload:
        raise IncrementalGrpcWebError("streaming-invalid-trailer")
    try:
        text = payload.decode("ascii")
    except UnicodeDecodeError:
        raise IncrementalGrpcWebError("streaming-invalid-trailer") from None
    status: str | None = None
    headers = [line for line in text.replace("\r\n", "\n").split("\n") if line]
    if not headers:
        raise IncrementalGrpcWebError("streaming-invalid-trailer")
    for line in headers:
        name, separator, value = line.partition(":")
        normalized_name = name.strip().casefold()
        if not separator or _TRAILER_NAME.fullmatch(normalized_name) is None:
            raise IncrementalGrpcWebError("streaming-invalid-trailer")
        if normalized_name == "grpc-status":
            candidate = value.strip()
            if not candidate.isdigit() or len(candidate) > 3:
                raise IncrementalGrpcWebError("streaming-invalid-grpc-status")
            status = candidate
    if status is None:
        raise IncrementalGrpcWebError("streaming-missing-grpc-status")
    return status
