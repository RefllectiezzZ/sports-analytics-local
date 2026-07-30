"""Browser-observed transport provenance and body-capture boundary tests."""

from __future__ import annotations

import base64
import json
from dataclasses import asdict
from datetime import UTC, datetime
from types import SimpleNamespace
from urllib.parse import urlunsplit

import pytest

from sports_analytics.core.exceptions import PermanentSourceError
from sports_analytics.sources.betclic.catalog import (
    BETCLIC_CATALOG,
    OFFERING_RESPONSE_HOSTNAME,
    OFFERING_RESPONSE_ROUTES,
)
from sports_analytics.sources.browser.contracts import (
    BrowserAcquisitionResult,
    BrowserBlockReason,
    BrowserBodyCaptureState,
    BrowserMode,
    BrowserResponseObservation,
    BrowserTransportType,
)
from sports_analytics.sources.browser.playwright_runtime import (
    PlaywrightBrowserSession,
    approved_json_payload_for_profile,
    classify_safe_websocket_url,
)
from sports_analytics.sources.browser.readiness import ReadinessBlockedError

NOW = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
TOP_EVENTS = json.dumps(
    {
        "data": {
            "topEventsV2": {
                "eventIdList": [],
                "events": {},
                "markets": {},
                "selections": {},
            }
        }
    }
)


class _Request:
    resource_type = "xhr"
    method = "GET"
    redirected_from = None


class _Response:
    def __init__(
        self,
        url: str,
        body: str,
        *,
        declared_length: int | None = None,
        content_type: str = "application/json",
        raw_body: bytes | None = None,
    ) -> None:
        self.url = url
        self.status = 200
        self.request = _Request()
        self._body = body
        self._raw_body = raw_body if raw_body is not None else body.encode()
        self.text_calls = 0
        self.body_calls = 0
        self.headers = {"content-type": content_type}
        if declared_length is not None:
            self.headers["content-length"] = str(declared_length)

    def text(self) -> str:
        self.text_calls += 1
        return self._body

    def body(self) -> bytes:
        self.body_calls += 1
        return self._raw_body


class _Page:
    def __init__(
        self,
        responses: tuple[_Response, ...],
        websocket_urls: tuple[str, ...] = (),
        *,
        finish_requests: bool = True,
        cdp_session=None,
    ) -> None:
        self._responses = responses
        self._finish_requests = finish_requests
        self._response_callback = None
        self._request_finished_callback = None
        self._websocket_callback = None
        self._websocket_urls = websocket_urls
        self._cdp_session = cdp_session
        self.url = "https://www.betano.pt/sport/futebol/"
        self.goto_calls = 0

    def on(self, event: str, callback) -> None:
        if event == "response":
            self._response_callback = callback
            return
        if event == "requestfinished":
            self._request_finished_callback = callback
            return
        assert event == "websocket"
        self._websocket_callback = callback

    def goto(self, url: str, *_args, **_kwargs) -> None:
        self.goto_calls += 1
        self.url = url
        assert self._response_callback is not None
        assert self._request_finished_callback is not None
        for response in self._responses:
            if self._cdp_session is not None:
                self._cdp_session.emit_response(response)
            self._response_callback(response)
            if self._finish_requests:
                self._request_finished_callback(response.request)
        assert self._websocket_callback is not None
        for socket_url in self._websocket_urls:
            self._websocket_callback(SimpleNamespace(url=socket_url))

    def title(self) -> str:
        return "Public sport"

    def inner_text(self, _selector: str) -> str:
        return "Public pre-match content"

    def inner_html(self, _selector: str) -> str:
        return "<main></main>"

    def wait_for_timeout(self, _timeout: float) -> None:
        return None


class _Context:
    def __init__(self, page: _Page, cdp_session=None) -> None:
        self.page = page
        self.cdp_session = cdp_session

    def new_page(self) -> _Page:
        return self.page

    def new_cdp_session(self, _page):
        if self.cdp_session is None:
            raise RuntimeError("synthetic CDP unavailable")
        return self.cdp_session

    def close(self) -> None:
        return None


