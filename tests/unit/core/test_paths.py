"""Unit tests for path resolution and runtime directory creation."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from sports_analytics.core.exceptions import RuntimeBootstrapError
from sports_analytics.core.paths import create_runtime_directories, resolve_paths
from sports_analytics.core.settings import Settings, load_settings


def test_relative_paths_resolve_against_base(tmp_path: Path) -> None:
    settings = load_settings(environ={}, base_directory=tmp_path)
    paths = resolve_paths(settings, tmp_path)
    assert paths.storage_root == (tmp_path / "storage").resolve()
    assert paths.logs_directory == (tmp_path / "storage" / "logs").resolve()


def test_absolute_paths_remain_absolute(tmp_path: Path) -> None:
    absolute_root = (tmp_path / "elsewhere").resolve()
    settings = load_settings(
        overrides={
            "storage": {
                "root_directory": str(absolute_root),
                "sqlite_path": str(absolute_root / "db.sqlite3"),
                "raw_directory": str(absolute_root / "raw"),
                "snapshots_directory": str(absolute_root / "snapshots"),
                "features_directory": str(absolute_root / "features"),
                "models_directory": str(absolute_root / "models"),
                "exports_directory": str(absolute_root / "exports"),
                "logs_directory": str(absolute_root / "logs"),
            }
        },
        environ={},
        base_directory=tmp_path,
    )
    paths = resolve_paths(settings, tmp_path)
    assert paths.storage_root == absolute_root
    assert paths.sqlite_path == absolute_root / "db.sqlite3"


def test_all_runtime_paths_are_absolute(tmp_path: Path) -> None:
    settings = Settings()
    paths = resolve_paths(settings, tmp_path)
    for value in (
        paths.base_directory,
        paths.storage_root,
        paths.sqlite_path,
        paths.raw_directory,
        paths.snapshots_directory,
        paths.features_directory,
        paths.models_directory,
        paths.exports_directory,
        paths.logs_directory,
    ):
        assert value.is_absolute()


def test_resolution_does_not_create_directories(tmp_path: Path) -> None:
    settings = Settings()
    paths = resolve_paths(settings, tmp_path)
    assert not paths.storage_root.exists()
    assert not paths.logs_directory.exists()


def test_resolution_does_not_change_cwd(tmp_path: Path) -> None:
    original = Path.cwd()
    settings = Settings()
    resolve_paths(settings, tmp_path)
    assert Path.cwd() == original


def test_paths_not_forced_inside_root(tmp_path: Path) -> None:
    outside = (tmp_path / "outside-logs").resolve()
    settings = load_settings(
        overrides={"storage": {"logs_directory": str(outside)}},
        environ={},
        base_directory=tmp_path,
    )
    paths = resolve_paths(settings, tmp_path)
    assert paths.logs_directory == outside
    assert paths.logs_directory != paths.storage_root
    assert paths.storage_root not in paths.logs_directory.parents


def test_create_runtime_directories_idempotent(tmp_path: Path) -> None:
    settings = Settings()
    paths = resolve_paths(settings, tmp_path)
    create_runtime_directories(paths)
    marker = paths.raw_directory / "keep-me.txt"
    marker.write_text("preserved", encoding="utf-8")
    create_runtime_directories(paths)
    assert marker.read_text(encoding="utf-8") == "preserved"
    for directory in (
        paths.storage_root,
        paths.raw_directory,
        paths.snapshots_directory,
        paths.features_directory,
        paths.models_directory,
        paths.exports_directory,
        paths.logs_directory,
        paths.sqlite_path.parent,
    ):
        assert directory.is_dir()


def test_sqlite_file_not_created(tmp_path: Path) -> None:
    settings = Settings()
    paths = resolve_paths(settings, tmp_path)
    create_runtime_directories(paths)
    assert paths.sqlite_path.parent.is_dir()
    assert not paths.sqlite_path.exists()


def test_create_directories_wraps_os_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings()
    paths = resolve_paths(settings, tmp_path)

    def boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "mkdir", boom)
    with pytest.raises(RuntimeBootstrapError, match="failed to create runtime directories"):
        create_runtime_directories(paths)


def test_windows_safe_pathlib_join(tmp_path: Path) -> None:
    settings = load_settings(
        overrides={"storage": {"raw_directory": "storage/raw"}},
        environ={},
        base_directory=tmp_path,
    )
    paths = resolve_paths(settings, tmp_path)
    assert paths.raw_directory == Path(tmp_path, "storage", "raw").resolve()
    assert os.fspath(paths.raw_directory)
