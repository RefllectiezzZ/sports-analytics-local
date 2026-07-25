"""Fake HTTPS transport and helpers for offline source tests."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field

from sports_analytics.core.exceptions import (
    PermanentSourceError,
    RetryableSourceError,
    SourceNotFoundError,
)
from sports_analytics.sources.http import HttpResponse, HttpResponseMetadata


@dataclass
class FakeHttpTransport:
    """Scripted HTTP transport that never touches the network."""

    responses: list[object] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)
    chunk_size: int = 8

    def get(
        self,
        url: str,
        *,
        timeout_seconds: float,
        headers: dict[str, str],
        maximum_redirects: int,
    ) -> HttpResponse:
        del timeout_seconds, headers, maximum_redirects
        self.calls.append(url)
        if not self.responses:
            msg = "no scripted HTTP responses remain"
            raise RetryableSourceError(msg)
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, tuple):
            status, body, content_type = item
        else:
            status, body, content_type = 200, item, "text/csv"
        if status in {404, 410}:
            msg = f"source resource not found (HTTP {status})"
            raise SourceNotFoundError(msg)
        if status in {408, 429} or status >= 500:
            msg = f"temporary source HTTP failure (HTTP {status})"
            raise RetryableSourceError(msg)
        if status >= 400:
            msg = f"permanent source HTTP failure (HTTP {status})"
            raise PermanentSourceError(msg)

        metadata = HttpResponseMetadata(
            status_code=status,
            content_type=content_type,
            content_length=len(body),
            etag='"etag-1"',
            last_modified="Wed, 01 Jan 2025 00:00:00 GMT",
            final_url=url,
        )

        def _iter() -> Iterator[bytes]:
            view = memoryview(body)
            for index in range(0, len(body), self.chunk_size):
                yield bytes(view[index : index + self.chunk_size])

        return HttpResponse(metadata=metadata, _body_iter=_iter(), _close=lambda: None)


class FakeClock:
    """Deterministic wall and monotonic clocks with injectable sleeper."""

    def __init__(self, start: float = 1_000.0) -> None:
        self.monotonic_value = start
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.monotonic_value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.monotonic_value += seconds