class _Browser:
    def __init__(self, page: _Page, cdp_session=None) -> None:
        self.page = page
        self.cdp_session = cdp_session
        self.context_kwargs = None

    def new_context(self, **kwargs):
        self.context_kwargs = kwargs
        return _Context(self.page, self.cdp_session)

    def close(self) -> None:
        return None


class _PlaywrightContext:
    def __init__(self, page: _Page, cdp_session=None) -> None:
        self.browser = _Browser(page, cdp_session)
        self.playwright = SimpleNamespace(
            chromium=SimpleNamespace(launch=lambda **_kwargs: self.browser)
        )

    def __enter__(self):
        return self.playwright

    def __exit__(self, *_args) -> None:
        return None


def _acquire(
    monkeypatch,
    responses: tuple[_Response, ...],
    *,
    blocked: bool = False,
    websocket_urls: tuple[str, ...] = (),
    start_urls: tuple[tuple[str, str], ...] | None = None,
    provider_id: str = "betano-pt",
    allowed_hostnames: frozenset[str] = frozenset({"www.betano.pt"}),
    diagnostic_directory=None,
    finish_requests: bool = True,
    cdp_session=None,
    **session_kwargs,
):
    page = _Page(
        responses,
        websocket_urls,
        finish_requests=finish_requests,
        cdp_session=cdp_session,
    )
    context = _PlaywrightContext(page, cdp_session)
    monkeypatch.setattr(
        "playwright.sync_api.sync_playwright",
        lambda: context,
    )
    if blocked:

        def _blocked(*_args, **kwargs):
            raise ReadinessBlockedError(
                BrowserBlockReason.ACCESS_DENIED,
                page_route_id=str(kwargs["page_route_id"]),
            )

        monkeypatch.setattr(
            "sports_analytics.sources.browser.playwright_runtime.wait_for_readiness",
            _blocked,
        )
    else:
        monkeypatch.setattr(
            "sports_analytics.sources.browser.playwright_runtime.wait_for_readiness",
            lambda *_args, **_kwargs: None,
        )
    monkeypatch.setattr(
        "sports_analytics.sources.browser.playwright_runtime.dismiss_cookie_consent",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        "sports_analytics.sources.browser.playwright_runtime.classify_readiness_block",
        lambda *_args, **_kwargs: None,
    )
    session = PlaywrightBrowserSession(
        clock=lambda: NOW,
        dwell_after_readiness_ms=0,
        **session_kwargs,
    )
    result = session.acquire(
        provider_id=provider_id,
        sport="football",
        acquisition_cycle_id="cycle-browser-observed",
        allowed_hostnames=allowed_hostnames,
        start_urls=start_urls or (("football-prematch", "https://www.betano.pt/sport/futebol/"),),
        observed_at_utc=NOW,
        browser_mode=BrowserMode.HEADLESS,
        diagnostic_directory=diagnostic_directory,
    )
    return result, page, context.browser


def _grpc_frame(payload: bytes) -> bytes:
    return b"\x00" + len(payload).to_bytes(4, "big") + payload


class _CdpSession:
    def __init__(self, streaming_body: bytes) -> None:
        self._streaming_body = streaming_body
        self.callbacks = {}
        self.detached = False

    def send(self, method, _params=None):
        if method in {
            "Debugger.disable",
            "Debugger.enable",
            "Network.enable",
            "Page.enable",
        }:
            return {}
        assert method == "Network.streamResourceContent"
        return {"bufferedData": ""}

    def on(self, event, callback) -> None:
        self.callbacks[event] = callback

    def emit_response(self, response: _Response) -> None:
        request_id = "ephemeral-runtime"
        self.callbacks["Network.requestWillBeSent"](
            {
                "requestId": request_id,
                "request": {"url": response.url, "method": "POST"},
            }
        )
        self.callbacks["Network.responseReceived"](
            {
                "requestId": request_id,
                "type": "Fetch",
                "response": {
                    "url": response.url,
                    "status": response.status,
                    "mimeType": response.headers["content-type"],
                },
            }
        )
        self.callbacks["Network.dataReceived"](
            {
                "requestId": request_id,
                "data": base64.b64encode(self._streaming_body).decode(),
                "timestamp": "1",
                "encodedDataLength": len(self._streaming_body),
            }
        )

    def detach(self) -> None:
        self.detached = True


