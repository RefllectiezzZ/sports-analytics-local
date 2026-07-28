"""Strict URL-free bookmaker capture-manifest v2 tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sports_analytics.core.exceptions import SnapshotIntegrityError
from sports_analytics.sources.bookmaker_capture import (
    CAPTURE_MANIFEST_SCHEMA,
    build_capture_manifest,
    parse_capture_manifest_from_bytes,
    persist_capture_manifest,
    verify_capture_manifest,
)
from sports_analytics.sources.raw_capture import BookmakerRawCaptureStore

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
RELATIVE = "betano-pt/manifests/sha256/aa/" + ("a" * 64) + ".json"


def _document() -> dict[str, object]:
    return {
        "schema": CAPTURE_MANIFEST_SCHEMA,
        "provider_id": "betano-pt",
        "acquisition_cycle_id": "cycle-manifest-v2",
        "captures": [
            {
                "relative_path": "betano-pt/sha256/aa/" + ("a" * 64) + ".json",
                "checksum_sha256": "a" * 64,
                "byte_count": 12,
                "capture_type": "provider-json",
                "observed_at_utc": "2026-08-20T12:00:00.000000Z",
            }
        ],
    }


def _parse(document: dict[str, object]):
    return parse_capture_manifest_from_bytes(
        manifest_bytes=json.dumps(document, sort_keys=True).encode("utf-8"),
        relative_path=RELATIVE,
        expected_provider_id="betano-pt",
        expected_acquisition_cycle_id="cycle-manifest-v2",
    )


def test_valid_v2_manifest_parses_without_url_fields() -> None:
    parsed = _parse(_document())
    assert parsed.schema == "bookmaker-capture-manifest-v2"
    assert len(parsed.entries) == 1
    assert "url" not in parsed.manifest_bytes.decode("utf-8").casefold()


@pytest.mark.parametrize(
    "unknown_key",
    ["source_url", "response_url", "url", "unexpected_field"],
)
def test_unknown_or_url_entry_fields_are_rejected(unknown_key: str) -> None:
    document = _document()
    captures = document["captures"]
    assert isinstance(captures, list)
    entry = captures[0]
    assert isinstance(entry, dict)
    entry[unknown_key] = "fake"
    with pytest.raises(SnapshotIntegrityError, match="entry 0 keys"):
        _parse(document)


def test_unknown_root_field_is_rejected() -> None:
    document = _document()
    document["unexpected_root"] = "fake"
    with pytest.raises(SnapshotIntegrityError, match="root keys"):
        _parse(document)


def test_legacy_v1_is_explicitly_rejected() -> None:
    document = _document()
    document["schema"] = "bookmaker-capture-manifest-v1"
    with pytest.raises(SnapshotIntegrityError, match="legacy capture manifest v1"):
        _parse(document)


def test_v2_checksum_persistence_verification_and_reload(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    capture = BookmakerRawCaptureStore(raw).store_text(
        source_name="betano-pt",
        capture_kind="provider-json",
        content='{"synthetic":true}',
        retrieved_at=NOW,
        extension="json",
    )
    manifest = persist_capture_manifest(
        raw_directory=raw,
        manifest=build_capture_manifest(
            provider_id="betano-pt",
            acquisition_cycle_id="cycle-manifest-v2",
            captures=(capture,),
        ),
    )
    verify_capture_manifest(raw_directory=raw, manifest=manifest)
    reloaded = parse_capture_manifest_from_bytes(
        manifest_bytes=(raw / manifest.relative_path).read_bytes(),
        relative_path=manifest.relative_path,
        expected_provider_id="betano-pt",
        expected_acquisition_cycle_id="cycle-manifest-v2",
    )
    assert reloaded.checksum_sha256 == manifest.checksum_sha256
    assert reloaded.entries == manifest.entries
