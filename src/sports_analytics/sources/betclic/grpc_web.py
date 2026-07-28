"""Bounded gRPC-web envelope inspection without protobuf interpretation."""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Final, NoReturn

from sports_analytics.core.exceptions import PermanentSourceError

MAX_GRPC_WEB_RESPONSE_BYTES: Final[int] = 2_097_152
_FRAME_HEADER_BYTES: Final[int] = 5
_HASH_CHUNK_BYTES: Final[int] = 64 * 1024
_TRAILER_NAME: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_BINARY_CONTENT_TYPES: Final[frozenset[str]] = frozenset(
    {"application/grpc-web", "application/grpc-web+proto"}
)
_TEXT_CONTENT_TYPES: Final[frozenset[str]] = frozenset(
    {"application/grpc-web-text", "application/grpc-web-text+proto"}
)


class GrpcWebEnvelopeError(PermanentSourceError):
    """Safe transport-only rejection with a bounded public classification."""

    def __init__(
        self,
        classification: str,
        *,
        malformed_or_truncated: bool,
    ) -> None:
        super().__init__(classification)
        self.classification = classification
        self.malformed_or_truncated = malformed_or_truncated


@dataclass(frozen=True, slots=True)
class GrpcWebEnvelopeInspection:
    """Transport-only gRPC-web framing facts."""

    framing: str
    data_frame_count: int
    trailer_frame_count: int
    compression_flag_present: bool
    total_framed_payload_bytes: int
    malformed_or_truncated: bool
    grpc_status: str | None
    classifications: tuple[str, ...] = (
        "betclic-offering-grpc-observed",
        "betclic-offering-envelope-recognized",
        "betclic-offering-schema-unverified",
    )


@dataclass(frozen=True, slots=True)
class StoredGrpcWebEvidence:
    """Ephemeral local storage outcome used to build a sanitized reference."""

    checksum_sha256: str
    byte_count: int
    absolute_path: Path
    newly_created: bool


