"""Content-addressed raw capture store for minimized bookmaker diagnostics."""

from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath

from sports_analytics.core.exceptions import PermanentSourceError
from sports_analytics.data.types import validate_relative_snapshot_path, validate_sha256_checksum
from sports_analytics.snapshots.paths import resolve_raw_path


@dataclass(frozen=True, slots=True)
class BookmakerRawCapture:
    """Typed metadata for a content-addressed bookmaker capture."""

    source_name: str
    source_url: str | None
    capture_kind: str
    checksum_sha256: str
    byte_count: int
    relative_path: str
    retrieved_at: datetime
    extension: str


class BookmakerRawCaptureStore:
    """Store minimized provider JSON/DOM captures without browser profiles."""

    def __init__(self, root_directory: Path, *, maximum_bytes: int = 2_097_152) -> None:
        self._root = Path(root_directory)
        if self._root.is_symlink():
            msg = "configured raw directory must not be a symlink"
            raise PermanentSourceError(msg)
        if maximum_bytes < 1:
            msg = "maximum_bytes must be positive"
            raise PermanentSourceError(msg)
        self._maximum_bytes_default = maximum_bytes
        self._root.mkdir(parents=True, exist_ok=True)

    def relative_path_for(
        self,
        *,
        source_name: str,
        checksum_sha256: str,
        extension: str,
    ) -> str:
        digest = validate_sha256_checksum(checksum_sha256)
        if extension not in {"json", "txt", "meta"}:
            msg = f"unsupported capture extension: {extension}"
            raise PermanentSourceError(msg)
        relative = PurePosixPath(source_name) / "sha256" / digest[:2] / f"{digest}.{extension}"
        return validate_relative_snapshot_path(relative.as_posix())

    def store_text(
        self,
        *,
        source_name: str,
        capture_kind: str,
        content: str,
        retrieved_at: datetime,
        extension: str,
        maximum_bytes: int | None = None,
        source_url: str | None = None,
    ) -> BookmakerRawCapture:
        payload = content.encode("utf-8")
        limit = self._maximum_bytes_default if maximum_bytes is None else maximum_bytes
        if len(payload) > limit:
            msg = "raw capture exceeds bounded capture size"
            raise PermanentSourceError(msg)
        if ".." in source_name or source_name.startswith(("/", "\\")):
            msg = "source_name path traversal rejected"
            raise PermanentSourceError(msg)
        digest = hashlib.sha256(payload).hexdigest()
        relative = self.relative_path_for(
            source_name=source_name,
            checksum_sha256=digest,
            extension=extension,
        )
        absolute = resolve_raw_path(self._root, relative)
        if absolute.is_symlink():
            msg = "raw capture must not be a symlink"
            raise PermanentSourceError(msg)
        absolute.parent.mkdir(parents=True, exist_ok=True)
        if absolute.exists():
            existing = absolute.read_bytes()
            if hashlib.sha256(existing).hexdigest() != digest:
                msg = "existing raw capture content does not match checksum path"
                raise PermanentSourceError(msg)
        else:
            fd, temp_name = tempfile.mkstemp(
                prefix=f".{digest}.",
                suffix=".tmp",
                dir=str(absolute.parent),
            )
            temp_path = Path(temp_name)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp_path, absolute)
            except BaseException:
                temp_path.unlink(missing_ok=True)
                raise
        return BookmakerRawCapture(
            source_name=source_name,
            source_url=source_url,
            capture_kind=capture_kind,
            checksum_sha256=digest,
            byte_count=len(payload),
            relative_path=relative,
            retrieved_at=retrieved_at,
            extension=extension,
        )
