"""Passive bounded Chromium CDP streaming observation for approved responses."""

from __future__ import annotations

import base64
import binascii
import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from queue import Empty, Full, Queue
from typing import Any, Protocol

from sports_analytics.core.exceptions import PermanentSourceError
from sports_analytics.sources.betclic.grpc_web import (
    is_recognized_grpc_web_content_type,
    store_content_addressed_grpc_evidence,
)
from sports_analytics.sources.browser.contracts import (
    BrowserGrpcWebDiagnostic,
    BrowserGrpcWebStreamSummary,
    BrowserStreamingState,
)
from sports_analytics.sources.browser.grpc_web_stream import (
    GrpcWebFrameKind,
    IncrementalGrpcWebDecoder,
    IncrementalGrpcWebError,
    IncrementalGrpcWebFrame,
    IncrementalGrpcWebLimits,
    framing_for_content_type,
)
from sports_analytics.sources.browser.protobuf_wire import (
    ProtobufWireInspectionError,
    inspect_protobuf_wire,
)


class CdpSession(Protocol):
    """Minimal page-scoped CDP session used by the synchronous runtime."""

    def send(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]: ...

    def on(self, event: str, callback: Callable[[dict[str, Any]], None]) -> None: ...

    def detach(self) -> None: ...


@dataclass(frozen=True, slots=True)
class ApprovedStreamingResponse:
    """Safe result from the existing exact provider response classifier."""

    hostname: str
    sanitized_path_hash: str
    approved_route_id: str
    metadata_only: bool


@dataclass(frozen=True, slots=True)
class CdpStreamingLimits:
    """Conservative cycle and time bounds for passive stream observation."""

    maximum_responses_per_cycle: int = 2
    maximum_total_streaming_bytes: int = 4_194_304
    maximum_queue_size: int = 512
    first_body_data_timeout_ms: int = 5_000
    idle_body_data_timeout_ms: int = 2_000
    maximum_stream_lifetime_ms: int = 15_000
    response: IncrementalGrpcWebLimits = IncrementalGrpcWebLimits()

    def __post_init__(self) -> None:
        values = (
            self.maximum_responses_per_cycle,
            self.maximum_total_streaming_bytes,
            self.maximum_queue_size,
            self.first_body_data_timeout_ms,
            self.idle_body_data_timeout_ms,
            self.maximum_stream_lifetime_ms,
        )
        if any(isinstance(value, bool) or value < 1 for value in values):
            msg = "CDP streaming limits must be positive integers"
            raise PermanentSourceError(msg)
        if self.maximum_total_streaming_bytes < self.response.maximum_buffered_bytes:
            msg = "cycle streaming byte limit must cover one bounded response"
            raise PermanentSourceError(msg)
        if self.maximum_stream_lifetime_ms < self.first_body_data_timeout_ms:
            msg = "stream lifetime must cover the first-data timeout"
            raise PermanentSourceError(msg)
        if self.maximum_stream_lifetime_ms < self.idle_body_data_timeout_ms:
            msg = "stream lifetime must cover the idle timeout"
            raise PermanentSourceError(msg)


@dataclass(frozen=True, slots=True)
class StreamingResponseOutcome:
    """Completed safe result plus one ephemeral URL for metadata construction."""

    response_url: str = field(repr=False, compare=False)
    summary: BrowserGrpcWebStreamSummary
    diagnostics: tuple[BrowserGrpcWebDiagnostic, ...]
    failure_code: str | None
    evidence_stored: bool
    status_code: int
    content_type: str
    resource_type: str
    request_method: str


@dataclass(frozen=True, slots=True)
class _RequestObservation:
    request_id: str
    url: str = field(repr=False, compare=False)
    method: str
    page_route_id: str
    loader_id: str | None
    redirected: bool
    observed_at_utc: datetime


@dataclass(frozen=True, slots=True)
class _ResponseObservation:
    request_id: str
    url: str = field(repr=False, compare=False)
    status: int
    content_type: str
    resource_type: str
    page_route_id: str
    loader_id: str | None
    observed_at_utc: datetime


