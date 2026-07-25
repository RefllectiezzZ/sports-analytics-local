"""Tests for immutable raw source artifact storage."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sports_analytics.core.exceptions import PermanentSourceError
from sports_analytics.sources.raw_store import RawSourceStore

SOURCE_NAME = "football-data-co-uk"
SOURCE_URL = "https://www.football-data.co.uk/mmz4281/2324/E0.csv"
RETRIEVED_AT = datetime(2025, 1, 2, 3, 4, 5, tzinfo=UTC)


def test_store_bytes_writes_content_addressed_artifact(tmp_path: Path) -> None:
    store = RawSourceStore(tmp_path / "raw")
    content = b"Div,Date\nE0,12/08/2023\n"
    expected_digest = hashlib.sha256(content).hexdigest()

    artifact = store.store_bytes(
        source_name=SOURCE_NAME,
        source_url=SOURCE_URL,
        content=content,
        retrieved_at=RETRIEVED_AT,
        content_type="text/csv",
        etag='"abc"',
        last_modified="Wed, 01 Jan 2025 00:00:00 GMT",
        encoding="utf-8",
    )

    assert artifact.source_name == SOURCE_NAME
    assert artifact.source_url == SOURCE_URL
    assert artifact.checksum_sha256 == expected_digest
    assert artifact.byte_count == len(content)
    assert artifact.relative_path == (
        f"{SOURCE_NAME}/sha256/{expected_digest[:2]}/{expected_digest}.csv"
    )
    assert artifact.content_type == "text/csv"
    assert artifact.retrieved_at == RETRIEVED_AT
    assert artifact.etag == '"abc"'
    assert artifact.last_modified == "Wed, 01 Jan 2025 00:00:00 GMT"
    assert artifact.encoding == "utf-8"
    assert store.absolute_path_for(artifact.relative_path).read_bytes() == content


def test_store_bytes_is_idempotent_for_existing_matching_artifact(tmp_path: Path) -> None:
    store = RawSourceStore(tmp_path / "raw")
    content = b"Div,Date\nE0,12/08/2023\n"

    first = store.store_bytes(
        source_name=SOURCE_NAME,
        source_url=SOURCE_URL,
        content=content,
        retrieved_at=RETRIEVED_AT,
    )
    second = store.store_bytes(
        source_name=SOURCE_NAME,
        source_url=SOURCE_URL,
        content=content,
        retrieved_at=RETRIEVED_AT,
    )

    assert second.relative_path == first.relative_path
    assert second.checksum_sha256 == first.checksum_sha256
    assert store.absolute_path_for(first.relative_path).read_bytes() == content


def test_store_bytes_rejects_content_over_limit(tmp_path: Path) -> None:
    store = RawSourceStore(tmp_path / "raw")

    with pytest.raises(PermanentSourceError, match="exceeds maximum_download_bytes"):
        store.store_bytes(
            source_name=SOURCE_NAME,
            source_url=SOURCE_URL,
            content=b"12345",
            retrieved_at=RETRIEVED_AT,
            maximum_bytes=4,
        )


def test_load_verified_returns_artifact_and_bytes(tmp_path: Path) -> None:
    store = RawSourceStore(tmp_path / "raw")
    stored = store.store_bytes(
        source_name=SOURCE_NAME,
        source_url=SOURCE_URL,
        content=b"Div,Date\n",
        retrieved_at=RETRIEVED_AT,
        content_type="text/csv",
    )

    loaded, content = store.load_verified(
        source_name=SOURCE_NAME,
        checksum_sha256=stored.checksum_sha256,
        source_url=SOURCE_URL,
        retrieved_at=RETRIEVED_AT,
    )

    assert content == b"Div,Date\n"
    assert loaded.relative_path == stored.relative_path
    assert loaded.byte_count == len(content)
    assert loaded.content_type is None
    assert loaded.etag is None
    assert loaded.last_modified is None


def test_load_verified_rejects_missing_artifact(tmp_path: Path) -> None:
    store = RawSourceStore(tmp_path / "raw")

    with pytest.raises(PermanentSourceError, match="artifact is missing"):
        store.load_verified(
            source_name=SOURCE_NAME,
            checksum_sha256="b" * 64,
            source_url=SOURCE_URL,
            retrieved_at=RETRIEVED_AT,
        )


def test_load_verified_rejects_checksum_mismatch(tmp_path: Path) -> None:
    store = RawSourceStore(tmp_path / "raw")
    stored = store.store_bytes(
        source_name=SOURCE_NAME,
        source_url=SOURCE_URL,
        content=b"Div,Date\n",
        retrieved_at=RETRIEVED_AT,
    )
    store.absolute_path_for(stored.relative_path).write_bytes(b"tampered")

    with pytest.raises(PermanentSourceError, match="checksum mismatch"):
        store.load_verified(
            source_name=SOURCE_NAME,
            checksum_sha256=stored.checksum_sha256,
            source_url=SOURCE_URL,
            retrieved_at=RETRIEVED_AT,
        )


def test_store_bytes_rejects_existing_corrupt_artifact_at_digest_path(tmp_path: Path) -> None:
    store = RawSourceStore(tmp_path / "raw")
    content = b"Div,Date\n"
    digest = hashlib.sha256(content).hexdigest()
    relative = store.relative_path_for(source_name=SOURCE_NAME, checksum_sha256=digest)
    absolute = store.absolute_path_for(relative)
    absolute.parent.mkdir(parents=True)
    absolute.write_bytes(b"corrupt")

    with pytest.raises(PermanentSourceError, match="size does not match|content does not match"):
        store.store_bytes(
            source_name=SOURCE_NAME,
            source_url=SOURCE_URL,
            content=content,
            retrieved_at=RETRIEVED_AT,
        )


def test_load_verified_rejects_symlink_artifact(tmp_path: Path) -> None:
    store = RawSourceStore(tmp_path / "raw")
    stored = store.store_bytes(
        source_name=SOURCE_NAME,
        source_url=SOURCE_URL,
        content=b"Div,Date\n",
        retrieved_at=RETRIEVED_AT,
    )
    artifact_path = store.absolute_path_for(stored.relative_path)
    artifact_path.unlink()
    target_path = tmp_path / "outside.csv"
    target_path.write_bytes(b"Div,Date\n")
    artifact_path.symlink_to(target_path)

    with pytest.raises(
        PermanentSourceError,
        match="must not be a symlink|escapes configured raw directory",
    ):
        store.load_verified(
            source_name=SOURCE_NAME,
            checksum_sha256=stored.checksum_sha256,
            source_url=SOURCE_URL,
            retrieved_at=RETRIEVED_AT,
        )
