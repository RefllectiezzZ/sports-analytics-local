"""Passive public-client inspection with wholly invented scripts."""

from __future__ import annotations

from dataclasses import asdict

import pytest

from sports_analytics.core.exceptions import PermanentSourceError
from sports_analytics.sources.browser.client_schema import (
    ClientSchemaInspectionLimits,
    PassiveClientSchemaInspector,
)
from sports_analytics.sources.browser.contracts import BrowserClientScriptInspection

APPROVED_URL = "https://client.example/assets/invented-client.js?discard=yes"
INVENTED_SOURCE = """
const InventedPopularResponse = {
  fields: [
    { no: 1, kind: "message", repeat: true, T: InventedEvent },
    { no: 2, kind: "scalar", T: 9 }
  ]
};
const InventedService = {
  GetPopularV2: {
    path: "/invented/InventedService/GetPopularV2",
    responseType: InventedPopularResponse
  }
};
jspb.BinaryReader.prototype.readMessage = function () {};
"""


class _Session:
    def __init__(self, sources: dict[str, str]) -> None:
        self.sources = sources
        self.callbacks = {}
        self.send_calls = []

    def on(self, event, callback) -> None:
        self.callbacks[event] = callback

    def send(self, method, params=None):
        self.send_calls.append((method, params))
        if method in {"Debugger.enable", "Debugger.disable"}:
            return {}
        assert method == "Debugger.getScriptSource"
        return {"scriptSource": self.sources[params["scriptId"]]}

    def detach(self) -> None:
        raise AssertionError("the shared inspector must not detach the CDP session")

    def emit_script(self, script_id: str, url: str, source: str) -> None:
        self.callbacks["Debugger.scriptParsed"](
            {"scriptId": script_id, "url": url, "length": len(source)}
        )


def _inspector(
    session: _Session,
    *,
    limits: ClientSchemaInspectionLimits | None = None,
) -> PassiveClientSchemaInspector:
    inspector = PassiveClientSchemaInspector(
        session=session,
        allowed_hostnames=frozenset({"client.example"}),
        target_rpc_symbols=("GetPopularV2",),
        limits=limits,
    )
    inspector.attach()
    return inspector


def test_approved_loaded_script_yields_safe_generated_descriptor_evidence() -> None:
    session = _Session({"approved": INVENTED_SOURCE})
    inspector = _inspector(session)
    session.emit_script("approved", APPROVED_URL, INVENTED_SOURCE)
    summary = inspector.close()

    assert summary.scripts_seen == 1
    assert summary.scripts_inspected == 1
    assert summary.scripts_rejected == 0
    inspection = summary.inspections[0]
    assert inspection.hostname == "client.example"
    assert inspection.runtime_families == (
        "binary-reader-generated",
        "google-protobuf",
    )
    assert inspection.rpc_symbols == ("GetPopularV2",)
    assert inspection.response_type_candidates == ("InventedPopularResponse",)
    assert inspection.method_response_associations == ("GetPopularV2->InventedPopularResponse",)
    assert inspection.field_number_type_sequences == ("1:message:repeated,2:scalar-9:optional",)
    assert inspection.descriptor_fingerprints
    persisted = repr(asdict(summary))
    assert INVENTED_SOURCE not in persisted
    assert APPROVED_URL not in persisted
    assert "discard=yes" not in persisted


def test_cross_provider_and_non_https_scripts_are_rejected_without_source_read() -> None:
    session = _Session(
        {
            "cross": INVENTED_SOURCE,
            "inline": INVENTED_SOURCE,
        }
    )
    inspector = _inspector(session)
    session.emit_script(
        "cross",
        "https://cross-provider.example/client.js",
        INVENTED_SOURCE,
    )
    session.emit_script("inline", "blob:https://client.example/invented", INVENTED_SOURCE)
    summary = inspector.close()

    assert summary.scripts_seen == 2
    assert summary.scripts_inspected == 0
    assert summary.scripts_rejected == 2
    assert summary.rejection_counts == (("unapproved-script", 2),)
    assert all(call[0] != "Debugger.getScriptSource" for call in session.send_calls)


