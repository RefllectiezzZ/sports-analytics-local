"""Content-addressed immutable raw source storage."""

from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath

from sports_analytics.core.exceptions import PermanentSourceError, RetryableSourceError
from sports_analytics.data.types import validate_relative_snapshot_path, validate_sha256_checksum
from sports_analytics.snapshots.paths import resolve_raw_path, resolve_under_root


@dataclass(frozen=True, slots=True)
class RawSourceArtifact:
    """Typed metadata for a content-addressed raw source file."""

    source_name: str
    source_url: str
    checksum_sha256: str
    byte_count: int
    relative_path: str
    content_type: str | None
    retrieved_at: datetime
    etag: str | None
    last_modified: str | None
    encoding: str | None = None


class RawSourceStore:
    """Store and load immutable content-addressed raw source bytes."""

    def __init__(self, root_directory: Path) -> None:
        self._root = Path(root_directory)
        if self._root.is_symlink():
            msg = "configured raw directory must not be a symlink"
            raise PermanentSourceError(msg)
        self._root.mkdir(parents=True, exist_ok=True)

    def relative_path_for(self, *, source_name: str, checksum_sha256: str) -> str:
        """Return the POSIX relative path for a content-addressed raw artifact."""
        digest = validate_sha256_checksum(checksum_sha256)
        relative = PurePosixPath(source_name) / "sha256" / digest[:2] / f"{digest}.csv"
        return validate_relative_snapshot_path(relative.as_posix())

    def absolute_path_for(self, relative_path: str) -> Path:
        """Resolve a stored relative path safely under the raw root."""
        validated = validate_relative_snapshot_path(relative_path)
        return resolve_raw_path(self._root, validated)

    def store_bytes(
        self,
        *,
        source_name: str,
        source_url: str,
        content: bytes,
        retrieved_at: datetime,
        content_type: str | None = None,
        etag: str | None = None,
        last_modified: str | None = None,
        maximum_bytes: int | None = None,
        encoding: str | None = None,
    ) -> RawSourceArtifact:
        """Write content into the content-addressed store atomically."""
        if maximum_bytes is not None and len(content) > maximum_bytes:
            msg = "raw content exceeds maximum_download_bytes"
            raise PermanentSourceError(msg)
        digest = hashlib.sha256(content).hexdigest()
        relative = self.relative_path_for(source_name=source_name, checksum_sha256=digest)
        absolute = self.absolute_path_for(relative)
        absolute.parent.mkdir(parents=True, exist_ok=True)

        if absolute.exists():
            self._verify_existing(absolute, expected_digest=digest, expected_size=len(content))
            return RawSourceArtifact(
                source_name=source_name,
                source_url=source_url,
                checksum_sha256=digest,
                byte_count=len(content),
                relative_path=relative,
                content_type=content_type,
                retrieved_at=retrieved_at,
                etag=etag,
                last_modified=last_modified,
                encoding=encoding,
            )

        fd, temp_name = tempfile.mkstemp(
            prefix=f".{digest}.",
            suffix=".tmp",
            dir=str(absolute.parent),
        )
        temp_path = Path(temp_name)
        primary_error: BaseException | None = None
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            # Close before rename so Windows can replace/rename reliably.
            if absolute.exists():
                self._verify_existing(absolute, expected_digest=digest, expected_size=len(content))
                temp_path.unlink(missing_ok=True)
            else:
                os.replace(temp_path, absolute)
        except BaseException as exc:
            primary_error = exc
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
            if isinstance(exc, PermanentSourceError):
                raise
            msg = "failed to store raw source artifact"
            raise PermanentSourceError(msg) from exc
        finally:
            if primary_error is None and temp_path.exists() and not absolute.exists():
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass

        return RawSourceArtifact(
            source_name=source_name,
            source_url=source_url,
            checksum_sha256=digest,
            byte_count=len(content),
            relative_path=relative,
            content_type=content_type,
            retrieved_at=retrieved_at,
            etag=etag,
            last_modified=last_modified,
            encoding=encoding,
        )

    def store_stream(
        self,
        *,
        source_name: str,
        source_url: str,
        chunk_iter: Iterable[bytes],
        retrieved_at: datetime,
        maximum_bytes: int,
        content_type: str | None = None,
        etag: str | None = None,
        last_modified: str | None = None,
        encoding: str | None = None,
    ) -> RawSourceArtifact:
        """Stream chunks into the content-addressed store without buffering the body."""
        if maximum_bytes < 1:
            msg = "maximum_bytes must be positive"
            raise PermanentSourceError(msg)
        source_relative = validate_relative_snapshot_path(PurePosixPath(source_name).as_posix())
        staging_relative = validate_relative_snapshot_path(
            (PurePosixPath(source_relative) / ".tmp").as_posix()
        )
        staging_dir = resolve_under_root(
            self._root,
            staging_relative,
            expect_file=False,
            error_type=PermanentSourceError,
        )
        staging_dir.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=".stream-",
            suffix=".tmp",
            dir=str(staging_dir),
        )
        temp_path = Path(temp_name)
        hasher = hashlib.sha256()
        total = 0
        prefix = bytearray()
        try:
            with os.fdopen(fd, "wb") as handle:
                for chunk in chunk_iter:
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > maximum_bytes:
                        msg = "raw content exceeds maximum_download_bytes"
                        raise PermanentSourceError(msg)
                    hasher.update(chunk)
                    if len(prefix) < 512:
                        prefix.extend(chunk[: 512 - len(prefix)])
                    handle.write(chunk)
                _reject_html_payload_prefix(bytes(prefix))
                handle.flush()
                os.fsync(handle.fileno())

            digest = hasher.hexdigest()
            relative = self.relative_path_for(source_name=source_name, checksum_sha256=digest)
            absolute = self.absolute_path_for(relative)
            absolute.parent.mkdir(parents=True, exist_ok=True)
            if absolute.exists():
                self._verify_existing(absolute, expected_digest=digest, expected_size=total)
                temp_path.unlink(missing_ok=True)
            else:
                os.replace(temp_path, absolute)
        except BaseException as exc:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
            if isinstance(exc, (PermanentSourceError, RetryableSourceError)):
                raise
            msg = "failed to store raw source artifact"
            raise PermanentSourceError(msg) from exc

        return RawSourceArtifact(
            source_name=source_name,
            source_url=source_url,
            checksum_sha256=digest,
            byte_count=total,
            relative_path=relative,
            content_type=content_type,
            retrieved_at=retrieved_at,
            etag=etag,
            last_modified=last_modified,
            encoding=encoding,
        )

    def load_verified(
        self,
        *,
        source_name: str,
        checksum_sha256: str,
        source_url: str,
        retrieved_at: datetime,
    ) -> tuple[RawSourceArtifact, bytes]:
        """Load and verify a cached content-addressed artifact."""
        digest = validate_sha256_checksum(checksum_sha256)
        relative = self.relative_path_for(source_name=source_name, checksum_sha256=digest)
        absolute = self.absolute_path_for(relative)
        if not absolute.exists():
            msg = "cached raw source artifact is missing"
            raise PermanentSourceError(msg)
        self._reject_unsafe_file(absolute)
        content = absolute.read_bytes()
        actual = hashlib.sha256(content).hexdigest()
        if actual != digest:
            msg = "cached raw source artifact checksum mismatch"
            raise PermanentSourceError(msg)
        artifact = RawSourceArtifact(
            source_name=source_name,
            source_url=source_url,
            checksum_sha256=digest,
            byte_count=len(content),
            relative_path=relative,
            content_type=None,
            retrieved_at=retrieved_at,
            etag=None,
            last_modified=None,
            encoding=None,
        )
        return artifact, content

    def _verify_existing(self, path: Path, *, expected_digest: str, expected_size: int) -> None:
        self._reject_unsafe_file(path)
        content = path.read_bytes()
        if len(content) != expected_size:
            msg = "existing raw artifact size does not match content"
            raise PermanentSourceError(msg)
        actual = hashlib.sha256(content).hexdigest()
        if actual != expected_digest:
            msg = "existing raw artifact content does not match checksum path"
            raise PermanentSourceError(msg)

    @staticmethod
    def _reject_unsafe_file(path: Path) -> None:
        if path.is_symlink():
            msg = "raw artifact must not be a symlink"
            raise PermanentSourceError(msg)
        if not path.is_file():
            msg = "raw artifact must be a regular file"
            raise PermanentSourceError(msg)


def _reject_html_payload_prefix(prefix: bytes) -> None:
    leading = prefix.lstrip()[:64].lower()
    if leading.startswith(b"<!doctype html") or leading.startswith(b"<html"):
        msg = "response body looks like HTML, not CSV"
        raise PermanentSourceError(msg)