def test_page_response_callback_retains_only_reviewed_structural_json(monkeypatch) -> None:
    approved = _Response("https://www.betano.pt/api/reviewed", TOP_EVENTS)
    configuration = _Response(
        "https://www.betano.pt/configuration.json",
        '{"configuration":{"release":"synthetic"}}',
    )
    result, _page, browser = _acquire(monkeypatch, (approved, configuration))

    assert len(result.responses) == 1
    observed = result.responses[0]
    assert observed.sport == "football"
    assert observed.acquisition_cycle_id == "cycle-browser-observed"
    assert observed.page_route_id == "football-prematch"
    assert observed.transport_type is BrowserTransportType.XHR
    assert observed.body_capture_state is BrowserBodyCaptureState.CAPTURED
    assert observed.actual_captured_byte_length == len(TOP_EVENTS.encode())
    assert observed.contributing_capture_checksum is not None
    assert browser.context_kwargs == {
        "locale": "pt-PT",
        "timezone_id": "Europe/Lisbon",
    }
    assert len(result.network_metadata) == 2
    assert result.network_metadata[0].body_captured
    assert result.network_metadata[1].body_capture_state is BrowserBodyCaptureState.NOT_APPROVED
    assert result.network_metadata[1].transport_type is BrowserTransportType.SCRIPT_CONFIGURATION
    assert not result.network_metadata[1].body_captured


def test_betclic_grpc_body_is_read_only_after_request_finished(
    monkeypatch,
    tmp_path,
) -> None:
    popular_path = next(
        path
        for path, route_id in OFFERING_RESPONSE_ROUTES.items()
        if route_id == "betclic-match-service-get-popular-v2"
    )
    grpc_response = _Response(
        urlunsplit(("https", OFFERING_RESPONSE_HOSTNAME, popular_path, "", "")),
        "",
        content_type="application/grpc-web",
        raw_body=_grpc_frame(b"PROTOBUF-FAKE"),
    )
    result, _page, _browser = _acquire(
        monkeypatch,
        (grpc_response,),
        provider_id="betclic-pt",
        allowed_hostnames=BETCLIC_CATALOG.allowed_hostnames,
        start_urls=BETCLIC_CATALOG.routes_for_sport("football"),
        diagnostic_directory=tmp_path,
    )
    assert grpc_response.body_calls == 1
    assert len(result.grpc_web_diagnostics) == 1
    approved = [
        item
        for item in result.network_metadata
        if item.approved_route_id == "betclic-match-service-get-popular-v2"
    ]
    assert len(approved) == 1
    assert approved[0].grpc_web_envelope_recognized is True
    assert approved[0].grpc_web_evidence_stored is True


def test_incomplete_betclic_grpc_response_remains_metadata_only(
    monkeypatch,
    tmp_path,
) -> None:
    popular_path = next(
        path
        for path, route_id in OFFERING_RESPONSE_ROUTES.items()
        if route_id == "betclic-match-service-get-popular-v2"
    )
    grpc_response = _Response(
        urlunsplit(("https", OFFERING_RESPONSE_HOSTNAME, popular_path, "", "")),
        "",
        content_type="application/grpc-web",
        raw_body=_grpc_frame(b"PROTOBUF-FAKE"),
    )
    result, _page, _browser = _acquire(
        monkeypatch,
        (grpc_response,),
        provider_id="betclic-pt",
        allowed_hostnames=BETCLIC_CATALOG.allowed_hostnames,
        start_urls=BETCLIC_CATALOG.routes_for_sport("football"),
        diagnostic_directory=tmp_path,
        finish_requests=False,
    )
    assert grpc_response.body_calls == 0
    assert result.grpc_web_diagnostics == ()
    approved = [
        item
        for item in result.network_metadata
        if item.approved_route_id == "betclic-match-service-get-popular-v2"
    ]
    assert len(approved) == 1
    assert approved[0].grpc_web_failure_code == "streaming-body-observation-unsupported"
    assert approved[0].grpc_web_body_read is False


