"""Bounded HTTPS GET transport for allowlisted football source downloads."""

from __future__ import annotations

import socket
import ssl
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Protocol
from urllib.parse import urlparse

from sports_analytics.core.exceptions import PermanentSourceError, RetryableSourceError, SourceNotFoundError
from sports_analytics.sources.types import ALLOWED_FOOTBALL_DATA_HOST, DEFAULT_USER_AGENT

Clock = Callable[[], datetime]
MonotonicClock = Callable[[], float]
Sleeper = Callable[[float], None]


class HttpTransport(Protocol):
    """Minimal transport protocol for injectable HTTPS GET requests."""

    def get(
        self,
        url: str,
        *,
        timeout_seconds: float,
        headers: dict[str, str],
        maximum_redirects: int,
    ) -> "HttpResponse":
        """Perform one GET and return a closable response."""


@dataclass(frozen=True, slots=True)
class HttpResponseMetadata:
    status_code: int
    content_type: str | None
    content_length: int | None
    etag: str | None
    last_modified: str | None
    final_url: str


@dataclass(slots=True)
class HttpResponse:
    metadata: HttpResponseMetadata
    _body_iter: Iterator[bytes]
    _close: Callable[[], None]
    _closed: bool = False

    def iter_chunks(self, chunk_size: int = 65_536) -> Iterator[bytes]:
        if self._closed:
            msg = "HTTP response is closed"
            raise PermanentSourceError(msg)
        yield from self._body_iter

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._close()


class UrllibHttpTransport:
    """Standard-library HTTPS transport with redirect host enforcement."""

    def get(
        self,
        url: str,
        *,
        timeout_seconds: float,
        headers: dict[str, str],
        maximum_redirects: int,
    ) -> HttpResponse:
        current_url = url
        redirects = 0
        while True:
            _validate_allowed_url(current_url)
            request = urllib.request.Request(
                current_url,
                method="GET",
                headers=headers,
            )
            try:
                raw = urllib.request.urlopen(  # noqa: S310 - URL allowlisted above
                    request,
                    timeout=timeout_seconds,
                    context=ssl.create_default_context(),
                )
            except urllib.error.HTTPError as exc:
                status = int(exc.code)
                if status in {301, 302, 303, 307, 308}:
                    location = exc.headers.get("Location")
                    exc.close()
                    if location is None:
                        msg = "redirect response missing Location header"
                        raise PermanentSourceError(msg) from exc
                    redirects += 1
                    if redirects > maximum_redirects:
                        msg = "too many redirects"
                        raise PermanentSourceError(msg) from exc
                    current_url = _resolve_redirect(current_url, location)
                    continue
                body = b""
                try:
                    body = exc.read(1024)
                finally:
                    exc.close()
                del body
                _raise_for_status(status)
                raise  # pragma: no cover - _raise_for_status always raises
            except TimeoutError as exc:
                msg = "HTTPS request timed out"
                raise RetryableSourceError(msg) from exc
            except ssl.SSLError as exc:
                msg = "TLS verification or handshake failed"
                raise RetryableSourceError(msg) from exc
            except socket.timeout as exc:
                msg = "HTTPS request timed out"
                raise RetryableSourceError(msg) from exc
            except urllib.error.URLError as exc:
                reason = exc.reason
                if isinstance(reason, (TimeoutError, socket.timeout, ConnectionResetError)):
                    msg = "temporary network failure during HTTPS request"
                    raise RetryableSourceError(msg) from exc
                if isinstance(reason, socket.gaierror):
                    msg = "temporary DNS failure during HTTPS request"
                    raise RetryableSourceError(msg) from exc
                msg = "HTTPS request failed"
                raise RetryableSourceError(msg) from exc
            except ConnectionResetError as exc:
                msg = "connection reset during HTTPS request"
                raise RetryableSourceError(msg) from exc

            final_url = str(raw.geturl())
            _validate_allowed_url(final_url)
            headers_map = {key.lower(): value for key, value in raw.headers.items()}
            content_length_raw = headers_map.get("content-length")
            content_length: int | None
            if content_length_raw is None:
                content_length = None
            else:
                try:
                    content_length = int(content_length_raw)
                except ValueError:
                    content_length = None
            metadata = HttpResponseMetadata(
                status_code=int(raw.status),
                content_type=headers_map.get("content-type"),
                content_length=content_length,
                etag=headers_map.get("etag"),
                last_modified=headers_map.get("last-modified"),
                final_url=final_url,
            )
            if metadata.status_code >= 300:
                raw.close()
                _raise_for_status(metadata.status_code)

            def _iter() -> Iterator[bytes]:
                while True:
                    chunk = raw.read(65_536)
                    if not chunk:
                        break
                    yield chunk

            return HttpResponse(metadata=metadata, _body_iter=_iter(), _close=raw.close)


