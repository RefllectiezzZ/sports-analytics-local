"""Deterministic capture manifest for admitted bookmaker raw evidence."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sports_analytics.core.exceptions import PermanentSourceError, SnapshotIntegrityError
from sports_analytics.data.codec import format_utc_timestamp
from sports_analytics.data.types import validate_relative_snapshot_path, validate_sha256_checksum
from sports_analytics.snapshots.paths import resolve_raw_path
from sports_analytics.snapshots.spec import RawArtifactReference
from sports_analytics.sources.bookmaker_contracts import ProviderAcquisitionBundle
from sports_analytics.sources.raw_capture import BookmakerRawCapture
from sports_analytics.sports.contracts import require_utc

CAPTURE_MANIFEST_SCHEMA_V1: str = "bookmaker-capture-manifest-v1"
CAPTURE_MANIFEST_SCHEMA: str = "bookmaker-capture-manifest-v2"
_MANIFEST_ROOT_KEYS = frozenset({"schema", "provider_id", "acquisition_cycle_id", "captures"})
_MANIFEST_ENTRY_KEYS = frozenset(
    {
        "relative_path",
        "checksum_sha256",
        "byte_count",
        "capture_type",
        "observed_at_utc",
    }
)


@dataclass(frozen=True, slots=True)
class CaptureManifestEntry:
    """One admitted raw capture without complete URL provenance."""

    relative_path: str
    checksum_sha256: str
    byte_count: int
    capture_type: str
    observed_at_utc: datetime

    def to_json(self) -> dict[str, object]:
        return {
            "relative_path": self.relative_path,
            "checksum_sha256": self.checksum_sha256,
            "byte_count": self.byte_count,
            "capture_type": self.capture_type,
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
        observed_at_utc=capture.retrieved_at,
    )


def attach_capture_references(
    bundle: ProviderAcquisitionBundle,
    captures: tuple[BookmakerRawCapture, ...],
) -> ProviderAcquisitionBundle:
    """Attach content-addressed evidence identities to native observations."""
    from dataclasses import replace

    from sports_analytics.sources.bookmaker_contracts import (
        EventCompletenessEvidence,
        provider_native_markets,
    )

    if not isinstance(bundle, ProviderAcquisitionBundle):
        msg = "capture references require a ProviderAcquisitionBundle"
        raise PermanentSourceError(msg)
    checksums = tuple(sorted({capture.checksum_sha256 for capture in captures}))
    primary = checksums[0] if checksums else None
    events = []
    for event in bundle.events:
        markets = []
        selection_count = 0
        markets_with_price = 0
        for market in provider_native_markets(event):
            selections = tuple(
                replace(selection, source_capture_id=selection.source_capture_id or primary)
                for selection in market.selections
            )
            selection_count += len(selections)
            if selections:
                markets_with_price += 1
            markets.append(
                replace(
                    market,
                    selections=selections,
                    source_capture_id=market.source_capture_id or primary,
                )
            )
        evidence = event.completeness
        if evidence.markets_observed == 0 and markets:
            evidence = EventCompletenessEvidence(
                provider_declared_market_references=(evidence.provider_declared_market_references),
                market_groups_observed=evidence.market_groups_observed,
                markets_observed=len(markets),
                markets_parsed=len(markets),
                selections_observed=selection_count,
                selections_parsed=selection_count,
                markets_with_valid_price=markets_with_price,
                source_responses_contributing=len(checksums),
                event_detail_surface_visited=evidence.event_detail_surface_visited,
                event_detail_readiness_reached=evidence.event_detail_readiness_reached,
                truncated_response_count=evidence.truncated_response_count,
                bounded_response_rejection_count=evidence.bounded_response_rejection_count,
                completeness_state=evidence.completeness_state,
            )
        events.append(
            replace(
                event,
                markets=tuple(
                    market
                    for market in markets
                    if market.canonical_market_definition_id is not None
                ),
                native_markets=tuple(markets),
                source_capture_ids=checksums,
                completeness=evidence,
            )
        )
    return replace(bundle, events=tuple(events))


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
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{manifest.checksum_sha256}.",
            suffix=".tmp",
            dir=str(absolute.parent),
        )
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(manifest.manifest_bytes)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, absolute)
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise
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
    if not manifest.entries:
        msg = "capture manifest must contain at least one entry for admitted acquisition"
        raise SnapshotIntegrityError(msg)
    for entry in manifest.entries:
        verify_capture_entry(raw_directory=raw_directory, entry=entry)


def parse_capture_manifest_from_bytes(
    *,
    manifest_bytes: bytes,
    relative_path: str,
    expected_provider_id: str | None = None,
    expected_acquisition_cycle_id: str | None = None,
) -> CaptureManifest:
    """Parse and validate a capture manifest from canonical JSON bytes."""
    if not manifest_bytes:
        msg = "capture manifest bytes must be non-empty"
        raise SnapshotIntegrityError(msg)
    checksum = hashlib.sha256(manifest_bytes).hexdigest()
    try:
        document = json.loads(manifest_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        msg = "capture manifest is not valid UTF-8 JSON"
        raise SnapshotIntegrityError(msg) from exc
    if not isinstance(document, dict):
        msg = "capture manifest must be a JSON object"
        raise SnapshotIntegrityError(msg)
    schema = document.get("schema")
    if schema == CAPTURE_MANIFEST_SCHEMA_V1:
        msg = "legacy capture manifest v1 is rejected because it may contain URL provenance"
        raise SnapshotIntegrityError(msg)
    if schema != CAPTURE_MANIFEST_SCHEMA:
        msg = "unexpected capture manifest schema"
        raise SnapshotIntegrityError(msg)
    if frozenset(document) != _MANIFEST_ROOT_KEYS:
        msg = "capture manifest root keys do not match the v2 schema"
        raise SnapshotIntegrityError(msg)
    provider_id = document.get("provider_id")
    acquisition_cycle_id = document.get("acquisition_cycle_id")
    if not isinstance(provider_id, str) or not provider_id.strip():
        msg = "capture manifest provider_id must be a non-empty string"
        raise SnapshotIntegrityError(msg)
    if not isinstance(acquisition_cycle_id, str) or not acquisition_cycle_id.strip():
        msg = "capture manifest acquisition_cycle_id must be a non-empty string"
        raise SnapshotIntegrityError(msg)
    if expected_provider_id is not None and provider_id != expected_provider_id:
        msg = "capture manifest provider_id mismatch"
        raise SnapshotIntegrityError(msg)
    if (
        expected_acquisition_cycle_id is not None
        and acquisition_cycle_id != expected_acquisition_cycle_id
    ):
        msg = "capture manifest acquisition_cycle_id mismatch"
        raise SnapshotIntegrityError(msg)
    captures_raw = document.get("captures")
    if not isinstance(captures_raw, list) or not captures_raw:
        msg = "capture manifest captures must be a non-empty list"
        raise SnapshotIntegrityError(msg)
    entries: list[CaptureManifestEntry] = []
    seen_paths: set[str] = set()
    for index, item in enumerate(captures_raw):
        if not isinstance(item, dict):
            msg = f"capture manifest entry {index} must be an object"
            raise SnapshotIntegrityError(msg)
        if frozenset(item) != _MANIFEST_ENTRY_KEYS:
            msg = f"capture manifest entry {index} keys do not match the v2 schema"
            raise SnapshotIntegrityError(msg)
        relative = item.get("relative_path")
        entry_checksum = item.get("checksum_sha256")
        byte_count = item.get("byte_count")
        capture_type = item.get("capture_type")
        observed_raw = item.get("observed_at_utc")
        if not isinstance(relative, str) or not relative.strip():
            msg = f"capture manifest entry {index} missing relative_path"
            raise SnapshotIntegrityError(msg)
        relative_path_entry = validate_relative_snapshot_path(relative)
        if relative_path_entry in seen_paths:
            msg = f"duplicate capture manifest entry path: {relative_path_entry}"
            raise SnapshotIntegrityError(msg)
        seen_paths.add(relative_path_entry)
        if not isinstance(entry_checksum, str):
            msg = f"capture manifest entry {index} missing checksum_sha256"
            raise SnapshotIntegrityError(msg)
        entry_checksum = validate_sha256_checksum(entry_checksum)
        if not isinstance(byte_count, int) or byte_count < 1:
            msg = f"capture manifest entry {index} byte_count must be positive"
            raise SnapshotIntegrityError(msg)
        if not isinstance(capture_type, str) or not capture_type.strip():
            msg = f"capture manifest entry {index} capture_type must be non-empty"
            raise SnapshotIntegrityError(msg)
        if not isinstance(observed_raw, str) or not observed_raw.strip():
            msg = f"capture manifest entry {index} observed_at_utc must be non-empty"
            raise SnapshotIntegrityError(msg)
        from sports_analytics.data.codec import parse_utc_timestamp

        observed_at = parse_utc_timestamp(observed_raw)
        entries.append(
            CaptureManifestEntry(
                relative_path=relative_path_entry,
                checksum_sha256=entry_checksum,
                byte_count=byte_count,
                capture_type=capture_type,
                observed_at_utc=observed_at,
            )
        )
    sorted_entries = tuple(sorted(entries, key=lambda entry: entry.relative_path))
    return CaptureManifest(
        schema=CAPTURE_MANIFEST_SCHEMA,
        provider_id=provider_id,
        acquisition_cycle_id=acquisition_cycle_id,
        entries=sorted_entries,
        manifest_bytes=manifest_bytes,
        checksum_sha256=checksum,
        relative_path=validate_relative_snapshot_path(relative_path),
    )
