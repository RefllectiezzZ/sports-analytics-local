"""Safe schema-agnostic protobuf structural inspection tests."""

from __future__ import annotations

import pytest

from sports_analytics.sources.browser.protobuf_wire import (
    ProtobufWireInspectionError,
    ProtobufWireLimits,
    inspect_protobuf_wire,
)


def test_wire_inspector_is_deterministic_and_value_blind() -> None:
    first = inspect_protobuf_wire(b"\x08\x01\x12\x03abc\x1d\x00\x00\x80?")
    second = inspect_protobuf_wire(b"\x08\x7f\x12\x03xyz\x1d\x00\x00\x00@")
    assert first.fingerprint_sha256 == second.fingerprint_sha256
    assert first.field_count == 3
    assert first.numeric_field_count == 2
    assert first.length_delimited_field_count == 1
    assert first.utf8_like_value_count == 1
    assert first.opaque_length_delimited_count == 0
    assert first.field_wire_shapes == ("1:0:1", "2:2:1", "3:5:1")


def test_wire_inspector_tracks_repeated_nested_shape_without_values() -> None:
    nested_a = b"\x08\x01\x12\x01a"
    nested_b = b"\x08\x02\x12\x01b"
    payload = (
        b"\x0a" + bytes([len(nested_a)]) + nested_a + b"\x0a" + bytes([len(nested_b)]) + nested_b
    )
    inspection = inspect_protobuf_wire(payload)
    assert inspection.nested_message_count == 2
    assert inspection.maximum_depth_observed == 1
    assert inspection.field_count == 6
    assert inspection.field_wire_shapes == (
        "1:2:2",
        "1.1:0:1",
        "1.2:2:1",
    )


@pytest.mark.parametrize(
    ("payload", "classification"),
    [
        (b"", "empty-payload"),
        (b"\x00", "field-number-zero"),
        (b"\x0b", "unsupported-wire-type"),
        (b"\x08\x80", "truncated-varint"),
        (b"\x09\x00", "truncated-fixed"),
        (b"\x0a\x02x", "truncated-length-delimited"),
    ],
)
def test_wire_inspector_rejects_invalid_or_truncated_values(
    payload: bytes,
    classification: str,
) -> None:
    with pytest.raises(ProtobufWireInspectionError, match=classification):
        inspect_protobuf_wire(payload)


def test_wire_inspector_enforces_total_field_and_nested_bounds() -> None:
    with pytest.raises(ProtobufWireInspectionError, match="total-byte-limit"):
        inspect_protobuf_wire(
            b"\x08\x01" * 3,
            limits=ProtobufWireLimits(
                maximum_total_bytes=4,
                maximum_nested_length=4,
            ),
        )
    with pytest.raises(ProtobufWireInspectionError, match="field-count-limit"):
        inspect_protobuf_wire(
            b"\x08\x01" * 3,
            limits=ProtobufWireLimits(maximum_fields=2),
        )
    with pytest.raises(ProtobufWireInspectionError, match="nested-length-limit"):
        inspect_protobuf_wire(
            b"\x0a\x03abc",
            limits=ProtobufWireLimits(maximum_nested_length=2),
        )