def _validate_allowed_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https":
        msg = "only HTTPS URLs are permitted"
        raise PermanentSourceError(msg)
    if parsed.username is not None or parsed.password is not None:
        msg = "credentials in source URLs are not permitted"
        raise PermanentSourceError(msg)
    host = (parsed.hostname or "").lower()
    if host != ALLOWED_FOOTBALL_DATA_HOST:
        msg = f"host {host!r} is not on the football-data allowlist"
        raise PermanentSourceError(msg)
    if parsed.port not in (None, 443):
        msg = "non-default HTTPS ports are not permitted"
        raise PermanentSourceError(msg)


def _resolve_redirect(current_url: str, location: str) -> str:
    from urllib.parse import urljoin

    next_url = urljoin(current_url, location)
    _validate_allowed_url(next_url)
    return next_url


def _raise_for_status(status: int) -> None:
    if status in {404, 410}:
        msg = f"source resource not found (HTTP {status})"
        raise SourceNotFoundError(msg)
    if status in {408, 429} or status >= 500:
        msg = f"temporary source HTTP failure (HTTP {status})"
        raise RetryableSourceError(msg)
    if status >= 400:
        msg = f"permanent source HTTP failure (HTTP {status})"
        raise PermanentSourceError(msg)
    msg = f"unexpected HTTP status {status}"
    raise PermanentSourceError(msg)


@dataclass(slots=True)
class HttpDownloadResult:
    content: bytes
    metadata: HttpResponseMetadata
    checksum_sha256: str


def download_bounded_bytes(
    *,
    url: str,
    transport: HttpTransport,
    timeout_seconds: float,
    maximum_bytes: int,
    maximum_retries: int,
    retry_backoff_base_seconds: float,
    retry_backoff_max_seconds: float,
    minimum_request_interval_seconds: float,
    monotonic_clock: MonotonicClock,
    sleeper: Sleeper,
    last_request_monotonic: float | None = None,
    user_agent: str = DEFAULT_USER_AGENT,
    maximum_redirects: int = 3,
) -> tuple[HttpDownloadResult, float]:
    """Download a bounded HTTPS body with deterministic retries and pacing.

    Returns ``(result, last_request_monotonic)``. Does not log cookies or bodies.
    """
    import hashlib

    if maximum_bytes < 1:
        msg = "maximum_bytes must be positive"
        raise PermanentSourceError(msg)

    attempt = 0
    last_mono = last_request_monotonic
    while True:
        if last_mono is not None and minimum_request_interval_seconds > 0:
            elapsed = monotonic_clock() - last_mono
            remaining = minimum_request_interval_seconds - elapsed
            if remaining > 0:
                sleeper(remaining)
        try:
            response = transport.get(
                url,
                timeout_seconds=timeout_seconds,
                headers={"User-Agent": user_agent, "Accept": "text/csv,text/plain,*/*"},
                maximum_redirects=maximum_redirects,
            )
        except RetryableSourceError:
            if attempt >= maximum_retries:
                raise
            delay = min(
                retry_backoff_max_seconds,
                retry_backoff_base_seconds * (2**attempt),
            )
            sleeper(delay)
            attempt += 1
            continue

        last_mono = monotonic_clock()
        hasher = hashlib.sha256()
        chunks: list[bytes] = []
        total = 0
        try:
            _validate_content_type(response.metadata.content_type)
            for chunk in response.iter_chunks():
                if not chunk:
                    continue
                total += len(chunk)
                if total > maximum_bytes:
                    msg = f"response exceeded maximum_download_bytes ({maximum_bytes})"
                    raise PermanentSourceError(msg)
                hasher.update(chunk)
                chunks.append(chunk)
            content = b"".join(chunks)
            _reject_html_payload(content)
            return (
                HttpDownloadResult(
                    content=content,
                    metadata=response.metadata,
                    checksum_sha256=hasher.hexdigest(),
                ),
                last_mono,
            )
        except RetryableSourceError:
            if attempt >= maximum_retries:
                raise
            delay = min(
                retry_backoff_max_seconds,
                retry_backoff_base_seconds * (2**attempt),
            )
            sleeper(delay)
            attempt += 1
        finally:
            response.close()


def _validate_content_type(content_type: str | None) -> None:
    if content_type is None:
        return
    lowered = content_type.split(";", 1)[0].strip().lower()
    allowed = {"text/csv", "text/plain", "application/octet-stream"}
    if lowered not in allowed and not lowered.startswith("text/"):
        # Be strict for clearly wrong types such as text/html.
        if "html" in lowered:
            msg = "HTML Content-Type is not accepted for CSV downloads"
            raise PermanentSourceError(msg)


def _reject_html_payload(content: bytes) -> None:
    leading = content.lstrip()[:64].lower()
    if leading.startswith(b"<!doctype html") or leading.startswith(b"<html"):
        msg = "response body looks like HTML, not CSV"
        raise PermanentSourceError(msg)


def parse_http_last_modified(value: str | None) -> datetime | None:
    """Parse an HTTP Last-Modified header when present; never replaces observation time."""
    if value is None:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
