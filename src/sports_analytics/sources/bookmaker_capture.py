"""Deterministic capture manifest for admitted bookmaker raw evidence."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sports_analytics.core.exceptions import PermanentSourceError, SnapshotIntegrityError
from sports_analytics.data.codec import format_utc_timestamp
from sports_analytics.data.types import validate_relative_snapshot_path, validate_sha256_checksum
from sports_analytics.snapshots.paths import resolve_raw_path
from sports_analytics.snapshots.spec import RawArtifactReference
from sports_analytics.sources.raw_capture import BookmakerRawCapture
from sports_analytics.sports.contracts import require_utc

CAPTURE_MANIFEST_SCHEMA: str = "bookmaker-capture-manifest-v1"


@dataclass(frozen=True, slots=True)
class CaptureManifestEntry:
    """One admitted raw capture with full provenance metadata."""

    relative_path: str
    checksum_sha256: str
    byte_count: int
    capture_type: str
    source_url: str | None
    observed_at_utc: datetime

    def to_json(self) -> dict[str, object]:
        return {
            "relative_path": self.relative_path,
            "checksum_sha256": self.checksum_sha256,
            "byte_count": self.byte_count,
            "capture_type": self.capture_type,
            "source_url": self.source_url,
            "observed_at_utc": format_utc_timestamp(
                require_utc(self.observed_at_utc, field_name="observed_at_utc")
            ),
        }


@dataclass(frozen=True, slots=True)
class CaptureManifest:
    """Typed manifest covering every raw capture admitted into one acquisition."""

    schema: str
    provider_id: str
    acquisition_cycle_id: str
    entries: tuple[CaptureManifestEntry, ...]
    manifest_bytes: bytes
    checksum_sha256: str
    relative_path: str

    @property
    def byte_count(self) -> int:
        return len(self.manifest_bytes)


def capture_entry_from_raw(capture: BookmakerRawCapture) -> CaptureManifestEntry:
    """Build a manifest entry from a stored raw capture artifact."""
    return CaptureManifestEntry(
        relative_path=capture.relative_path,
        checksum_sha256=capture.checksum_sha256,
        byte_count=capture.byte_count,
        capture_type=capture.capture_kind,
        source_url=capture.source_url,
        observed_at_utc=capture.retrieved_at,
    )


def build_capture_manifest(
    *,
    provider_id: str,
    acquisition_cycle_id: str,
    captures: tuple[BookmakerRawCapture, ...],
    store: object | None = None,
) -> CaptureManifest:
    """Create the smallest deterministic capture-manifest artifact."""
    del store
    entries = tuple(capture_entry_from_raw(item) for item in captures)
    payload = {
        "schema": CAPTURE_MANIFEST_SCHEMA,
        "provider_id": provider_id,
        "acquisition_cycle_id": acquisition_cycle_id,
        "captures": [entry.to_json() for entry in entries],
    }
    manifest_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    checksum = hashlib.sha256(manifest_bytes).hexdigest()
    relative = validate_relative_snapshot_path(
        f"{provider_id}/manifests/sha256/{checksum[:2]}/{checksum}.json"
    )
    return CaptureManifest(
        schema=CAPTURE_MANIFEST_SCHEMA,
        provider_id=provider_id,
        acquisition_cycle_id=acquisition_cycle_id,
        entries=entries,
        manifest_bytes=manifest_bytes,
        checksum_sha256=checksum,
        relative_path=relative,
    )


def persist_capture_manifest(
    *,
    raw_directory: Path,
    manifest: CaptureManifest,
) -> CaptureManifest:
    """Write manifest bytes to the content-addressed raw store."""
    root = Path(raw_directory)
    if root.is_symlink():
        msg = "configured raw directory must not be a symlink"
        raise PermanentSourceError(msg)
    absolute = resolve_raw_path(root, manifest.relative_path)
    if absolute.is_symlink():
        msg = "capture manifest must not be a symlink"
        raise PermanentSourceError(msg)
    absolute.parent.mkdir(parents=True, exist_ok=True)
    if absolute.exists():
        existing = absolute.read_bytes()
        if hashlib.sha256(existing).hexdigest() != manifest.checksum_sha256:
            msg = "existing capture manifest content does not match checksum path"
            raise PermanentSourceError(msg)
    else:
        absolute.write_bytes(manifest.manifest_bytes)
        with absolute.open("rb") as handle:
            os.fsync(handle.fileno())
    return manifest


def manifest_to_raw_artifact(manifest: CaptureManifest) -> RawArtifactReference:
    """Convert a verified capture manifest into a snapshot raw artifact reference."""
    return RawArtifactReference(
        relative_path=manifest.relative_path,
        checksum_sha256=manifest.checksum_sha256,
        byte_count=manifest.byte_count,
        encoding="utf-8",
    )


def verify_capture_entry(
    *,
    raw_directory: Path,
    entry: CaptureManifestEntry,
) -> None:
    """Reopen one raw capture and verify path containment, checksum, and size."""
    root = Path(raw_directory).resolve()
    relative = validate_relative_snapshot_path(entry.relative_path)
    absolute = resolve_raw_path(root, relative).resolve()
    if not str(absolute).startswith(str(root)):
        msg = f"raw capture path escapes root: {entry.relative_path}"
        raise SnapshotIntegrityError(msg)
    if absolute.is_symlink():
        msg = f"raw capture must be a regular file: {entry.relative_path}"
        raise SnapshotIntegrityError(msg)
    if not absolute.is_file():
        msg = f"raw capture file missing: {entry.relative_path}"
        raise SnapshotIntegrityError(msg)
    payload = absolute.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    if digest != validate_sha256_checksum(entry.checksum_sha256):
        msg = f"raw capture checksum mismatch: {entry.relative_path}"
        raise SnapshotIntegrityError(msg)
    if len(payload) != entry.byte_count:
        msg = f"raw capture byte count mismatch: {entry.relative_path}"
        raise SnapshotIntegrityError(msg)


def verify_capture_manifest(
    *,
    raw_directory: Path,
    manifest: CaptureManifest,
) -> None:
    """Verify manifest bytes and every referenced capture before publication."""
    absolute = resolve_raw_path(Path(raw_directory), manifest.relative_path)
    if absolute.is_symlink() or not absolute.is_file():
        msg = "capture manifest file missing or not regular"
        raise SnapshotIntegrityError(msg)
    payload = absolute.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    if digest != manifest.checksum_sha256:
        msg = "capture manifest checksum mismatch"
        raise SnapshotIntegrityError(msg)
    if len(payload) != manifest.byte_count:
        msg = "capture manifest byte count mismatch"
        raise SnapshotIntegrityError(msg)
    for entry in manifest.entries:
        verify_capture_entry(raw_directory=raw_directory, entry=entry)
