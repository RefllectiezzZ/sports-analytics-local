"""Offline tests for bounded HTTP downloads."""

from __future__ import annotations

import hashlib

import pytest

from sports_analytics.core.exceptions import (
    PermanentSourceError,
    RetryableSourceError,
    SourceNotFoundError,
)
from sports_analytics.sources.http import download_bounded_bytes
from tests.helpers_http import FakeClock, FakeHttpTransport

URL = "https://www.football-data.co.uk/mmz4281/2324/E0.csv"


def _download(
    transport: FakeHttpTransport,
    clock: FakeClock,
    *,
    maximum_bytes: int = 1024,
    maximum_retries: int = 0,
    last_request_monotonic: float | None = None,
    minimum_request_interval_seconds: float = 0.0,
) -> tuple[bytes, float]:
    result, last_request = download_bounded_bytes(
        url=URL,
        transport=transport,
        timeout_seconds=5.0,
        maximum_bytes=maximum_bytes,
        maximum_retries=maximum_retries,
        retry_backoff_base_seconds=0.5,
        retry_backoff_max_seconds=2.0,
        minimum_request_interval_seconds=minimum_request_interval_seconds,
        monotonic_clock=clock.monotonic,
        sleeper=clock.sleep,
        last_request_monotonic=last_request_monotonic,
    )
    return result.content, last_request


def test_download_returns_content_metadata_and_checksum() -> None:
    body = b"Div,Date\nE0,12/08/2023\n"
    transport = FakeHttpTransport(responses=[body])
    clock = FakeClock(start=100.0)

    result, last_request = download_bounded_bytes(
        url=URL,
        transport=transport,
        timeout_seconds=5.0,
        maximum_bytes=1024,
        maximum_retries=0,
        retry_backoff_base_seconds=0.5,
        retry_backoff_max_seconds=2.0,
        minimum_request_interval_seconds=0.0,
        monotonic_clock=clock.monotonic,
        sleeper=clock.sleep,
    )

    assert result.content == body
    assert result.metadata.status_code == 200
    assert result.metadata.content_type == "text/csv"
    assert result.metadata.content_length == len(body)
    assert result.metadata.final_url == URL
    assert result.checksum_sha256 == hashlib.sha256(body).hexdigest()
    assert last_request == 100.0
    assert transport.calls == [URL]
    assert clock.sleeps == []


def test_download_enforces_request_pacing_without_real_sleep() -> None:
    transport = FakeHttpTransport(responses=[b"Div,Date\n"])
    clock = FakeClock(start=100.0)

    content, last_request = _download(
        transport,
        clock,
        last_request_monotonic=96.5,
        minimum_request_interval_seconds=5.0,
    )

    assert content == b"Div,Date\n"
    assert clock.sleeps == [1.5]
    assert last_request == 101.5


def test_download_retries_retryable_failures_with_exponential_backoff() -> None:
    transport = FakeHttpTransport(
        responses=[
            RetryableSourceError("temporary outage"),
            RetryableSourceError("still temporary"),
            b"Div,Date\n",
        ]
    )
    clock = FakeClock(start=10.0)

    content, last_request = _download(transport, clock, maximum_retries=2)

    assert content == b"Div,Date\n"
    assert transport.calls == [URL, URL, URL]
    assert clock.sleeps == [0.5, 1.0]
    assert last_request == 11.5


def test_download_stops_retrying_after_retry_budget_is_exhausted() -> None:
    transport = FakeHttpTransport(
        responses=[
            RetryableSourceError("temporary outage"),
            RetryableSourceError("still temporary"),
        ]
    )
    clock = FakeClock()

    with pytest.raises(RetryableSourceError, match="still temporary"):
        _download(transport, clock, maximum_retries=1)

    assert transport.calls == [URL, URL]
    assert clock.sleeps == [0.5]


def test_download_does_not_retry_permanent_source_failure() -> None:
    transport = FakeHttpTransport(responses=[(403, b"forbidden", "text/plain"), b"unused"])
    clock = FakeClock()

    with pytest.raises(PermanentSourceError, match="permanent source HTTP failure"):
        _download(transport, clock, maximum_retries=3)

    assert transport.calls == [URL]
    assert clock.sleeps == []


def test_download_maps_not_found_responses() -> None:
    transport = FakeHttpTransport(responses=[(404, b"not found", "text/plain")])
    clock = FakeClock()

    with pytest.raises(SourceNotFoundError, match="HTTP 404"):
        _download(transport, clock, maximum_retries=3)

    assert transport.calls == [URL]


def test_download_retries_server_errors_from_transport() -> None:
    transport = FakeHttpTransport(responses=[(503, b"busy", "text/plain"), b"Div,Date\n"])
    clock = FakeClock()

    content, last_request = _download(transport, clock, maximum_retries=1)

    assert content == b"Div,Date\n"
    assert transport.calls == [URL, URL]
    assert clock.sleeps == [0.5]
    assert last_request == 1000.5


def test_download_rejects_response_larger_than_limit() -> None:
    transport = FakeHttpTransport(responses=[b"123456789"], chunk_size=3)
    clock = FakeClock()

    with pytest.raises(PermanentSourceError, match="exceeded maximum_download_bytes"):
        _download(transport, clock, maximum_bytes=8)


def test_download_rejects_html_content_type() -> None:
    transport = FakeHttpTransport(responses=[(200, b"not html", "application/xhtml+xml")])
    clock = FakeClock()

    with pytest.raises(PermanentSourceError, match="HTML Content-Type"):
        _download(transport, clock)


def test_download_rejects_html_payload_even_when_content_type_is_csv() -> None:
    transport = FakeHttpTransport(responses=[b"  <!doctype html><title>blocked</title>"])
    clock = FakeClock()

    with pytest.raises(PermanentSourceError, match="looks like HTML"):
        _download(transport, clock)


def test_download_rejects_non_positive_byte_limit_before_transport_call() -> None:
    transport = FakeHttpTransport(responses=[b"unused"])
    clock = FakeClock()

    with pytest.raises(PermanentSourceError, match="maximum_bytes must be positive"):
        _download(transport, clock, maximum_bytes=0)

    assert transport.calls == []
