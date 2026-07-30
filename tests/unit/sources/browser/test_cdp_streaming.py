"""Deterministic passive CDP streaming harness with invented frame bytes."""

from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sports_analytics.core.exceptions import PermanentSourceError
from sports_analytics.sources.browser.cdp_streaming import (
    ApprovedStreamingResponse,
    CdpStreamingLimits,
    PassiveCdpStreamingObserver,
)
from sports_analytics.sources.browser.contracts import BrowserStreamingState
from sports_analytics.sources.browser.grpc_web_stream import IncrementalGrpcWebLimits

NOW = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)
URL = "https://stream.example/rpc"


def _frame(payload: bytes, *, trailer: bool = False) -> bytes:
    return bytes([0x80 if trailer else 0x00]) + len(payload).to_bytes(4, "big") + payload


def _encoded(payload: bytes) -> str:
    return base64.b64encode(payload).decode("ascii")


class _Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now

    def advance(self, milliseconds: int) -> None:
        self.now += timedelta(milliseconds=milliseconds)


class _CdpSession:
    def __init__(
        self,
        *,
        buffered_data: str = "",
        unsupported: bool = False,
        detach_error: bool = False,
    ) -> None:
        self.buffered_data = buffered_data
        self.unsupported = unsupported
        self.detach_error = detach_error
        self.callbacks: dict[str, object] = {}
        self.send_calls: list[tuple[str, dict[str, object] | None]] = []
        self.detached = False

    def send(self, method: str, params=None):
        self.send_calls.append((method, params))
        if method == "Network.streamResourceContent":
            if self.unsupported:
                raise RuntimeError("Protocol error: method wasn't found")
            return {"bufferedData": self.buffered_data}
        assert method in {"Network.enable", "Page.enable"}
        return {}

    def on(self, event: str, callback) -> None:
        self.callbacks[event] = callback

    def emit(self, event: str, payload: dict[str, object]) -> None:
        callback = self.callbacks[event]
        callback(payload)  # type: ignore[operator]

    def detach(self) -> None:
        self.detached = True
        if self.detach_error:
            raise RuntimeError("synthetic detach failure")


def _classify(url: str) -> ApprovedStreamingResponse:
    if url != URL:
        raise PermanentSourceError("unapproved synthetic route")
    return ApprovedStreamingResponse(
        hostname="stream.example",
        sanitized_path_hash=hashlib.sha256(b"/rpc").hexdigest(),
        approved_route_id="invented-rpc",
        metadata_only=False,
    )


def _observer(
    session: _CdpSession,
    *,
    clock: _Clock | None = None,
    diagnostic_directory: Path | None = None,
    limits: CdpStreamingLimits | None = None,
) -> PassiveCdpStreamingObserver:
    observer = PassiveCdpStreamingObserver(
        session=session,
        classify_response=_classify,
        page_route_id=lambda: "football-prematch",
        diagnostic_directory=diagnostic_directory,
        clock=clock or _Clock(),
        limits=limits,
        capture_kind="invented-grpc-web-frame",
    )
    observer.attach()
    return observer


def _emit_candidate(session: _CdpSession, *, duplicate_response: bool = False) -> None:
    session.emit(
        "Network.requestWillBeSent",
        {
            "requestId": "ephemeral-1",
            "request": {"url": URL, "method": "POST", "headers": {"secret": "ignored"}},
        },
    )
    response = {
        "requestId": "ephemeral-1",
        "type": "Fetch",
        "response": {
            "url": URL,
            "status": 200,
            "mimeType": "application/grpc-web",
            "headers": {"secret": "ignored"},
        },
    }
    session.emit("Network.responseReceived", response)
    if duplicate_response:
        session.emit("Network.responseReceived", response)


def _emit_data(
    session: _CdpSession,
    payload: bytes,
    *,
    timestamp: str = "1.0",
) -> None:
    session.emit(
        "Network.dataReceived",
        {
            "requestId": "ephemeral-1",
            "data": _encoded(payload),
            "timestamp": timestamp,
            "encodedDataLength": len(payload),
        },
    )


