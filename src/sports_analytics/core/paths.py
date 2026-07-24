"""Deterministic path resolution and safe runtime directory creation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sports_analytics.core.exceptions import RuntimeBootstrapError
from sports_analytics.core.settings import Settings


@dataclass(frozen=True, slots=True)
class RuntimePaths:
    """Absolute filesystem paths used by a local runtime instance."""

    base_directory: Path
    storage_root: Path
    sqlite_path: Path
    raw_directory: Path
    snapshots_directory: Path
    features_directory: Path
    models_directory: Path
    exports_directory: Path
    logs_directory: Path


def _resolve_path(path: Path, base_directory: Path) -> Path:
    """Resolve a configured path against ``base_directory`` when relative."""
    if path.is_absolute():
        return path.resolve()
    return (base_directory / path).resolve()


def resolve_paths(settings: Settings, base_directory: Path | str) -> RuntimePaths:
    """Resolve configured storage paths to absolute normalized locations.

    This function is pure: it does not create files or directories and does not
    change the process working directory.
    """
    base = Path(base_directory)
    if not base.is_absolute():
        base = base.resolve()
    else:
        base = base.resolve()

    storage = settings.storage
    return RuntimePaths(
        base_directory=base,
        storage_root=_resolve_path(storage.root_directory, base),
        sqlite_path=_resolve_path(storage.sqlite_path, base),
        raw_directory=_resolve_path(storage.raw_directory, base),
        snapshots_directory=_resolve_path(storage.snapshots_directory, base),
        features_directory=_resolve_path(storage.features_directory, base),
        models_directory=_resolve_path(storage.models_directory, base),
        exports_directory=_resolve_path(storage.exports_directory, base),
        logs_directory=_resolve_path(storage.logs_directory, base),
    )


def create_runtime_directories(paths: RuntimePaths) -> None:
    """Create required runtime directories idempotently.

    Never creates the SQLite database file itself. Never deletes or clears
    existing data.
    """
    directories = (
        paths.storage_root,
        paths.sqlite_path.parent,
        paths.raw_directory,
        paths.snapshots_directory,
        paths.features_directory,
        paths.models_directory,
        paths.exports_directory,
        paths.logs_directory,
    )
    try:
        for directory in directories:
            directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        msg = f"failed to create runtime directories under {paths.storage_root}: {exc}"
        raise RuntimeBootstrapError(msg) from exc