def test_exact_rpc_symbol_does_not_match_substrings() -> None:
    source = INVENTED_SOURCE.replace("GetPopularV2", "GetPopularV20")
    session = _Session({"approved": source})
    inspector = _inspector(session)
    session.emit_script("approved", APPROVED_URL, source)
    summary = inspector.close()

    inspection = summary.inspections[0]
    assert inspection.rpc_symbols == ()
    assert inspection.method_response_associations == ()


def test_source_and_script_count_limits_fail_closed() -> None:
    oversized = INVENTED_SOURCE + ("x" * 100)
    session = _Session({"one": oversized, "two": INVENTED_SOURCE})
    inspector = _inspector(
        session,
        limits=ClientSchemaInspectionLimits(
            maximum_scripts_seen=1,
            maximum_scripts_inspected=1,
            maximum_source_chars_per_script=len(INVENTED_SOURCE),
            maximum_total_source_chars=len(INVENTED_SOURCE),
        ),
    )
    session.emit_script("one", APPROVED_URL, oversized)
    session.emit_script("two", APPROVED_URL, INVENTED_SOURCE)
    summary = inspector.close()

    assert summary.scripts_seen == 2
    assert summary.scripts_inspected == 0
    assert summary.scripts_rejected == 2
    assert summary.bounds_hit == ("per-script-source-limit", "script-seen-limit")
    assert all(call[0] != "Debugger.getScriptSource" for call in session.send_calls)


def test_generated_method_descriptor_associates_exact_response_type() -> None:
    source = """
    const method = new grpc.web.MethodDescriptor(
      "/invented/InventedService/GetPopularV2",
      grpc.web.MethodType.UNARY,
      InventedRequest,
      InventedResponse,
      serialize,
      deserialize
    );
    """
    session = _Session({"approved": source})
    inspector = _inspector(session)
    session.emit_script("approved", APPROVED_URL, source)
    summary = inspector.close()
    assert summary.inspections[0].method_response_associations == (
        "GetPopularV2->InventedResponse",
    )


def test_protobuf_ts_message_type_resolves_minified_alias_and_field_table() -> None:
    source = """
    oeh = new runtime.MessageType(
      "invented.api.PopularResponse",
      [
        { no: 1, name: "invented_events", kind: "message", repeat: 1 },
        { no: 2, name: "invented_total", kind: "scalar", T: 5 }
      ]
    );
    const service = {
      name: "GetPopularV2",
      I: InventedRequest,
      O: oeh,
      kind: MethodKind.Unary
    };
    """
    session = _Session({"approved": source})
    inspector = _inspector(session)
    session.emit_script("approved", APPROVED_URL, source)
    inspection = inspector.close().inspections[0]

    assert inspection.response_type_candidates == ("invented.api.PopularResponse",)
    assert inspection.method_response_associations == (
        "GetPopularV2->invented.api.PopularResponse",
    )
    assert inspection.field_number_type_sequences == (
        "invented.api.PopularResponse=1:message:repeated,2:scalar-5:optional",
    )


def test_duplicate_script_and_source_length_mismatch_are_rejected_and_cleanup_is_safe() -> None:
    session = _Session({"approved": INVENTED_SOURCE})
    inspector = _inspector(session)
    session.emit_script("approved", APPROVED_URL, INVENTED_SOURCE)
    session.emit_script("approved", APPROVED_URL, INVENTED_SOURCE)
    session.sources["approved"] = INVENTED_SOURCE + "x"
    summary = inspector.close()

    assert summary.scripts_seen == 2
    assert summary.scripts_inspected == 0
    assert summary.rejection_counts == (
        ("duplicate-script", 1),
        ("script-source-length-mismatch", 1),
    )
    assert session.send_calls[-1][0] == "Debugger.disable"


def test_safe_summary_contract_rejects_complete_urls() -> None:
    with pytest.raises(PermanentSourceError, match="non-structural"):
        BrowserClientScriptInspection(
            hostname="client.example",
            sanitized_path_hash="0" * 64,
            source_hash_sha256="1" * 64,
            source_char_count=10,
            method_response_associations=("GetPopularV2->https://client.example/forbidden",),
        )