@dataclass(frozen=True, slots=True)
class _DataObservation:
    request_id: str
    encoded_data: str = field(repr=False, compare=False)
    timestamp: str
    encoded_length: int
    observed_at_utc: datetime


@dataclass(frozen=True, slots=True)
class _LoadingObservation:
    request_id: str
    failed: bool
    observed_at_utc: datetime


@dataclass(frozen=True, slots=True)
class _MissingDataObservation:
    request_id: str


@dataclass(frozen=True, slots=True)
class _NavigationObservation:
    loader_id: str


@dataclass(slots=True)
class _ActiveStream:
    request_id: str = field(repr=False)
    response_url: str = field(repr=False)
    approved: ApprovedStreamingResponse
    page_route_id: str
    content_type: str
    status_code: int
    resource_type: str
    request_method: str
    loader_id: str | None
    started_at_utc: datetime
    decoder: IncrementalGrpcWebDecoder
    last_data_at_utc: datetime | None = None
    diagnostics: list[BrowserGrpcWebDiagnostic] = field(default_factory=list)
    fingerprints: set[str] = field(default_factory=set)
    data_frame_count: int = 0
    trailer_frame_count: int = 0
    retained_byte_count: int = 0
    bounded_rejection_count: int = 0
    missing_data_event_count: int = 0
    duplicate_data_event_count: int = 0
    deduplicated_overlap_byte_count: int = 0
    http_completed: bool = False
    rejected_reason: str | None = None
    buffered_data: bytes | None = field(default=None, repr=False)
    first_network_data_seen: bool = False
    seen_data_events: set[str] = field(default_factory=set, repr=False)


