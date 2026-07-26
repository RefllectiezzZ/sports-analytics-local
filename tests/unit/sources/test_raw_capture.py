"""Offline bookmaker raw-capture path and size boundary tests."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from sports_analytics.core.exceptions import PermanentSourceError
from sports_analytics.sources.raw_capture import BookmakerRawCaptureStore

RETRIEVED_AT = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)


def test_store_text_writes_relative_content_addressed_path(tmp_path: Path) -> None:
    store = BookmakerRawCaptureStore(tmp_path / "raw", maximum_bytes=1024)
    capture = store.store_text(
        source_name="betano-pt",
        capture_kind="response-json",
        content='{"schema":"betano-fixture-bundle-v1","events":[]}',
        retrieved_at=RETRIEVED_AT,
        extension="json",
        source_url="https://www.betano.pt/synthetic",
    )
    assert not capture.relative_path.startswith(("/", "\\"))
    assert ".." not in capture.relative_path
    assert capture.relative_path.startswith("betano-pt/sha256/")
    absolute = (tmp_path / "raw" / Path(capture.relative_path)).resolve()
    assert absolute.is_file()
    assert absolute.is_relative_to((tmp_path / "raw").resolve())


@pytest.mark.parametrize(
    "source_name",
    ["../escape", "/absolute", "\\\\windows", "betano/../other"],
)
def test_rejects_path_traversal_in_source_name(tmp_path: Path, source_name: str) -> None:
    store = BookmakerRawCaptureStore(tmp_path / "raw", maximum_bytes=1024)
    with pytest.raises(PermanentSourceError, match="path traversal"):
        store.store_text(
            source_name=source_name,
            capture_kind="response-json",
            content="{}",
            retrieved_at=RETRIEVED_AT,
            extension="json",
        )


def test_rejects_symlink_root(tmp_path: Path) -> None:
    target = tmp_path / "real-raw"
    target.mkdir()
    link = tmp_path / "raw-link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as exc:  # pragma: no cover - platform may disallow symlinks
        pytest.skip(f"symlink unavailable: {exc}")
    with pytest.raises(PermanentSourceError, match="must not be a symlink"):
        BookmakerRawCaptureStore(link, maximum_bytes=1024)


def test_rejects_existing_symlink_capture_path(tmp_path: Path) -> None:
    store = BookmakerRawCaptureStore(tmp_path / "raw", maximum_bytes=1024)
    content = '{"ok":true}'
    first = store.store_text(
        source_name="betano-pt",
        capture_kind="response-json",
        content=content,
        retrieved_at=RETRIEVED_AT,
        extension="json",
    )
    absolute = tmp_path / "raw" / Path(first.relative_path)
    backup = absolute.read_bytes()
    absolute.unlink()
    decoy = tmp_path / "decoy.json"
    decoy.write_bytes(backup)
    try:
        absolute.symlink_to(decoy)
    except OSError as exc:  # pragma: no cover
        pytest.skip(f"symlink unavailable: {exc}")
    with pytest.raises(PermanentSourceError, match="must not be a symlink"):
        store.store_text(
            source_name="betano-pt",
            capture_kind="response-json",
            content=content,
            retrieved_at=RETRIEVED_AT,
            extension="json",
        )


def test_rejects_oversized_capture(tmp_path: Path) -> None:
    store = BookmakerRawCaptureStore(tmp_path / "raw", maximum_bytes=16)
    with pytest.raises(PermanentSourceError, match="bounded capture size"):
        store.store_text(
            source_name="betclic-pt",
            capture_kind="dom-fragment",
            content="x" * 64,
            retrieved_at=RETRIEVED_AT,
            extension="txt",
        )


def test_relative_path_helper_rejects_absolute_style_extension(tmp_path: Path) -> None:
    store = BookmakerRawCaptureStore(tmp_path / "raw", maximum_bytes=1024)
    with pytest.raises(PermanentSourceError, match="unsupported capture extension"):
        store.relative_path_for(
            source_name="betano-pt",
            checksum_sha256="a" * 64,
            extension="html",
        )
