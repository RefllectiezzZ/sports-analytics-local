"""Symlink-safe snapshot path resolution tests."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from sports_analytics.core.exceptions import SnapshotVerificationError
from sports_analytics.snapshots.paths import resolve_snapshot_dir, resolve_snapshot_file

pytestmark = pytest.mark.skipif(
    not hasattr(os, "symlink"),
    reason="platform lacks symlink support",
)


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "snapshots"
    root.mkdir()
    return root


def test_rejects_symlink_root_child_directory(tmp_path: Path) -> None:
    root = _root(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "football-ingestion").symlink_to(outside, target_is_directory=True)
    with pytest.raises(SnapshotVerificationError, match="symlink"):
        resolve_snapshot_dir(root, "football-ingestion/child")


def test_rejects_symlink_intermediate_directory(tmp_path: Path) -> None:
    root = _root(tmp_path)
    (root / "football-ingestion").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "football-ingestion" / "football-canonical-v1").symlink_to(
        outside, target_is_directory=True
    )
    with pytest.raises(SnapshotVerificationError, match="symlink"):
        resolve_snapshot_dir(
            root,
            "football-ingestion/football-canonical-v1/eng-premier-league",
        )


def test_rejects_symlink_manifest_file(tmp_path: Path) -> None:
    root = _root(tmp_path)
    directory = root / "dir"
    directory.mkdir()
    target = tmp_path / "manifest.json"
    target.write_text("{}", encoding="utf-8")
    (directory / "manifest.json").symlink_to(target)
    with pytest.raises(SnapshotVerificationError, match="symlink"):
        resolve_snapshot_file(root, "dir/manifest.json")


def test_rejects_symlink_parquet_file(tmp_path: Path) -> None:
    root = _root(tmp_path)
    directory = root / "dir"
    directory.mkdir()
    target = tmp_path / "games.parquet"
    target.write_bytes(b"parquet")
    (directory / "games.parquet").symlink_to(target)
    with pytest.raises(SnapshotVerificationError, match="symlink"):
        resolve_snapshot_file(root, "dir/games.parquet")
