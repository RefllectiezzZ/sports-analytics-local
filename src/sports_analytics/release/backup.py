"""Content-verified local v1 backup and fail-closed restore."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import uuid
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Final

from sports_analytics import __version__
from sports_analytics.core.paths import RuntimePaths
from sports_analytics.data.database import verify_sqlite_file
from sports_analytics.data.migrations import get_migration_status
from sports_analytics.release.doctor import inspect_path_safety

BACKUP_FORMAT: Final[str] = "sports-analytics-local-backup-v1"
MANIFEST_NAME: Final[str] = "manifest.json"
DATABASE_RELATIVE_PATH: Final[str] = "database/operational.sqlite3"
CONFIG_RELATIVE_PATH: Final[str] = "config/settings.toml"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_EXCLUDED_DIRECTORY_NAMES: Final[frozenset[str]] = frozenset(
    {
        ".cache",
        ".git",
        ".mypy_cache",
        ".playwright",
        ".pytest_cache",
        ".ruff_cache",
        ".tmp",
        "__pycache__",
        "browser-executables",
        "browsers",
        "cache",
        "caches",
        "playwright",
        "temp",
        "tmp",
    }
)
_EXCLUDED_FILE_SUFFIXES: Final[frozenset[str]] = frozenset({".log", ".pyc", ".temp", ".tmp"})
_PROHIBITED_EXECUTABLE_SUFFIXES: Final[frozenset[str]] = frozenset(
    {".bat", ".cmd", ".com", ".dll", ".exe", ".msi", ".ps1", ".scr", ".sh"}
)
_PROHIBITED_SENSITIVE_NAMES: Final[frozenset[str]] = frozenset(
    {
        "cookies.json",
        "credentials.json",
        "secrets.json",
        "tokens.json",
    }
)
_ROLE_PATHS: Final[tuple[tuple[str, str], ...]] = (
    ("raw", "raw_directory"),
    ("snapshots", "snapshots_directory"),
    ("features", "features_directory"),
    ("models", "models_directory"),
    ("exports", "exports_directory"),
)
_MANIFEST_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "application_version",
        "created_at_utc",
        "database",
        "file_count",
        "files",
        "format",
        "included_config",
        "schema_version",
        "source_directories",
        "total_byte_count",
    }
)


class BackupError(ValueError):
    """Raised when a local v1 backup or restore fails closed."""


def create_backup(
    destination: Path | str,
    *,
    paths: RuntimePaths,
    explicit_config: Path | str | None = None,
) -> dict[str, Any]:
    """Create a new content-verified backup directory atomically."""
    issues = inspect_path_safety(paths)
    if issues:
        raise BackupError("configured path safety failed: " + "; ".join(issues))
    final = _absolute_without_following(Path(destination))
    _validate_new_destination(final, paths)
    if not paths.sqlite_path.is_file():
        raise BackupError("operational SQLite database is not initialized")
    _reject_source_symlinks(paths)

    config: Path | None = None
    if explicit_config is not None:
        config = _absolute_without_following(Path(explicit_config))
        _reject_symlink_chain(config)
        if config.exists():
            if not config.is_file():
                raise BackupError("explicit configuration must be a regular non-symlink file")
            config = config.resolve()

    temporary = final.with_name(f".{final.name}.tmp-{uuid.uuid4().hex}")
    _reject_symlink_chain(temporary)
    if temporary.exists():
        raise BackupError("temporary backup destination unexpectedly exists")
    try:
        temporary.mkdir(parents=False)
        database_target = temporary / DATABASE_RELATIVE_PATH
        database_target.parent.mkdir()
        _backup_sqlite(paths.sqlite_path, database_target)

        source_directories = {role: f"state/{role}" for role, _attribute in _ROLE_PATHS}
        for role, attribute in _ROLE_PATHS:
            source = getattr(paths, attribute)
            target = temporary / source_directories[role]
            target.mkdir(parents=True)
            _copy_tree_verified(source, target)

        included_config: dict[str, str] | None = None
        if config is not None and config.exists():
            config_target = temporary / CONFIG_RELATIVE_PATH
            config_target.parent.mkdir()
            _copy_file_verified(config, config_target)
            included_config = {
                "relative_path": CONFIG_RELATIVE_PATH,
                "sha256": _sha256_file(config_target),
            }

        files = _inventory_payload_files(temporary)
        database_entry = next(
            item for item in files if item["relative_path"] == DATABASE_RELATIVE_PATH
        )
        manifest: dict[str, Any] = {
            "application_version": __version__,
            "created_at_utc": datetime.now(tz=UTC)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z"),
            "database": {
                "relative_path": DATABASE_RELATIVE_PATH,
                "sha256": database_entry["sha256"],
            },
            "file_count": len(files),
            "files": files,
            "format": BACKUP_FORMAT,
            "included_config": included_config,
            "schema_version": 1,
            "source_directories": source_directories,
            "total_byte_count": sum(int(item["size"]) for item in files),
        }
        _write_manifest(temporary / MANIFEST_NAME, manifest)
        _verify_backup_directory(temporary)
        os.rename(temporary, final)
    except Exception as exc:
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)
        if isinstance(exc, BackupError):
            raise
        raise BackupError(f"backup creation failed: {_safe_detail(exc)}") from exc

    return {
        "application_version": __version__,
        "backup_directory": str(final),
        "file_count": manifest["file_count"],
        "format": BACKUP_FORMAT,
        "state": "backup-created",
        "total_byte_count": manifest["total_byte_count"],
    }


def restore_backup(
    backup_directory: Path | str,
    *,
    paths: RuntimePaths,
) -> dict[str, Any]:
    """Strictly verify and restore a v1 backup into absent or empty state."""
    issues = inspect_path_safety(paths)
    if issues:
        raise BackupError("configured path safety failed: " + "; ".join(issues))
    backup = _absolute_without_following(Path(backup_directory))
    _reject_symlink_chain(backup)
    backup = backup.resolve()
    manifest = _verify_backup_directory(backup)
    _validate_restore_targets(paths)

    token = uuid.uuid4().hex
    staged_directories: dict[str, Path] = {}
    staged_database = paths.sqlite_path.with_name(f".{paths.sqlite_path.name}.restore-{token}")
    published: list[Path] = []
    created_parents: list[Path] = []
    target_states = {
        getattr(paths, attribute): "existing-empty"
        if getattr(paths, attribute).is_dir()
        else "absent"
        for _role, attribute in _ROLE_PATHS
    }
    preexisting_empty_targets = [
        target for target, state in target_states.items() if state == "existing-empty"
    ]
    try:
        required_parents = {
            paths.sqlite_path.parent,
            *(getattr(paths, attribute).parent for _role, attribute in _ROLE_PATHS),
        }
        for parent in sorted(required_parents, key=lambda item: len(item.parts)):
            created_parents.extend(_create_missing_parents(parent))
        for role, attribute in _ROLE_PATHS:
            target = getattr(paths, attribute)
            staged = target.with_name(f".{target.name}.restore-{token}")
            _reject_symlink_chain(staged)
            staged.mkdir(parents=False)
            source = backup / str(manifest["source_directories"][role])
            _copy_tree_verified(source, staged)
            staged_directories[role] = staged

        _copy_file_verified(backup / DATABASE_RELATIVE_PATH, staged_database)
        _verify_compatible_database(staged_database)

        for role, attribute in _ROLE_PATHS:
            target = getattr(paths, attribute)
            if target.exists():
                target.rmdir()
            os.rename(staged_directories[role], target)
            published.append(target)
        os.replace(staged_database, paths.sqlite_path)
        published.append(paths.sqlite_path)
        _verify_compatible_database(paths.sqlite_path)
    except Exception as exc:
        for staged in (*staged_directories.values(), staged_database):
            if staged.is_dir():
                shutil.rmtree(staged, ignore_errors=True)
            elif staged.exists():
                staged.unlink(missing_ok=True)
        for item in reversed(published):
            if item.is_dir():
                shutil.rmtree(item, ignore_errors=True)
            elif item.exists():
                item.unlink(missing_ok=True)
        for target in preexisting_empty_targets:
            if not target.exists():
                target.mkdir()
        for parent in reversed(created_parents):
            try:
                parent.rmdir()
            except OSError:
                pass
        if isinstance(exc, BackupError):
            raise
        message = f"restore failed without retained partial state: {_safe_detail(exc)}"
        raise BackupError(message) from exc

    return {
        "application_version": __version__,
        "backup_directory": str(backup),
        "database_path": str(paths.sqlite_path),
        "file_count": manifest["file_count"],
        "format": BACKUP_FORMAT,
        "restored_roles": [role for role, _attribute in _ROLE_PATHS],
        "state": "restore-complete",
    }


def verify_backup(backup_directory: Path | str) -> dict[str, Any]:
    """Verify a backup and return its validated manifest."""
    backup = _absolute_without_following(Path(backup_directory))
    _reject_symlink_chain(backup)
    return _verify_backup_directory(backup.resolve())


def _validate_new_destination(final: Path, paths: RuntimePaths) -> None:
    if final.exists():
        raise BackupError("backup destination must be new and non-existing")
    if final == Path(final.anchor):
        raise BackupError("backup destination must not be a filesystem root")
    _reject_symlink_chain(final)
    sources = (
        paths.storage_root,
        paths.sqlite_path,
        *(getattr(paths, attribute) for _role, attribute in _ROLE_PATHS),
    )
    for source in sources:
        if _is_relative_to(final, source.resolve()):
            raise BackupError("backup destination must not be inside persistent source state")
    if not final.parent.is_dir():
        raise BackupError("backup destination parent must already exist")


def _validate_restore_targets(paths: RuntimePaths) -> None:
    _reject_symlink_chain(paths.sqlite_path)
    if paths.sqlite_path.exists():
        raise BackupError("restore refuses to overwrite an operational database")
    for _role, attribute in _ROLE_PATHS:
        target = getattr(paths, attribute)
        _reject_symlink_chain(target)
        if target.exists():
            if not target.is_dir():
                raise BackupError(f"restore destination is not a directory: {target.name}")
            try:
                next(target.iterdir())
            except StopIteration:
                pass
            else:
                raise BackupError(f"restore destination is not empty: {target.name}")


def _create_missing_parents(path: Path) -> list[Path]:
    missing: list[Path] = []
    current = path
    while not current.exists():
        missing.append(current)
        if current.parent == current:
            raise BackupError("restore destination has no existing filesystem parent")
        current = current.parent
    _reject_symlink_chain(current)
    created: list[Path] = []
    for directory in reversed(missing):
        directory.mkdir()
        created.append(directory)
    return created


def _reject_source_symlinks(paths: RuntimePaths) -> None:
    for _role, attribute in _ROLE_PATHS:
        root = getattr(paths, attribute)
        _reject_symlink_chain(root)
        if not root.exists():
            continue
        for directory, directory_names, file_names in os.walk(root, followlinks=False):
            current = Path(directory)
            for name in [*directory_names, *file_names]:
                candidate = current / name
                if candidate.is_symlink():
                    raise BackupError(f"persistent source contains a symlink: {attribute}")


def _copy_tree_verified(source: Path, destination: Path) -> None:
    if not source.exists():
        return
    if not source.is_dir() or source.is_symlink():
        raise BackupError("persistent source must be a regular directory")
    for directory, directory_names, file_names in os.walk(source, followlinks=False):
        directory_names.sort()
        file_names.sort()
        current = Path(directory)
        relative = current.relative_to(source)
        included_directories = [
            name for name in directory_names if name.casefold() not in _EXCLUDED_DIRECTORY_NAMES
        ]
        directory_names[:] = included_directories
        for name in included_directories:
            child = current / name
            if child.is_symlink():
                raise BackupError("source symlink rejected")
            (destination / relative / name).mkdir()
        for name in file_names:
            if _exclude_file_name(name):
                continue
            if name.casefold() in _PROHIBITED_SENSITIVE_NAMES:
                raise BackupError("credential or browser-session file rejected")
            child = current / name
            if child.is_symlink() or not child.is_file():
                raise BackupError("source must contain regular non-symlink files")
            target = destination / relative / name
            target.parent.mkdir(parents=True, exist_ok=True)
            _copy_file_verified(child, target)


def _copy_file_verified(source: Path, destination: Path) -> None:
    before = source.stat(follow_symlinks=False)
    if source.is_symlink() or not source.is_file():
        raise BackupError("source file must be regular and non-symlink")
    digest = hashlib.sha256()
    size = 0
    destination.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as source_handle, destination.open("xb") as target_handle:
        while chunk := source_handle.read(1024 * 1024):
            target_handle.write(chunk)
            digest.update(chunk)
            size += len(chunk)
        target_handle.flush()
        os.fsync(target_handle.fileno())
    after = source.stat(follow_symlinks=False)
    identity_before = (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
    identity_after = (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
    if identity_before != identity_after or size != before.st_size:
        destination.unlink(missing_ok=True)
        raise BackupError("source file mutation detected during copying")
    if digest.hexdigest() != _sha256_file(destination):
        destination.unlink(missing_ok=True)
        raise BackupError("copied file checksum verification failed")


def _backup_sqlite(source: Path, destination: Path) -> None:
    _reject_symlink_chain(source)
    if source.is_symlink():
        raise BackupError("SQLite source symlink rejected")
    try:
        source_uri = source.resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(source_uri, uri=True)) as source_connection:
            with closing(sqlite3.connect(destination)) as destination_connection:
                source_connection.backup(destination_connection)
                destination_connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                mode = destination_connection.execute("PRAGMA journal_mode=DELETE").fetchone()
                if mode is None or str(mode[0]).lower() != "delete":
                    raise BackupError("SQLite backup could not be finalized without WAL sidecars")
                destination_connection.commit()
    except sqlite3.Error as exc:
        raise BackupError(f"SQLite backup API failed: {_safe_detail(exc)}") from exc
    _verify_compatible_database(destination)


def _verify_compatible_database(path: Path) -> None:
    try:
        verify_sqlite_file(path, quick=False)
        status = get_migration_status(path)
    except Exception as exc:
        raise BackupError(f"backed-up database verification failed: {_safe_detail(exc)}") from exc
    if not status.is_up_to_date or status.current_version != 5 or status.latest_version != 5:
        raise BackupError("backed-up database is not at the exact v1 migration state")


def _inventory_payload_files(root: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for directory, directory_names, file_names in os.walk(root, followlinks=False):
        directory_names.sort()
        file_names.sort()
        current = Path(directory)
        for name in [*directory_names, *file_names]:
            if (current / name).is_symlink():
                raise BackupError("backup staging tree contains a symlink")
        for name in file_names:
            path = current / name
            relative = path.relative_to(root).as_posix()
            if relative == MANIFEST_NAME:
                continue
            entries.append(
                {
                    "relative_path": relative,
                    "sha256": _sha256_file(path),
                    "size": path.stat().st_size,
                }
            )
    return sorted(entries, key=lambda item: str(item["relative_path"]))


def _verify_backup_directory(backup: Path) -> dict[str, Any]:
    if not backup.is_dir() or backup.is_symlink():
        raise BackupError("backup must be an existing non-symlink directory")
    _reject_symlink_tree(backup)
    manifest_path = backup / MANIFEST_NAME
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BackupError("backup manifest is missing or invalid JSON") from exc
    manifest = _validate_manifest(raw)
    actual = {item["relative_path"]: item for item in _inventory_payload_files(backup)}
    declared = {item["relative_path"]: item for item in manifest["files"]}
    if set(actual) != set(declared):
        raise BackupError("backup has unexpected or missing files")
    for relative in sorted(declared):
        expected = declared[relative]
        observed = actual[relative]
        if expected["size"] != observed["size"] or expected["sha256"] != observed["sha256"]:
            raise BackupError(f"backup content verification failed: {relative}")
        if relative != DATABASE_RELATIVE_PATH:
            _reject_executable_file(backup / relative)
    _verify_compatible_database(backup / DATABASE_RELATIVE_PATH)
    return manifest


def _validate_manifest(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != _MANIFEST_FIELDS:
        raise BackupError("backup manifest fields are not exact")
    if (
        raw["format"] != BACKUP_FORMAT
        or type(raw["schema_version"]) is not int
        or raw["schema_version"] != 1
    ):
        raise BackupError("backup format or schema version is incompatible")
    if raw["application_version"] != __version__:
        raise BackupError("backup application version is incompatible")
    try:
        if type(raw["created_at_utc"]) is not str:
            raise ValueError
        timestamp = raw["created_at_utc"]
        datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BackupError("backup creation timestamp is invalid") from exc
    if not timestamp.endswith("Z"):
        raise BackupError("backup creation timestamp must be UTC")

    expected_roles = {role: f"state/{role}" for role, _attribute in _ROLE_PATHS}
    if raw["source_directories"] != expected_roles:
        raise BackupError("backup source-directory roles are incompatible")
    database = raw["database"]
    if (
        not isinstance(database, dict)
        or set(database) != {"relative_path", "sha256"}
        or database["relative_path"] != DATABASE_RELATIVE_PATH
        or not _valid_sha(database["sha256"])
    ):
        raise BackupError("backup database declaration is invalid")
    included_config = raw["included_config"]
    if included_config is not None and (
        not isinstance(included_config, dict)
        or set(included_config) != {"relative_path", "sha256"}
        or included_config["relative_path"] != CONFIG_RELATIVE_PATH
        or not _valid_sha(included_config["sha256"])
    ):
        raise BackupError("backup configuration declaration is invalid")

    files = raw["files"]
    if not isinstance(files, list):
        raise BackupError("backup file inventory must be a list")
    normalized: list[dict[str, Any]] = []
    for item in files:
        if not isinstance(item, dict) or set(item) != {"relative_path", "sha256", "size"}:
            raise BackupError("backup file inventory entry is invalid")
        relative = _validate_relative_path(item["relative_path"])
        if type(item["size"]) is not int or item["size"] < 0 or not _valid_sha(item["sha256"]):
            raise BackupError("backup file size or checksum is invalid")
        normalized.append(
            {"relative_path": relative, "sha256": item["sha256"], "size": item["size"]}
        )
    if normalized != sorted(normalized, key=lambda item: item["relative_path"]):
        raise BackupError("backup file inventory ordering is not canonical")
    relative_paths = [item["relative_path"] for item in normalized]
    if len(relative_paths) != len(set(relative_paths)):
        raise BackupError("backup contains duplicate canonical relative paths")
    allowed_prefixes = tuple(f"state/{role}/" for role, _attribute in _ROLE_PATHS)
    allowed_exact = {DATABASE_RELATIVE_PATH}
    if included_config is not None:
        allowed_exact.add(CONFIG_RELATIVE_PATH)
    unsupported = [
        path
        for path in relative_paths
        if path not in allowed_exact and not path.startswith(allowed_prefixes)
    ]
    if unsupported:
        raise BackupError(
            "backup inventory contains an unsupported semantic path: " + ", ".join(unsupported)
        )
    by_path = {item["relative_path"]: item for item in normalized}
    if DATABASE_RELATIVE_PATH not in by_path:
        raise BackupError("backup database file is missing from inventory")
    if by_path[DATABASE_RELATIVE_PATH]["sha256"] != database["sha256"]:
        raise BackupError("backup database manifest checksum is inconsistent")
    if included_config is not None and (
        CONFIG_RELATIVE_PATH not in by_path
        or by_path[CONFIG_RELATIVE_PATH]["sha256"] != included_config["sha256"]
    ):
        raise BackupError("backup configuration manifest checksum is inconsistent")
    if (
        type(raw["file_count"]) is not int
        or type(raw["total_byte_count"]) is not int
        or raw["file_count"] != len(normalized)
        or raw["total_byte_count"] != sum(item["size"] for item in normalized)
    ):
        raise BackupError("backup manifest counts are inconsistent")
    raw["files"] = normalized
    return raw


def _write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    encoded = (
        json.dumps(manifest, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
            "utf-8"
        )
        + b"\n"
    )
    with path.open("xb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())


def _reject_symlink_tree(root: Path) -> None:
    _reject_symlink_chain(root)
    for directory, directory_names, file_names in os.walk(root, followlinks=False):
        current = Path(directory)
        for name in [*directory_names, *file_names]:
            if (current / name).is_symlink():
                raise BackupError("backup symlink rejected")


def _reject_symlink_chain(path: Path) -> None:
    current = _absolute_without_following(path)
    existing: list[Path] = []
    while True:
        if current.exists() or current.is_symlink():
            existing.append(current)
        if current.parent == current:
            break
        current = current.parent
    if any(item.is_symlink() for item in existing):
        raise BackupError("symlink path component rejected")


def _validate_relative_path(value: object) -> str:
    if type(value) is not str or not value or "\\" in value:
        raise BackupError("backup relative path is invalid")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or value != path.as_posix()
        or any(part in {"", ".", ".."} for part in path.parts)
        or (len(value) >= 2 and value[1] == ":")
    ):
        raise BackupError("backup path traversal rejected")
    return value


def _absolute_without_following(path: Path) -> Path:
    return Path(os.path.abspath(path))


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _exclude_file_name(name: str) -> bool:
    folded = name.casefold()
    if folded == ".env" or folded.startswith(".env."):
        return True
    return Path(folded).suffix in _EXCLUDED_FILE_SUFFIXES


def _reject_executable_file(path: Path) -> None:
    if path.suffix.casefold() in _PROHIBITED_EXECUTABLE_SUFFIXES:
        raise BackupError(f"executable file is prohibited in local v1 backup: {path.name}")
    with path.open("rb") as handle:
        prefix = handle.read(4)
    if prefix.startswith((b"MZ", b"\x7fELF", b"#!")):
        raise BackupError(f"executable content is prohibited in local v1 backup: {path.name}")


def _valid_sha(value: object) -> bool:
    return type(value) is str and _SHA256.fullmatch(value) is not None


def _safe_detail(exc: BaseException) -> str:
    text = str(exc).replace("\r", " ").replace("\n", " ").strip()
    return text[:500] or type(exc).__name__