def test_supported_buffered_data_retains_complete_frame_and_safe_fingerprint(
    tmp_path: Path,
) -> None:
    session = _CdpSession(buffered_data=_encoded(_frame(b"\x08\x01")))
    observer = _observer(session, diagnostic_directory=tmp_path)
    _emit_candidate(session)
    observer.drain()
    _emit_data(session, _frame(b"grpc-status: 0\r\n", trailer=True))
    session.emit("Network.loadingFinished", {"requestId": "ephemeral-1"})
    observer.drain()
    outcomes = observer.close()

    assert len(outcomes) == 1
    summary = outcomes[0].summary
    assert summary.state is BrowserStreamingState.RPC_COMPLETE
    assert summary.http_response_completed is True
    assert summary.logical_rpc_completed is True
    assert summary.data_frame_count == 1
    assert summary.trailer_frame_count == 1
    assert len(summary.protobuf_wire_fingerprints) == 1
    assert len(outcomes[0].diagnostics) == 2
    assert all(item.capture_unit == "complete-streaming-frame" for item in outcomes[0].diagnostics)
    assert all("ephemeral-1" not in repr(item) for item in outcomes[0].diagnostics)
    assert session.detached is True


def test_data_received_before_consumer_scheduling_and_no_buffered_data() -> None:
    session = _CdpSession()
    observer = _observer(session)
    _emit_candidate(session)
    _emit_data(session, _frame(b"\x08\x01"))
    observer.drain()
    outcome = observer.close()[0]
    assert outcome.summary.data_frame_count == 1
    assert outcome.summary.state is BrowserStreamingState.RPC_OPEN
    assert outcome.summary.http_response_completed is False


def test_runtime_method_unsupported_is_explicit_and_finite_path_can_continue() -> None:
    session = _CdpSession(unsupported=True)
    observer = _observer(session)
    _emit_candidate(session)
    observer.drain()
    outcome = observer.close()[0]
    assert outcome.summary.observation_supported is False
    assert outcome.summary.state is BrowserStreamingState.UNSUPPORTED
    assert outcome.failure_code == "streaming-body-observation-unsupported"


def test_duplicate_response_and_data_events_are_idempotent() -> None:
    session = _CdpSession()
    observer = _observer(session)
    _emit_candidate(session, duplicate_response=True)
    observer.drain()
    framed = _frame(b"\x08\x01")
    _emit_data(session, framed, timestamp="2.0")
    _emit_data(session, framed, timestamp="2.0")
    observer.drain()
    outcome = observer.close()[0]
    stream_calls = [
        item for item in session.send_calls if item[0] == "Network.streamResourceContent"
    ]
    assert len(stream_calls) == 1
    assert outcome.summary.data_frame_count == 1
    assert outcome.summary.data_chunk_count == 1
    assert outcome.summary.duplicate_data_event_count == 1


def test_buffered_data_repeated_by_first_data_event_is_not_counted_twice() -> None:
    framed = _frame(b"\x08\x01")
    session = _CdpSession(buffered_data=_encoded(framed))
    observer = _observer(session)
    _emit_candidate(session)
    observer.drain()
    _emit_data(session, framed, timestamp="2.0")
    observer.drain()

    outcome = observer.close()[0]
    assert outcome.summary.data_frame_count == 1
    assert outcome.summary.data_chunk_count == 1
    assert outcome.summary.retained_byte_count == len(framed)
    assert outcome.summary.deduplicated_overlap_byte_count == len(framed)


def test_missing_data_field_is_accounted_without_rejecting_complete_buffered_frame() -> None:
    framed = _frame(b"\x08\x01")
    session = _CdpSession(buffered_data=_encoded(framed))
    observer = _observer(session)
    _emit_candidate(session)
    observer.drain()
    session.emit(
        "Network.dataReceived",
        {
            "requestId": "ephemeral-1",
            "timestamp": "2.0",
            "encodedDataLength": len(framed),
        },
    )
    observer.drain()

    outcome = observer.close()[0]
    assert outcome.summary.data_frame_count == 1
    assert outcome.summary.missing_data_event_count == 1
    assert outcome.failure_code is None


