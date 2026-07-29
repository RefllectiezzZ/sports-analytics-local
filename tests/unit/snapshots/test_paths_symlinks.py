"""Symlink-safe snapshot path resolution tests."""

from __future__ import annotations

import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from sports_analytics.core.exceptions import SnapshotVerificationError
from sports_analytics.snapshots.paths import resolve_snapshot_dir, resolve_snapshot_file


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "snapshots"
    root.mkdir()
    return root


def _mark_as_symlink(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    """Exercise the real lstat-based guard without privileged filesystem setup."""
    original_lstat = Path.lstat

    def fake_lstat(candidate: Path) -> object:
        if candidate == path:
            return SimpleNamespace(st_mode=stat.S_IFLNK)
        return original_lstat(candidate)

    monkeypatch.setattr(Path, "lstat", fake_lstat)


def test_rejects_symlink_root_child_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _root(tmp_path)
    _mark_as_symlink(monkeypatch, root / "football-ingestion")
    with pytest.raises(SnapshotVerificationError, match="symlink"):
        resolve_snapshot_dir(root, "football-ingestion/child")


def test_rejects_symlink_intermediate_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _root(tmp_path)
    (root / "football-ingestion").mkdir()
    _mark_as_symlink(monkeypatch, root / "football-ingestion" / "football-canonical-v1")
    with pytest.raises(SnapshotVerificationError, match="symlink"):
        resolve_snapshot_dir(
            root,
            "football-ingestion/football-canonical-v1/eng-premier-league",
        )


def test_rejects_symlink_manifest_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _root(tmp_path)
    directory = root / "dir"
    directory.mkdir()
    manifest = directory / "manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    _mark_as_symlink(monkeypatch, manifest)
    with pytest.raises(SnapshotVerificationError, match="symlink"):
        resolve_snapshot_file(root, "dir/manifest.json")


def test_rejects_symlink_parquet_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _root(tmp_path)
    directory = root / "dir"
    directory.mkdir()
    parquet = directory / "games.parquet"
    parquet.write_bytes(b"parquet")
    _mark_as_symlink(monkeypatch, parquet)
    with pytest.raises(SnapshotVerificationError, match="symlink"):
        resolve_snapshot_file(root, "dir/games.parquet")