def inspect_grpc_web_envelope(
    body: bytes,
    *,
    content_type: str,
    maximum_bytes: int = MAX_GRPC_WEB_RESPONSE_BYTES,
) -> GrpcWebEnvelopeInspection:
    """Inspect binary or base64 text framing and reject malformed semantics."""
    lowered = content_type.casefold().split(";", 1)[0].strip()
    if lowered in _TEXT_CONTENT_TYPES:
        framing = "text"
    elif lowered in _BINARY_CONTENT_TYPES:
        framing = "binary"
    else:
        _reject_envelope("unsupported-content-type", malformed_or_truncated=False)
    if not body:
        _reject_envelope("empty-envelope", malformed_or_truncated=True)
    if framing == "text":
        maximum_encoded_bytes = ((maximum_bytes + 2) // 3) * 4 + 4
        if len(body) > maximum_encoded_bytes:
            _reject_envelope("encoded-size-exceeded", malformed_or_truncated=False)
        try:
            framed = base64.b64decode(b"".join(body.split()), validate=True)
        except (binascii.Error, ValueError):
            _reject_envelope("invalid-base64", malformed_or_truncated=True)
        if not framed:
            _reject_envelope("empty-envelope", malformed_or_truncated=True)
        if len(framed) > maximum_bytes:
            _reject_envelope("decoded-size-exceeded", malformed_or_truncated=False)
    else:
        if len(body) > maximum_bytes:
            _reject_envelope("body-size-exceeded", malformed_or_truncated=False)
        framed = body

    offset = 0
    data_frames = 0
    trailer_frames = 0
    compression = False
    total_payload = 0
    grpc_status: str | None = None
    trailer_seen = False
    while offset < len(framed):
        if len(framed) - offset < _FRAME_HEADER_BYTES:
            _reject_envelope("truncated-header", malformed_or_truncated=True)
        flags = framed[offset]
        length = int.from_bytes(framed[offset + 1 : offset + 5], "big")
        offset += _FRAME_HEADER_BYTES
        if flags & 0x01:
            compression = True
            _reject_envelope("compression-unsupported", malformed_or_truncated=False)
        if flags not in {0x00, 0x80}:
            _reject_envelope("invalid-flags", malformed_or_truncated=True)
        if trailer_seen:
            if flags == 0x80:
                _reject_envelope("multiple-trailers", malformed_or_truncated=True)
            _reject_envelope("frame-after-trailer", malformed_or_truncated=True)
        if length > maximum_bytes or offset + length > len(framed):
            _reject_envelope(
                "truncated-or-impossible-frame-size",
                malformed_or_truncated=True,
            )
        payload = framed[offset : offset + length]
        offset += length
        total_payload += length
        if flags & 0x80:
            _validate_trailer_payload(payload)
            trailer_frames += 1
            trailer_seen = True
            grpc_status = _safe_grpc_status(payload)
        else:
            data_frames += 1
    return GrpcWebEnvelopeInspection(
        framing=framing,
        data_frame_count=data_frames,
        trailer_frame_count=trailer_frames,
        compression_flag_present=compression,
        total_framed_payload_bytes=total_payload,
        malformed_or_truncated=False,
        grpc_status=grpc_status,
    )


def store_content_addressed_grpc_evidence(
    body: bytes,
    *,
    directory: Path,
) -> StoredGrpcWebEvidence:
    """Store bounded raw evidence by digest without logging its content."""
    if len(body) > MAX_GRPC_WEB_RESPONSE_BYTES:
        msg = "gRPC-web response exceeds the maximum retained size"
        raise PermanentSourceError(msg)
    checksum = hashlib.sha256(body).hexdigest()
    try:
        if directory.is_symlink():
            msg = "gRPC-web evidence directory must not be a symlink"
            raise PermanentSourceError(msg)
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        msg = "gRPC-web evidence directory is unavailable"
        raise PermanentSourceError(msg) from None
    target = directory / f"{checksum}.grpc-web"
    try:
        if target.is_symlink():
            msg = "gRPC-web evidence target must not be a symlink"
            raise PermanentSourceError(msg)
        if target.exists():
            target_stat = target.stat()
            if not stat.S_ISREG(target_stat.st_mode):
                msg = "existing gRPC-web evidence must be a regular file"
                raise PermanentSourceError(msg)
            if target_stat.st_size > MAX_GRPC_WEB_RESPONSE_BYTES:
                msg = "existing gRPC-web evidence exceeds the maximum retained size"
                raise PermanentSourceError(msg)
            if target_stat.st_size != len(body):
                msg = "existing gRPC-web evidence has an unexpected size"
                raise PermanentSourceError(msg)
            digest = hashlib.sha256()
            with target.open("rb") as handle:
                opened_stat = os.fstat(handle.fileno())
                if (
                    not stat.S_ISREG(opened_stat.st_mode)
                    or opened_stat.st_size != len(body)
                    or opened_stat.st_size > MAX_GRPC_WEB_RESPONSE_BYTES
                ):
                    msg = "existing gRPC-web evidence changed during verification"
                    raise PermanentSourceError(msg)
                remaining = len(body)
                while remaining:
                    chunk = handle.read(min(_HASH_CHUNK_BYTES, remaining))
                    if not chunk:
                        msg = "existing gRPC-web evidence has an unexpected size"
                        raise PermanentSourceError(msg)
                    digest.update(chunk)
                    remaining -= len(chunk)
                if handle.read(1):
                    msg = "existing gRPC-web evidence has an unexpected size"
                    raise PermanentSourceError(msg)
            if digest.hexdigest() != checksum:
                msg = "existing gRPC-web evidence does not match its checksum"
                raise PermanentSourceError(msg)
            return StoredGrpcWebEvidence(
                checksum_sha256=checksum,
                byte_count=len(body),
                absolute_path=target,
                newly_created=False,
            )
    except OSError:
        msg = "existing gRPC-web evidence could not be verified"
        raise PermanentSourceError(msg) from None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{checksum}.",
            suffix=".tmp",
            dir=str(directory),
        )
    except OSError:
        msg = "gRPC-web evidence temporary file could not be created"
        raise PermanentSourceError(msg) from None
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except OSError:
        msg = "gRPC-web evidence could not be stored"
        raise PermanentSourceError(msg) from None
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
    return StoredGrpcWebEvidence(
        checksum_sha256=checksum,
        byte_count=len(body),
        absolute_path=target,
        newly_created=True,
    )


def is_recognized_grpc_web_content_type(content_type: str | None) -> bool:
    """Return whether a MIME value is one of the reviewed gRPC-web forms."""
    if content_type is None:
        return False
    media_type = content_type.casefold().split(";", 1)[0].strip()
    return media_type in _BINARY_CONTENT_TYPES | _TEXT_CONTENT_TYPES


def _reject_envelope(
    classification: str,
    *,
    malformed_or_truncated: bool,
) -> NoReturn:
    raise GrpcWebEnvelopeError(
        classification,
        malformed_or_truncated=malformed_or_truncated,
    )


def _safe_grpc_status(payload: bytes) -> str | None:
    try:
        trailer_text = payload.decode("ascii")
    except UnicodeDecodeError:
        return None
    for line in trailer_text.replace("\r\n", "\n").split("\n"):
        name, separator, value = line.partition(":")
        if separator and name.strip().casefold() == "grpc-status":
            status = value.strip()
            return status if status.isdigit() and len(status) <= 3 else None
    return None


def _validate_trailer_payload(payload: bytes) -> None:
    if not payload:
        _reject_envelope("invalid-trailer", malformed_or_truncated=True)
    try:
        trailer_text = payload.decode("ascii")
    except UnicodeDecodeError:
        _reject_envelope("invalid-trailer", malformed_or_truncated=True)
    lines = trailer_text.replace("\r\n", "\n").split("\n")
    headers = [line for line in lines if line]
    if not headers:
        _reject_envelope("invalid-trailer", malformed_or_truncated=True)
    for line in headers:
        name, separator, _value = line.partition(":")
        if not separator or _TRAILER_NAME.fullmatch(name.strip().casefold()) is None:
            _reject_envelope("invalid-trailer", malformed_or_truncated=True)