class PassiveCdpStreamingObserver:
    """Single-consumer CDP correlation and incremental frame observer."""

    def __init__(
        self,
        *,
        session: CdpSession,
        classify_response: Callable[[str], ApprovedStreamingResponse],
        page_route_id: Callable[[], str],
        diagnostic_directory: Path | None,
        clock: Callable[[], datetime] | None = None,
        limits: CdpStreamingLimits | None = None,
        capture_kind: str = "betclic-grpc-web-frame",
    ) -> None:
        self._session = session
        self._classify_response = classify_response
        self._page_route_id = page_route_id
        self._diagnostic_directory = diagnostic_directory
        self._clock = clock or (lambda: datetime.now(UTC))
        self._limits = limits or CdpStreamingLimits()
        self._capture_kind = capture_kind
        self._queue: Queue[object] = Queue(maxsize=self._limits.maximum_queue_size)
        self._requests: dict[str, _RequestObservation] = {}
        self._streams: dict[str, _ActiveStream] = {}
        self._outcomes: list[StreamingResponseOutcome] = []
        self._streaming_bytes = 0
        self._queue_overflowed = False
        self._closed = False
        self._network_enabled = False
        self._main_loader_id: str | None = None

    def attach(self) -> None:
        """Enable Network before navigation and install enqueue-only callbacks."""
        self._session.send("Network.enable")
        self._network_enabled = True
        try:
            self._session.send("Page.enable")
        except Exception:  # noqa: BLE001 - optional lifecycle feature detection
            pass
        self._session.on("Network.requestWillBeSent", self._on_request_will_be_sent)
        self._session.on("Network.responseReceived", self._on_response_received)
        self._session.on("Network.dataReceived", self._on_data_received)
        self._session.on("Network.loadingFinished", self._on_loading_finished)
        self._session.on("Network.loadingFailed", self._on_loading_failed)
        self._session.on("Page.frameNavigated", self._on_frame_navigated)

    def drain(self) -> None:
        """Process queued observations sequentially outside CDP callbacks."""
        if self._closed:
            return
        if self._queue_overflowed:
            self._queue_overflowed = False
            for request_id in tuple(self._streams):
                self._reject_stream(request_id, "streaming-queue-overflow")
        while True:
            try:
                observation = self._queue.get_nowait()
            except Empty:
                break
            if isinstance(observation, _RequestObservation):
                self._consume_request(observation)
            elif isinstance(observation, _ResponseObservation):
                self._consume_response(observation)
            elif isinstance(observation, _DataObservation):
                self._consume_data(observation)
            elif isinstance(observation, _LoadingObservation):
                self._consume_loading(observation)
            elif isinstance(observation, _MissingDataObservation):
                self._consume_missing_data(observation)
            elif isinstance(observation, _NavigationObservation):
                self._consume_navigation(observation)
        self._apply_timeouts(self._clock())

    def close(self) -> tuple[StreamingResponseOutcome, ...]:
        """Drain, finalize open streams, and detach without leaking handlers."""
        if self._closed:
            return tuple(self._outcomes)
        self.drain()
        for request_id in tuple(self._streams):
            stream = self._streams.get(request_id)
            if stream is None:
                continue
            try:
                stream.decoder.finalize()
            except IncrementalGrpcWebError:
                self._finish_stream(
                    request_id,
                    state=BrowserStreamingState.FRAME_TRUNCATED,
                    failure_code="streaming-frame-truncated",
                )
            else:
                state = (
                    BrowserStreamingState.RPC_COMPLETE
                    if stream.decoder.trailer_seen
                    else BrowserStreamingState.RPC_OPEN
                )
                self._finish_stream(request_id, state=state)
        self._closed = True
        try:
            if self._network_enabled:
                self._session.detach()
        except Exception:  # noqa: BLE001 - cleanup must not mask primary evidence
            pass
        self._requests.clear()
        return tuple(self._outcomes)

    @property
    def outcomes(self) -> tuple[StreamingResponseOutcome, ...]:
        return tuple(self._outcomes)

    def uses_streaming_for_url(self, response_url: str) -> bool:
        """Return whether supported CDP capture owns this ephemeral response."""
        if any(stream.response_url == response_url for stream in self._streams.values()):
            return True
        return any(
            item.response_url == response_url and item.summary.observation_supported
            for item in self._outcomes
        )

    def _enqueue(self, observation: object) -> None:
        try:
            self._queue.put_nowait(observation)
        except Full:
            self._queue_overflowed = True

    def _on_request_will_be_sent(self, event: dict[str, Any]) -> None:
        request = event.get("request")
        if not isinstance(request, dict):
            return
        request_id = event.get("requestId")
        url = request.get("url")
        method = request.get("method")
        loader_id = event.get("loaderId")
        if (
            not isinstance(request_id, str)
            or not request_id
            or not isinstance(url, str)
            or not url
            or not isinstance(method, str)
            or not method
            or (loader_id is not None and not isinstance(loader_id, str))
        ):
            return
        self._enqueue(
            _RequestObservation(
                request_id=request_id,
                url=url,
                method=method.upper(),
                page_route_id=self._page_route_id(),
                loader_id=loader_id or None,
                redirected=isinstance(event.get("redirectResponse"), dict),
                observed_at_utc=self._clock(),
            )
        )

    def _on_response_received(self, event: dict[str, Any]) -> None:
        response = event.get("response")
        if not isinstance(response, dict):
            return
        request_id = event.get("requestId")
        url = response.get("url")
        status = response.get("status")
        content_type = response.get("mimeType")
        resource_type = event.get("type")
        loader_id = event.get("loaderId")
        if (
            not isinstance(request_id, str)
            or not isinstance(url, str)
            or isinstance(status, bool)
            or not isinstance(status, int | float)
            or not isinstance(content_type, str)
            or not isinstance(resource_type, str)
            or (loader_id is not None and not isinstance(loader_id, str))
        ):
            return
        self._enqueue(
            _ResponseObservation(
                request_id=request_id,
                url=url,
                status=int(status),
                content_type=content_type,
                resource_type=resource_type,
                page_route_id=self._page_route_id(),
                loader_id=loader_id or None,
                observed_at_utc=self._clock(),
            )
        )

    def _on_data_received(self, event: dict[str, Any]) -> None:
        request_id = event.get("requestId")
        encoded_data = event.get("data")
        encoded_length = event.get("encodedDataLength", 0)
        if isinstance(request_id, str) and not isinstance(encoded_data, str):
            self._enqueue(_MissingDataObservation(request_id=request_id))
            return
        if (
            not isinstance(request_id, str)
            or not isinstance(encoded_data, str)
            or isinstance(encoded_length, bool)
            or not isinstance(encoded_length, int | float)
        ):
            return
        self._enqueue(
            _DataObservation(
                request_id=request_id,
                encoded_data=encoded_data,
                timestamp=str(event.get("timestamp", "")),
                encoded_length=int(encoded_length),
                observed_at_utc=self._clock(),
            )
        )

    def _on_frame_navigated(self, event: dict[str, Any]) -> None:
        frame = event.get("frame")
        if not isinstance(frame, dict) or frame.get("parentId") is not None:
            return
        loader_id = frame.get("loaderId")
        if isinstance(loader_id, str) and loader_id:
            self._enqueue(_NavigationObservation(loader_id=loader_id))

    def _on_loading_finished(self, event: dict[str, Any]) -> None:
        request_id = event.get("requestId")
        if isinstance(request_id, str):
            self._enqueue(
                _LoadingObservation(
                    request_id=request_id,
                    failed=False,
                    observed_at_utc=self._clock(),
                )
            )

    def _on_loading_failed(self, event: dict[str, Any]) -> None:
        request_id = event.get("requestId")
        if isinstance(request_id, str):
            self._enqueue(
                _LoadingObservation(
                    request_id=request_id,
                    failed=True,
                    observed_at_utc=self._clock(),
                )
            )

    def _consume_request(self, observation: _RequestObservation) -> None:
        previous = self._requests.get(observation.request_id)
        if previous is not None:
            if (
                not observation.redirected
                and previous.url == observation.url
                and previous.method == observation.method
                and previous.page_route_id == observation.page_route_id
                and previous.loader_id == observation.loader_id
            ):
                return
            if observation.request_id in self._streams:
                self._finish_stream(
                    observation.request_id,
                    state=BrowserStreamingState.TRANSPORT_ERROR,
                    failure_code=(
                        "streaming-request-redirected"
                        if observation.redirected
                        else "streaming-request-id-reused"
                    ),
                )
            self._requests.pop(observation.request_id, None)
        try:
            approved = self._classify_response(observation.url)
        except PermanentSourceError:
            return
        if approved.metadata_only:
            return
        self._requests[observation.request_id] = observation

    def _consume_response(self, observation: _ResponseObservation) -> None:
        if observation.request_id in self._streams:
            return
        request = self._requests.get(observation.request_id)
        if request is None or request.page_route_id != observation.page_route_id:
            return
        if (
            request.loader_id is not None
            and observation.loader_id is not None
            and request.loader_id != observation.loader_id
        ):
            return
        if observation.resource_type.casefold() not in {"fetch", "xhr"}:
            return
        if observation.status < 200 or observation.status >= 300:
            return
        if not is_recognized_grpc_web_content_type(observation.content_type):
            return
        try:
            approved = self._classify_response(observation.url)
        except PermanentSourceError:
            return
        if approved.metadata_only:
            return
        if len(self._streams) + len(self._outcomes) >= self._limits.maximum_responses_per_cycle:
            if not any(
                item.failure_code == "streaming-response-limit-exceeded" for item in self._outcomes
            ):
                self._outcomes.append(
                    self._unsupported_outcome(
                        observation,
                        approved=approved,
                        failure_code="streaming-response-limit-exceeded",
                        state=BrowserStreamingState.BOUNDED_REJECTION,
                        supported=True,
                    )
                )
            return
        try:
            framing = framing_for_content_type(observation.content_type)
        except IncrementalGrpcWebError as exc:
            self._outcomes.append(
                self._unsupported_outcome(
                    observation,
                    approved=approved,
                    failure_code=exc.classification,
                    state=BrowserStreamingState.UNSUPPORTED,
                    supported=True,
                )
            )
            return
        stream = _ActiveStream(
            request_id=observation.request_id,
            response_url=observation.url,
            approved=approved,
            page_route_id=observation.page_route_id,
            content_type=observation.content_type,
            status_code=observation.status,
            resource_type=observation.resource_type.casefold(),
            request_method=request.method,
            loader_id=observation.loader_id or request.loader_id,
            started_at_utc=observation.observed_at_utc,
            decoder=IncrementalGrpcWebDecoder(
                framing=framing,
                limits=self._limits.response,
            ),
        )
        self._streams[observation.request_id] = stream
        try:
            result = self._session.send(
                "Network.streamResourceContent",
                {"requestId": observation.request_id},
            )
        except Exception as exc:  # noqa: BLE001 - feature detection is runtime-only
            failure_code = (
                "streaming-body-observation-unsupported"
                if _is_unsupported_method_error(exc)
                else "streaming-transport-error"
            )
            state = (
                BrowserStreamingState.UNSUPPORTED
                if failure_code == "streaming-body-observation-unsupported"
                else BrowserStreamingState.TRANSPORT_ERROR
            )
            self._finish_stream(
                observation.request_id,
                state=state,
                failure_code=failure_code,
                observation_supported=False,
            )
            return
        buffered_data = result.get("bufferedData") if isinstance(result, dict) else None
        if isinstance(buffered_data, str) and buffered_data:
            raw = self._decode_data(stream, buffered_data)
            if raw is not None:
                stream.buffered_data = raw
                self._feed_raw_data(
                    stream,
                    raw=raw,
                    observed_at_utc=observation.observed_at_utc,
                )

    def _consume_data(self, observation: _DataObservation) -> None:
        stream = self._streams.get(observation.request_id)
        if stream is None:
            return
        signature = hashlib.sha256(
            (
                observation.request_id
                + "\x00"
                + observation.timestamp
                + "\x00"
                + str(observation.encoded_length)
                + "\x00"
                + observation.encoded_data
            ).encode("utf-8")
        ).hexdigest()
        if signature in stream.seen_data_events:
            stream.duplicate_data_event_count += 1
            return
        stream.seen_data_events.add(signature)
        raw = self._decode_data(stream, observation.encoded_data)
        if raw is None:
            return
        if not stream.first_network_data_seen:
            stream.first_network_data_seen = True
            buffered = stream.buffered_data
            stream.buffered_data = None
            if buffered:
                if raw == buffered:
                    stream.deduplicated_overlap_byte_count += len(raw)
                    return
                if raw.startswith(buffered):
                    stream.deduplicated_overlap_byte_count += len(buffered)
                    raw = raw[len(buffered) :]
                    if not raw:
                        return
        self._feed_raw_data(
            stream,
            raw=raw,
            observed_at_utc=observation.observed_at_utc,
        )

    def _consume_missing_data(self, observation: _MissingDataObservation) -> None:
        stream = self._streams.get(observation.request_id)
        if stream is not None:
            stream.missing_data_event_count += 1

    def _consume_navigation(self, observation: _NavigationObservation) -> None:
        if self._main_loader_id is None:
            self._main_loader_id = observation.loader_id
            return
        if observation.loader_id == self._main_loader_id:
            return
        self._main_loader_id = observation.loader_id
        for request_id, stream in tuple(self._streams.items()):
            if stream.loader_id is not None and stream.loader_id != observation.loader_id:
                self._finish_stream(
                    request_id,
                    state=BrowserStreamingState.TRANSPORT_ERROR,
                    failure_code="streaming-page-replaced",
                )

    def _consume_loading(self, observation: _LoadingObservation) -> None:
        stream = self._streams.get(observation.request_id)
        if stream is None:
            return
        stream.http_completed = not observation.failed
        if observation.failed:
            self._finish_stream(
                observation.request_id,
                state=BrowserStreamingState.TRANSPORT_ERROR,
                failure_code="streaming-loading-failed",
            )
            return
        try:
            stream.decoder.finalize()
        except IncrementalGrpcWebError:
            self._finish_stream(
                observation.request_id,
                state=BrowserStreamingState.FRAME_TRUNCATED,
                failure_code="streaming-frame-truncated",
            )
            return
        state = (
            BrowserStreamingState.RPC_COMPLETE
            if stream.decoder.trailer_seen
            else BrowserStreamingState.RPC_OPEN
        )
        self._finish_stream(observation.request_id, state=state)

    def _decode_data(self, stream: _ActiveStream, encoded_data: str) -> bytes | None:
        try:
            return base64.b64decode(encoded_data, validate=True)
        except (binascii.Error, ValueError):
            self._reject_stream(stream.request_id, "streaming-invalid-cdp-data")
            return None

    def _feed_raw_data(
        self,
        stream: _ActiveStream,
        *,
        raw: bytes,
        observed_at_utc: datetime,
    ) -> None:
        if self._streaming_bytes + len(raw) > self._limits.maximum_total_streaming_bytes:
            self._reject_stream(stream.request_id, "streaming-total-byte-limit-exceeded")
            return
        self._streaming_bytes += len(raw)
        stream.last_data_at_utc = observed_at_utc
        try:
            frames = stream.decoder.feed(
                raw,
                observed_at_utc=observed_at_utc,
                source_capture_reference=stream.approved.approved_route_id,
            )
        except IncrementalGrpcWebError as exc:
            self._reject_stream(stream.request_id, exc.classification)
            return
        for frame in frames:
            if not self._retain_frame(stream, frame):
                return

    def _retain_frame(self, stream: _ActiveStream, frame: IncrementalGrpcWebFrame) -> bool:
        fingerprint: str | None = None
        if frame.kind is GrpcWebFrameKind.DATA:
            stream.data_frame_count += 1
            try:
                fingerprint = inspect_protobuf_wire(frame.payload).fingerprint_sha256
            except ProtobufWireInspectionError:
                fingerprint = None
            if fingerprint is not None:
                stream.fingerprints.add(fingerprint)
        else:
            stream.trailer_frame_count += 1
        stream.retained_byte_count += len(frame.framed_bytes)
        if self._diagnostic_directory is None:
            return True
        stored = None
        try:
            stored = store_content_addressed_grpc_evidence(
                frame.framed_bytes,
                directory=self._diagnostic_directory / "grpc-web-stream",
                suffix=".grpc-web-frame",
            )
            relative_path = stored.absolute_path.relative_to(self._diagnostic_directory).as_posix()
            stream.diagnostics.append(
                BrowserGrpcWebDiagnostic(
                    capture_kind=self._capture_kind,
                    checksum_sha256=stored.checksum_sha256,
                    relative_path=relative_path,
                    byte_count=stored.byte_count,
                    framing=framing_for_content_type(stream.content_type).value,
                    data_frame_count=int(frame.kind is GrpcWebFrameKind.DATA),
                    trailer_frame_count=int(frame.kind is GrpcWebFrameKind.TRAILER),
                    compression_flag_present=frame.compressed,
                    total_framed_payload_bytes=frame.payload_length,
                    malformed_or_truncated=False,
                    grpc_status=frame.grpc_status,
                    newly_created=stored.newly_created,
                    capture_unit="complete-streaming-frame",
                    frame_index=frame.frame_index,
                    frame_kind=frame.kind.value,
                    payload_checksum_sha256=frame.payload_checksum_sha256,
                    protobuf_wire_fingerprint=fingerprint,
                    source_capture_reference=frame.source_capture_reference,
                )
            )
        except Exception:  # noqa: BLE001 - local evidence failure is a safe rejection
            if stored is not None and stored.newly_created:
                try:
                    stored.absolute_path.unlink(missing_ok=True)
                except OSError:
                    pass
            self._reject_stream(stream.request_id, "streaming-evidence-storage-failed")
            return False
        return True

    def _reject_stream(self, request_id: str, failure_code: str) -> None:
        stream = self._streams.get(request_id)
        if stream is None:
            return
        stream.bounded_rejection_count += 1
        stream.rejected_reason = failure_code
        self._finish_stream(
            request_id,
            state=BrowserStreamingState.BOUNDED_REJECTION,
            failure_code=failure_code,
        )

    def _apply_timeouts(self, now: datetime) -> None:
        for request_id, stream in tuple(self._streams.items()):
            lifetime = now - stream.started_at_utc
            if lifetime >= timedelta(milliseconds=self._limits.maximum_stream_lifetime_ms):
                self._finish_stream(
                    request_id,
                    state=BrowserStreamingState.LIFETIME_TIMEOUT,
                    failure_code="streaming-lifetime-timeout",
                )
            elif stream.last_data_at_utc is None and lifetime >= timedelta(
                milliseconds=self._limits.first_body_data_timeout_ms
            ):
                self._finish_stream(
                    request_id,
                    state=BrowserStreamingState.FIRST_DATA_TIMEOUT,
                    failure_code="streaming-first-data-timeout",
                )
            elif stream.last_data_at_utc is not None and now - stream.last_data_at_utc >= timedelta(
                milliseconds=self._limits.idle_body_data_timeout_ms
            ):
                self._finish_stream(
                    request_id,
                    state=BrowserStreamingState.IDLE_TIMEOUT,
                    failure_code="streaming-idle-timeout",
                )

    def _finish_stream(
        self,
        request_id: str,
        *,
        state: BrowserStreamingState,
        failure_code: str | None = None,
        observation_supported: bool = True,
    ) -> None:
        stream = self._streams.pop(request_id, None)
        self._requests.pop(request_id, None)
        if stream is None:
            return
        logical_complete = state is BrowserStreamingState.RPC_COMPLETE
        summary = BrowserGrpcWebStreamSummary(
            approved_route_id=stream.approved.approved_route_id,
            page_route_id=stream.page_route_id,
            hostname=stream.approved.hostname,
            sanitized_path_hash=stream.approved.sanitized_path_hash,
            observation_supported=observation_supported,
            support_reason=failure_code,
            state=state,
            http_response_completed=stream.http_completed,
            logical_rpc_completed=logical_complete,
            data_frame_count=stream.data_frame_count,
            trailer_frame_count=stream.trailer_frame_count,
            complete_frame_count=stream.data_frame_count + stream.trailer_frame_count,
            retained_byte_count=stream.retained_byte_count,
            data_chunk_count=stream.decoder.chunk_count,
            bounded_rejection_count=stream.bounded_rejection_count,
            missing_data_event_count=stream.missing_data_event_count,
            duplicate_data_event_count=stream.duplicate_data_event_count,
            deduplicated_overlap_byte_count=stream.deduplicated_overlap_byte_count,
            protobuf_wire_fingerprints=tuple(stream.fingerprints),
        )
        self._outcomes.append(
            StreamingResponseOutcome(
                response_url=stream.response_url,
                summary=summary,
                diagnostics=tuple(stream.diagnostics),
                failure_code=failure_code,
                evidence_stored=bool(stream.diagnostics),
                status_code=stream.status_code,
                content_type=stream.content_type,
                resource_type=stream.resource_type,
                request_method=stream.request_method,
            )
        )

    def _unsupported_outcome(
        self,
        observation: _ResponseObservation,
        *,
        approved: ApprovedStreamingResponse,
        failure_code: str,
        state: BrowserStreamingState,
        supported: bool,
    ) -> StreamingResponseOutcome:
        return StreamingResponseOutcome(
            response_url=observation.url,
            summary=BrowserGrpcWebStreamSummary(
                approved_route_id=approved.approved_route_id,
                page_route_id=observation.page_route_id,
                hostname=approved.hostname,
                sanitized_path_hash=approved.sanitized_path_hash,
                observation_supported=supported,
                support_reason=failure_code,
                state=state,
                http_response_completed=False,
                logical_rpc_completed=False,
                data_frame_count=0,
                trailer_frame_count=0,
                complete_frame_count=0,
                retained_byte_count=0,
                data_chunk_count=0,
                bounded_rejection_count=1,
            ),
            diagnostics=(),
            failure_code=failure_code,
            evidence_stored=False,
            status_code=observation.status,
            content_type=observation.content_type,
            resource_type=observation.resource_type.casefold(),
            request_method=self._requests[observation.request_id].method,
        )


def _is_unsupported_method_error(exc: Exception) -> bool:
    text = str(exc).casefold()
    return any(
        marker in text
        for marker in (
            "method not found",
            "wasn't found",
            "unknown command",
            "unknown method",
            "-32601",
        )
    )