def test_incomplete_http_response_yields_complete_streaming_frame_via_passive_cdp(
    monkeypatch,
    tmp_path,
) -> None:
    popular_path = next(
        path
        for path, route_id in OFFERING_RESPONSE_ROUTES.items()
        if route_id == "betclic-match-service-get-popular-v2"
    )
    grpc_response = _Response(
        urlunsplit(("https", OFFERING_RESPONSE_HOSTNAME, popular_path, "", "")),
        "",
        content_type="application/grpc-web",
        raw_body=_grpc_frame(b"\x08\x01"),
    )
    cdp_session = _CdpSession(_grpc_frame(b"\x08\x01"))
    result, _page, _browser = _acquire(
        monkeypatch,
        (grpc_response,),
        provider_id="betclic-pt",
        allowed_hostnames=BETCLIC_CATALOG.allowed_hostnames,
        start_urls=BETCLIC_CATALOG.routes_for_sport("football"),
        diagnostic_directory=tmp_path,
        finish_requests=False,
        cdp_session=cdp_session,
    )
    assert grpc_response.body_calls == 0
    assert cdp_session.detached is True
    assert len(result.grpc_web_stream_summaries) == 1
    summary = result.grpc_web_stream_summaries[0]
    assert summary.state.value == "streaming-rpc-open"
    assert summary.data_frame_count == 1
    assert summary.trailer_frame_count == 0
    assert summary.http_response_completed is False
    assert len(summary.protobuf_wire_fingerprints) == 1
    assert len(result.grpc_web_diagnostics) == 1
    assert result.grpc_web_diagnostics[0].capture_unit == "complete-streaming-frame"
    persisted = repr(asdict(result))
    assert "ephemeral-runtime" not in persisted
    assert grpc_response.url not in persisted


def test_completed_response_is_not_retained_by_finite_and_streaming_paths(
    monkeypatch,
    tmp_path,
) -> None:
    popular_path = next(
        path
        for path, route_id in OFFERING_RESPONSE_ROUTES.items()
        if route_id == "betclic-match-service-get-popular-v2"
    )
    framed = _grpc_frame(b"\x08\x01")
    grpc_response = _Response(
        urlunsplit(("https", OFFERING_RESPONSE_HOSTNAME, popular_path, "", "")),
        "",
        content_type="application/grpc-web",
        raw_body=framed,
    )
    result, _page, _browser = _acquire(
        monkeypatch,
        (grpc_response,),
        provider_id="betclic-pt",
        allowed_hostnames=BETCLIC_CATALOG.allowed_hostnames,
        start_urls=BETCLIC_CATALOG.routes_for_sport("football"),
        diagnostic_directory=tmp_path,
        finish_requests=True,
        cdp_session=_CdpSession(framed),
    )

    assert grpc_response.body_calls == 0
    assert len(result.grpc_web_stream_summaries) == 1
    assert len(result.grpc_web_diagnostics) == 1
    assert result.grpc_web_diagnostics[0].capture_unit == "complete-streaming-frame"


def test_declared_oversize_is_not_read_and_forces_truncation(monkeypatch) -> None:
    response = _Response(
        "https://www.betano.pt/api/reviewed",
        TOP_EVENTS,
        declared_length=10_000,
    )
    result, _page, _browser = _acquire(
        monkeypatch,
        (response,),
        maximum_response_bytes=512,
        maximum_total_capture_bytes=1_024,
    )
    assert response.text_calls == 0
    assert result.responses == ()
    assert result.truncated_response_count == 1
    assert (
        result.network_metadata[0].body_capture_state
        is BrowserBodyCaptureState.DECLARED_SIZE_REJECTED
    )