def test_redirect_replaces_request_generation_and_does_not_correlate_old_response() -> None:
    session = _CdpSession()
    observer = _observer(session)
    session.emit(
        "Network.requestWillBeSent",
        {
            "requestId": "ephemeral-1",
            "loaderId": "loader-old",
            "request": {"url": URL, "method": "POST"},
        },
    )
    session.emit(
        "Network.requestWillBeSent",
        {
            "requestId": "ephemeral-1",
            "loaderId": "loader-new",
            "redirectResponse": {"status": 302},
            "request": {"url": URL, "method": "POST"},
        },
    )
    session.emit(
        "Network.responseReceived",
        {
            "requestId": "ephemeral-1",
            "loaderId": "loader-old",
            "type": "Fetch",
            "response": {
                "url": URL,
                "status": 200,
                "mimeType": "application/grpc-web",
            },
        },
    )
    observer.drain()
    assert observer.outcomes == ()

    session.emit(
        "Network.responseReceived",
        {
            "requestId": "ephemeral-1",
            "loaderId": "loader-new",
            "type": "Fetch",
            "response": {
                "url": URL,
                "status": 200,
                "mimeType": "application/grpc-web",
            },
        },
    )
    _emit_data(session, _frame(b"\x08\x01"))
    observer.drain()
    assert observer.close()[0].summary.data_frame_count == 1


def test_main_frame_replacement_closes_stream_from_prior_loader() -> None:
    session = _CdpSession()
    observer = _observer(session)
    session.emit(
        "Page.frameNavigated",
        {"frame": {"id": "main", "loaderId": "loader-old"}},
    )
    session.emit(
        "Network.requestWillBeSent",
        {
            "requestId": "ephemeral-1",
            "loaderId": "loader-old",
            "request": {"url": URL, "method": "POST"},
        },
    )
    session.emit(
        "Network.responseReceived",
        {
            "requestId": "ephemeral-1",
            "loaderId": "loader-old",
            "type": "Fetch",
            "response": {
                "url": URL,
                "status": 200,
                "mimeType": "application/grpc-web",
            },
        },
    )
    observer.drain()
    session.emit(
        "Page.frameNavigated",
        {"frame": {"id": "main", "loaderId": "loader-new"}},
    )
    observer.drain()

    outcome = observer.close()[0]
    assert outcome.summary.state is BrowserStreamingState.TRANSPORT_ERROR
    assert outcome.failure_code == "streaming-page-replaced"


def test_unknown_request_id_and_unapproved_response_fail_closed() -> None:
    session = _CdpSession()
    observer = _observer(session)
    session.emit(
        "Network.dataReceived",
        {
            "requestId": "unknown",
            "data": _encoded(_frame(b"\x08\x01")),
            "timestamp": "1",
            "encodedDataLength": 7,
        },
    )
    session.emit(
        "Network.requestWillBeSent",
        {
            "requestId": "unapproved",
            "request": {"url": "https://other.example/rpc", "method": "POST"},
        },
    )
    observer.drain()
    assert observer.close() == ()


def test_loading_failed_and_partial_loading_finished_are_distinct() -> None:
    failed_session = _CdpSession()
    failed = _observer(failed_session)
    _emit_candidate(failed_session)
    failed.drain()
    failed_session.emit("Network.loadingFailed", {"requestId": "ephemeral-1"})
    failed.drain()
    failed_outcome = failed.close()[0]
    assert failed_outcome.summary.state is BrowserStreamingState.TRANSPORT_ERROR
    assert failed_outcome.summary.http_response_completed is False

    partial_session = _CdpSession(buffered_data=_encoded(b"\x00\x00"))
    partial = _observer(partial_session)
    _emit_candidate(partial_session)
    partial.drain()
    partial_session.emit("Network.loadingFinished", {"requestId": "ephemeral-1"})
    partial.drain()
    partial_outcome = partial.close()[0]
    assert partial_outcome.summary.state is BrowserStreamingState.FRAME_TRUNCATED
    assert partial_outcome.summary.http_response_completed is True


