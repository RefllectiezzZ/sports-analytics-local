"""Sport-agnostic Parquet write/verify helpers for snapshot datasets."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from sports_analytics.core.exceptions import SnapshotIntegrityError
from sports_analytics.snapshots.arrow import schema_fingerprint
from sports_analytics.snapshots.paths import is_absolute_path_text
from sports_analytics.snapshots.spec import SnapshotDatasetSuite

PARQUET_COMPRESSION = "zstd"


def write_parquet_file(path: Path, table: pa.Table) -> None:
    """Write one Parquet file with deterministic project options."""
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        table,
        where=path,
        compression=PARQUET_COMPRESSION,
        coerce_timestamps="us",
        allow_truncated_timestamps=False,
        use_dictionary=True,
        write_statistics=True,
    )


def verify_parquet_file(path: Path, *, expected_schema: pa.Schema, expected_rows: int) -> None:
    """Re-open a Parquet file and verify schema, metadata policy, and row count.

    Every Parquet resource is closed before returning or raising.
    """
    if path.is_symlink():
        msg = f"parquet file must not be a symlink: {path.name}"
        raise SnapshotIntegrityError(msg)
    if not path.is_file():
        msg = f"parquet file missing: {path.name}"
        raise SnapshotIntegrityError(msg)
    parquet_file: Any | None = None
    try:
        parquet_file = pq.ParquetFile(path)
        table = parquet_file.read()
        file_metadata = parquet_file.metadata
    except Exception as exc:  # noqa: BLE001 - corrupt files become integrity errors
        msg = f"failed to read parquet file {path.name}"
        raise SnapshotIntegrityError(msg) from exc
    finally:
        if parquet_file is not None:
            close = getattr(parquet_file, "close", None)
            if callable(close):
                close()

    _reject_disallowed_metadata(path.name, table.schema.metadata)
    parquet_metadata = getattr(file_metadata, "metadata", None)
    if isinstance(parquet_metadata, dict):
        _reject_disallowed_metadata(path.name, parquet_metadata)

    if table.num_rows != expected_rows:
        msg = (
            f"parquet row count mismatch for {path.name}: "
            f"expected {expected_rows}, found {table.num_rows}"
        )
        raise SnapshotIntegrityError(msg)
    if schema_fingerprint(table.schema) != schema_fingerprint(expected_schema):
        msg = f"parquet schema mismatch for {path.name}"
        raise SnapshotIntegrityError(msg)


def _reject_disallowed_metadata(
    filename: str,
    metadata: dict[bytes | str, bytes | str] | None,
) -> None:
    if not metadata:
        return
    for key, value in metadata.items():
        decoded_key = key.decode("utf-8", errors="replace") if isinstance(key, bytes) else str(key)
        if "pandas" in decoded_key.lower():
            msg = f"parquet file {filename} must not contain pandas metadata"
            raise SnapshotIntegrityError(msg)
        if decoded_key == "ARROW:schema":
            continue
        decoded_value = (
            value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value)
        )
        if _contains_absolute_path_metadata(decoded_key) or _contains_absolute_path_metadata(
            decoded_value
        ):
            msg = f"parquet file {filename} must not contain absolute path metadata"
            raise SnapshotIntegrityError(msg)


def _contains_absolute_path_metadata(text: str) -> bool:
    stripped = text.strip()
    if is_absolute_path_text(stripped):
        return True
    try:
        decoded = json.loads(stripped)
    except json.JSONDecodeError:
        return False
    return _json_contains_absolute_path(decoded)


def _json_contains_absolute_path(value: object) -> bool:
    if isinstance(value, str):
        return is_absolute_path_text(value.strip())
    if isinstance(value, list):
        return any(_json_contains_absolute_path(item) for item in value)
    if isinstance(value, dict):
        return any(
            _json_contains_absolute_path(key) or _json_contains_absolute_path(item)
            for key, item in value.items()
        )
    return False


def file_sha256_and_size(path: Path) -> tuple[str, int]:
    """Compute SHA-256 and byte size for a regular file."""
    hasher = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            hasher.update(chunk)
    return hasher.hexdigest(), size


def write_suite_parquet_files(
    directory: Path,
    *,
    suite: SnapshotDatasetSuite,
    tables: dict[str, pa.Table],
) -> dict[str, dict[str, object]]:
    """Write and verify every dataset of ``suite`` into ``directory``.

    Returns per-dataset file metadata used by the manifest writer.
    """
    missing = sorted(set(suite.dataset_names) - set(tables))
    unexpected = sorted(set(tables) - set(suite.dataset_names))
    if missing or unexpected:
        msg = f"snapshot tables mismatch missing={missing} unexpected={unexpected}"
        raise SnapshotIntegrityError(msg)

    file_meta: dict[str, dict[str, object]] = {}
    for descriptor in suite.descriptors:
        table = tables[descriptor.dataset_name]
        if schema_fingerprint(table.schema) != descriptor.schema_fingerprint:
            msg = f"table schema mismatch for dataset {descriptor.dataset_name}"
            raise SnapshotIntegrityError(msg)
        path = directory / descriptor.relative_filename
        write_parquet_file(path, table)
        verify_parquet_file(
            path,
            expected_schema=descriptor.schema,
            expected_rows=table.num_rows,
        )
        digest, size = file_sha256_and_size(path)
        file_meta[descriptor.dataset_name] = {
            "relative_filename": descriptor.relative_filename,
            "sha256": digest,
            "byte_count": size,
            "row_count": table.num_rows,
            "schema_fingerprint": descriptor.schema_fingerprint,
        }

    actual = {path.name for path in directory.iterdir() if path.is_file()}
    if actual != suite.filenames:
        msg = "unexpected files present before manifest write"
        raise SnapshotIntegrityError(msg)
    return file_meta
