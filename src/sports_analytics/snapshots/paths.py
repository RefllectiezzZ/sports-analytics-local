"""Symlink-safe path resolution under configured storage roots."""

from __future__ import annotations

import os
import stat
from pathlib import Path, PurePosixPath

from sports_analytics.core.exceptions import PermanentSourceError, SnapshotVerificationError
from sports_analytics.data.types import validate_relative_snapshot_path


def _reject_symlink_component(path: Path, *, error_type: type[Exception]) -> None:
    try:
        st = path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        msg = f"unable to inspect path component: {path.name}"
        raise error_type(msg) from exc
    if stat.S_ISLNK(st.st_mode):
        msg = f"path component must not be a symlink: {path.name}"
        raise error_type(msg)


def _resolved_path_is_under_root(final: Path, root_real: Path) -> bool:
    """Return whether ``final`` is the same as or under ``root_real``."""
    try:
        final.relative_to(root_real)
        return True
    except ValueError:
        if os.name != "nt":
            return False

    def _normalize_windows_path(path: Path) -> str:
        text = os.path.normcase(os.path.normpath(str(path)))
        if text.startswith("\\\\?\\"):
            return text[4:]
        return text

    try:
        common = os.path.commonpath(
            [_normalize_windows_path(final), _normalize_windows_path(root_real)]
        )
    except ValueError:
        return False
    return common == _normalize_windows_path(root_real)


def resolve_under_root(
    root: Path,
    relative_path: str,
    *,
    expect_file: bool | None = None,
    error_type: type[Exception] = SnapshotVerificationError,
) -> Path:
    """Resolve ``relative_path`` under ``root`` without following unsafe symlinks.

    Walks each existing path component with ``lstat`` and rejects symlinks before
    following. Confirms the final canonical path remains under the configured root.
    """
    validated = validate_relative_snapshot_path(relative_path)
    root_path = Path(root)
    if root_path.is_symlink():
        msg = "configured storage root must not be a symlink"
        raise error_type(msg)
    try:
        root_real = root_path.resolve(strict=False)
    except OSError as exc:
        msg = "unable to resolve configured storage root"
        raise error_type(msg) from exc
    if not root_real.exists():
        msg = "configured storage root does not exist"
        raise error_type(msg)
    if root_real.is_symlink():
        msg = "configured storage root must not be a symlink"
        raise error_type(msg)
    if not root_real.is_dir():
        msg = "configured storage root must be a directory"
        raise error_type(msg)

    current = root_real
    parts = PurePosixPath(validated).parts
    for index, part in enumerate(parts):
        current = current / part
        _reject_symlink_component(current, error_type=error_type)
        if current.exists():
            try:
                st = current.lstat()
            except OSError as exc:
                msg = f"unable to inspect path component: {part}"
                raise error_type(msg) from exc
            is_last = index == len(parts) - 1
            if not is_last and not stat.S_ISDIR(st.st_mode):
                msg = f"intermediate path component must be a directory: {part}"
                raise error_type(msg)
            if is_last and expect_file is True and not stat.S_ISREG(st.st_mode):
                msg = f"path must be a regular file: {part}"
                raise error_type(msg)
            if is_last and expect_file is False and not stat.S_ISDIR(st.st_mode):
                msg = f"path must be a directory: {part}"
                raise error_type(msg)

    try:
        final = current.resolve(strict=False)
    except OSError as exc:
        msg = "unable to canonicalize path under storage root"
        raise error_type(msg) from exc
    if not _resolved_path_is_under_root(final, root_real):
        msg = "path escapes configured storage root"
        raise error_type(msg)
    # Final component must not be a symlink even if it appeared after resolution.
    if current.exists():
        _reject_symlink_component(current, error_type=error_type)
    return current


def resolve_raw_path(root: Path, relative_path: str) -> Path:
    """Resolve a raw-store relative path with symlink-safe checks."""
    return resolve_under_root(
        root,
        relative_path,
        expect_file=True,
        error_type=PermanentSourceError,
    )


def resolve_snapshot_file(root: Path, relative_path: str) -> Path:
    """Resolve a snapshot file path with symlink-safe checks."""
    return resolve_under_root(
        root,
        relative_path,
        expect_file=True,
        error_type=SnapshotVerificationError,
    )


def resolve_snapshot_dir(root: Path, relative_directory: str) -> Path:
    """Resolve a snapshot directory path with symlink-safe checks."""
    return resolve_under_root(
        root,
        relative_directory,
        expect_file=False,
        error_type=SnapshotVerificationError,
    )


def is_absolute_path_text(value: str) -> bool:
    """Return whether ``value`` looks like an absolute POSIX, Windows, or UNC path."""
    if not value:
        return False
    if value.startswith("/") or value.startswith("\\"):
        return True
    if len(value) >= 3 and value[1] == ":" and value[2] in {"/", "\\"}:
        return True
    if value.startswith("\\\\") or value.startswith("//"):
        return True
    return os.path.isabs(value)