def test_response_cycle_mismatch_is_rejected() -> None:
    response = BrowserResponseObservation(
        provider_id="betano-pt",
        sport="football",
        acquisition_cycle_id="cycle-other",
        page_route_id="football-prematch",
        response_url="https://www.betano.pt/api/reviewed",
        observed_at_utc=NOW,
        content_type="application/json",
        body_text=TOP_EVENTS,
        status_code=200,
        warnings=(),
    )
    with pytest.raises(PermanentSourceError, match="cycle"):
        BrowserAcquisitionResult(
            provider_id="betano-pt",
            sport="football",
            acquisition_cycle_id="cycle-active",
            observed_at_utc=NOW,
            browser_mode=BrowserMode.HEADLESS,
            pages=(),
            responses=(response,),
            diagnostics=(),
            block_reason=None,
            warnings=(),
        )


def test_configuration_and_url_keywords_are_not_structural_approval() -> None:
    assert not approved_json_payload_for_profile(
        provider_id="betano-pt",
        resource_type="xhr",
        payload={"event": {}, "market": {}, "odds": {}},
    )
    assert not approved_json_payload_for_profile(
        provider_id="betclic-pt",
        resource_type="fetch",
        payload={"configuration": {"event": "synthetic"}},
    )


def test_wss_callback_produces_sanitized_metadata_only(monkeypatch) -> None:
    socket_url = "wss://www.betano.pt/live/stream?token=secret#private"
    result, _page, _browser = _acquire(
        monkeypatch,
        (),
        websocket_urls=(socket_url,),
    )
    assert result.responses == ()
    assert len(result.network_metadata) == 1
    metadata = result.network_metadata[0]
    assert metadata.hostname == "www.betano.pt"
    assert metadata.transport_type is BrowserTransportType.WEBSOCKET
    assert metadata.body_capture_state is BrowserBodyCaptureState.METADATA_ONLY
    assert metadata.sanitized_path_hash is not None
    assert "secret" not in repr(metadata)
    assert socket_url not in repr(metadata)


@pytest.mark.parametrize(
    "url,approved_hosts",
    [
        ("ws://www.betano.pt/live", frozenset({"www.betano.pt"})),
        ("wss://user:pass@www.betano.pt/live", frozenset({"www.betano.pt"})),
        ("wss://other.example/live", frozenset({"www.betano.pt"})),
        ("wss://localhost/live", frozenset({"localhost"})),
        ("wss://127.0.0.1/live", frozenset({"127.0.0.1"})),
        ("wss://[::1]/live", frozenset({"::1"})),
        ("wss://www.betan\u043e.pt/live", frozenset({"www.betano.pt"})),
        ("wss:///missing-host", frozenset({"www.betano.pt"})),
        ("wss://www.betano.pt:not-a-port/live", frozenset({"www.betano.pt"})),
        ("wss://www.betano.pt:8443/live", frozenset({"www.betano.pt"})),
    ],
)
def test_wss_classifier_rejects_unsafe_targets(
    url: str,
    approved_hosts: frozenset[str],
) -> None:
    with pytest.raises(PermanentSourceError):
        classify_safe_websocket_url(url, allowed_hostnames=approved_hosts)


def test_explicit_block_stops_remaining_routes_without_retry(monkeypatch) -> None:
    result, page, _browser = _acquire(
        monkeypatch,
        (),
        blocked=True,
        start_urls=(
            ("football-prematch", "https://www.betano.pt/sport/futebol/"),
            ("second-fixed-route", "https://www.betano.pt/sport/futebol/"),
        ),
    )
    assert result.block_reason is BrowserBlockReason.ACCESS_DENIED
    assert page.goto_calls == 1
