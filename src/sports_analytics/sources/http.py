"""Bounded HTTPS GET transport for allowlisted football source downloads."""

from __future__ import annotations

import hashlib
import http.client
import socket
import ssl
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Protocol
from urllib.parse import urlparse

from sports_analytics.core.exceptions import (
    PermanentSourceError,
    RetryableSourceError,
    SourceNotFoundError,
)
from sports_analytics.sources.raw_store import RawSourceArtifact, RawSourceStore
from sports_analytics.sources.types import ALLOWED_FOOTBALL_DATA_HOST, DEFAULT_USER_AGENT

Clock = Callable[[], datetime]
MonotonicClock = Callable[[], float]
Sleeper = Callable[[float], None]
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


class HttpTransport(Protocol):
    """Minimal transport protocol for injectable HTTPS GET requests."""

    def get(
        self,
        url: str,
        *,
        timeout_seconds: float,
        headers: dict[str, str],
        maximum_redirects: int,
    ) -> HttpResponse:
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


class NonRedirectingHTTPRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Redirect handler that exposes 3xx responses without following them."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> None:
        del req, fp, code, msg, headers, newurl
        return None


class UrllibHttpTransport:
    """Standard-library HTTPS transport with redirect host enforcement."""

    def __init__(self, opener: urllib.request.OpenerDirector | None = None) -> None:
        self._opener = opener if opener is not None else _build_non_redirecting_opener()

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
                raw = self._opener.open(  # noqa: S310 - URL allowlisted above
                    request,
                    timeout=timeout_seconds,
                )
            except urllib.error.HTTPError as exc:
                status = int(exc.code)
                if status in REDIRECT_STATUSES:
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
            if metadata.status_code in REDIRECT_STATUSES:
                location = raw.headers.get("Location")
                raw.close()
                if location is None:
                    msg = "redirect response missing Location header"
                    raise PermanentSourceError(msg)
                redirects += 1
                if redirects > maximum_redirects:
                    msg = "too many redirects"
                    raise PermanentSourceError(msg)
                current_url = _resolve_redirect(current_url, location)
                continue
            if metadata.status_code >= 300:
                raw.close()
                _raise_for_status(metadata.status_code)

            body_handle = raw

            def _iter(handle: http.client.HTTPResponse = body_handle) -> Iterator[bytes]:
                while True:
                    chunk = handle.read(65_536)
                    if not chunk:
                        break
                    yield chunk

            return HttpResponse(
                metadata=metadata,
                _body_iter=_iter(),
                _close=body_handle.close,
            )


def _build_non_redirecting_opener() -> urllib.request.OpenerDirector:
    """Build the project-owned opener without cookies, auth, or automatic redirects."""
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()),
        NonRedirectingHTTPRedirectHandler(),
    )


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
    try:
        port = parsed.port
    except ValueError as exc:
        msg = "source URL port is invalid"
        raise PermanentSourceError(msg) from exc
    if port not in (None, 443):
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


@dataclass(slots=True)
class HttpRawStoreDownloadResult:
    artifact: RawSourceArtifact
    metadata: HttpResponseMetadata


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

    Returns ``(result, last_request_monotonic)``. ``last_request_monotonic`` is
    updated at each request start, including starts that fail before a response is
    returned. Retryable failures sleep deterministic exponential backoff; the next
    attempt still enforces ``minimum_request_interval_seconds`` from the failed
    request start, so short backoffs are topped up by pacing before the next start.
    Does not log cookies or bodies.
    """
    if maximum_bytes < 1:
        msg = "maximum_bytes must be positive"
        raise PermanentSourceError(msg)

    attempt = 0
    last_mono = last_request_monotonic
    while True:
        _sleep_for_request_pacing(
            last_mono=last_mono,
            minimum_request_interval_seconds=minimum_request_interval_seconds,
            monotonic_clock=monotonic_clock,
            sleeper=sleeper,
        )
        last_mono = monotonic_clock()
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
            sleeper(
                _retry_backoff_delay(
                    attempt=attempt,
                    retry_backoff_base_seconds=retry_backoff_base_seconds,
                    retry_backoff_max_seconds=retry_backoff_max_seconds,
                )
            )
            attempt += 1
            continue

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
            sleeper(
                _retry_backoff_delay(
                    attempt=attempt,
                    retry_backoff_base_seconds=retry_backoff_base_seconds,
                    retry_backoff_max_seconds=retry_backoff_max_seconds,
                )
            )
            attempt += 1
        finally:
            response.close()


def download_to_raw_store(
    *,
    url: str,
    transport: HttpTransport,
    store: RawSourceStore,
    source_name: str,
    retrieved_at: datetime,
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
) -> tuple[HttpRawStoreDownloadResult, float]:
    """Stream an HTTPS response directly into the raw store with retry pacing.

    Pacing/backoff composition matches ``download_bounded_bytes``: failed starts
    update ``last_request_monotonic``, then sleep backoff, then top up pacing as
    needed before the next request start. No random jitter is applied.
    """
    if maximum_bytes < 1:
        msg = "maximum_bytes must be positive"
        raise PermanentSourceError(msg)

    attempt = 0
    last_mono = last_request_monotonic
    while True:
        _sleep_for_request_pacing(
            last_mono=last_mono,
            minimum_request_interval_seconds=minimum_request_interval_seconds,
            monotonic_clock=monotonic_clock,
            sleeper=sleeper,
        )
        last_mono = monotonic_clock()
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
            sleeper(
                _retry_backoff_delay(
                    attempt=attempt,
                    retry_backoff_base_seconds=retry_backoff_base_seconds,
                    retry_backoff_max_seconds=retry_backoff_max_seconds,
                )
            )
            attempt += 1
            continue

        try:
            _validate_content_type(response.metadata.content_type)
            artifact = store.store_stream(
                source_name=source_name,
                source_url=url,
                chunk_iter=response.iter_chunks(),
                retrieved_at=retrieved_at,
                maximum_bytes=maximum_bytes,
                content_type=response.metadata.content_type,
                etag=response.metadata.etag,
                last_modified=response.metadata.last_modified,
            )
            return (
                HttpRawStoreDownloadResult(
                    artifact=artifact,
                    metadata=response.metadata,
                ),
                last_mono,
            )
        except RetryableSourceError:
            if attempt >= maximum_retries:
                raise
            sleeper(
                _retry_backoff_delay(
                    attempt=attempt,
                    retry_backoff_base_seconds=retry_backoff_base_seconds,
                    retry_backoff_max_seconds=retry_backoff_max_seconds,
                )
            )
            attempt += 1
        finally:
            response.close()


def _sleep_for_request_pacing(
    *,
    last_mono: float | None,
    minimum_request_interval_seconds: float,
    monotonic_clock: MonotonicClock,
    sleeper: Sleeper,
) -> None:
    if last_mono is None or minimum_request_interval_seconds <= 0:
        return
    elapsed = monotonic_clock() - last_mono
    remaining = minimum_request_interval_seconds - elapsed
    if remaining > 0:
        sleeper(remaining)


def _retry_backoff_delay(
    *,
    attempt: int,
    retry_backoff_base_seconds: float,
    retry_backoff_max_seconds: float,
) -> float:
    delay = retry_backoff_base_seconds * (2.0**attempt)
    return float(
        min(
            retry_backoff_max_seconds,
            delay,
        )
    )


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