def test_first_data_idle_and_lifetime_timeouts_are_bounded() -> None:
    first_clock = _Clock()
    first_session = _CdpSession()
    first = _observer(first_session, clock=first_clock)
    _emit_candidate(first_session)
    first.drain()
    first_clock.advance(5_001)
    first.drain()
    assert first.close()[0].summary.state is BrowserStreamingState.FIRST_DATA_TIMEOUT

    idle_clock = _Clock()
    idle_session = _CdpSession()
    idle = _observer(idle_session, clock=idle_clock)
    _emit_candidate(idle_session)
    idle.drain()
    _emit_data(idle_session, _frame(b"\x08\x01"))
    idle.drain()
    idle_clock.advance(2_001)
    idle.drain()
    assert idle.close()[0].summary.state is BrowserStreamingState.IDLE_TIMEOUT

    lifetime_clock = _Clock()
    lifetime_session = _CdpSession()
    lifetime = _observer(
        lifetime_session,
        clock=lifetime_clock,
        limits=CdpStreamingLimits(
            first_body_data_timeout_ms=20_000,
            idle_body_data_timeout_ms=20_000,
            maximum_stream_lifetime_ms=20_000,
        ),
    )
    _emit_candidate(lifetime_session)
    lifetime.drain()
    lifetime_clock.advance(20_001)
    lifetime.drain()
    assert lifetime.close()[0].summary.state is BrowserStreamingState.LIFETIME_TIMEOUT


def test_queue_overflow_rejects_active_stream_without_unbounded_work() -> None:
    session = _CdpSession()
    observer = _observer(
        session,
        limits=CdpStreamingLimits(maximum_queue_size=1),
    )
    session.emit(
        "Network.requestWillBeSent",
        {
            "requestId": "ephemeral-1",
            "request": {"url": URL, "method": "POST"},
        },
    )
    observer.drain()
    session.emit(
        "Network.responseReceived",
        {
            "requestId": "ephemeral-1",
            "type": "Fetch",
            "response": {
                "url": URL,
                "status": 200,
                "mimeType": "application/grpc-web",
            },
        },
    )
    observer.drain()
    _emit_data(session, _frame(b"\x08\x01"), timestamp="1")
    _emit_data(session, _frame(b"\x08\x02"), timestamp="2")
    observer.drain()
    outcome = observer.close()[0]
    assert outcome.summary.state is BrowserStreamingState.BOUNDED_REJECTION
    assert outcome.failure_code == "streaming-queue-overflow"
    assert outcome.summary.bounded_rejection_count == 1


def test_cleanup_during_active_stream_is_idempotent_and_detach_failure_is_safe() -> None:
    session = _CdpSession(detach_error=True)
    observer = _observer(session)
    _emit_candidate(session)
    observer.drain()
    _emit_data(session, b"\x00\x00")
    observer.drain()
    first = observer.close()
    second = observer.close()
    assert first == second
    assert first[0].summary.state is BrowserStreamingState.FRAME_TRUNCATED
    assert session.detached is True


def test_total_byte_chunk_frame_and_response_limits_are_typed() -> None:
    total_session = _CdpSession()
    total = _observer(
        total_session,
        limits=CdpStreamingLimits(
            maximum_total_streaming_bytes=8,
            response=IncrementalGrpcWebLimits(
                maximum_buffered_bytes=8,
                maximum_frame_payload_bytes=8,
                maximum_incomplete_trailing_bytes=8,
            ),
        ),
    )
    _emit_candidate(total_session)
    total.drain()
    _emit_data(total_session, _frame(b"abcd"))
    total.drain()
    assert total.close()[0].failure_code == "streaming-total-byte-limit-exceeded"

    chunk_session = _CdpSession()
    chunked = _observer(
        chunk_session,
        limits=CdpStreamingLimits(
            response=IncrementalGrpcWebLimits(maximum_data_chunks=1),
        ),
    )
    _emit_candidate(chunk_session)
    chunked.drain()
    _emit_data(chunk_session, b"\x00", timestamp="1")
    chunked.drain()
    _emit_data(chunk_session, b"\x00", timestamp="2")
    chunked.drain()
    assert chunked.close()[0].failure_code == "streaming-chunk-limit-exceeded"
