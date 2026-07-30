"""Bounded schema-agnostic protobuf wire inspection for safe diagnostics."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from sports_analytics.core.exceptions import PermanentSourceError


class ProtobufWireInspectionError(PermanentSourceError):
    """Safe structural rejection without exposing protobuf field values."""

    def __init__(self, classification: str) -> None:
        super().__init__(classification)
        self.classification = classification


@dataclass(frozen=True, slots=True)
class ProtobufWireLimits:
    """Conservative recursion and work bounds for structural inspection."""

    maximum_total_bytes: int = 524_288
    maximum_fields: int = 4_096
    maximum_field_number: int = 536_870_911
    maximum_recursion_depth: int = 5
    maximum_nested_length: int = 262_144

    def __post_init__(self) -> None:
        values = (
            self.maximum_total_bytes,
            self.maximum_fields,
            self.maximum_field_number,
            self.maximum_recursion_depth,
            self.maximum_nested_length,
        )
        if any(isinstance(value, bool) or value < 1 for value in values):
            msg = "protobuf wire limits must be positive integers"
            raise PermanentSourceError(msg)
        if self.maximum_nested_length > self.maximum_total_bytes:
            msg = "nested protobuf length must fit inside the total byte bound"
            raise PermanentSourceError(msg)


@dataclass(frozen=True, slots=True)
class ProtobufWireInspection:
    """Safe structural facts and deterministic fingerprint only."""

    fingerprint_sha256: str
    field_count: int
    numeric_field_count: int
    length_delimited_field_count: int
    utf8_like_value_count: int
    nested_message_count: int
    maximum_depth_observed: int
    opaque_length_delimited_count: int
    field_wire_shapes: tuple[str, ...]


@dataclass(slots=True)
class _InspectionBudget:
    fields: int = 0
    numeric: int = 0
    length_delimited: int = 0
    utf8_like: int = 0
    nested: int = 0
    opaque: int = 0
    maximum_depth: int = 0


def inspect_protobuf_wire(
    payload: bytes,
    *,
    limits: ProtobufWireLimits | None = None,
) -> ProtobufWireInspection:
    """Inspect standard protobuf wire types without assigning field semantics."""
    resolved = limits or ProtobufWireLimits()
    if not payload:
        raise ProtobufWireInspectionError("protobuf-empty-payload")
    if len(payload) > resolved.maximum_total_bytes:
        raise ProtobufWireInspectionError("protobuf-total-byte-limit-exceeded")
    budget = _InspectionBudget()
    signature = _inspect_message(payload, depth=0, limits=resolved, budget=budget)
    encoded = json.dumps(signature, sort_keys=True, separators=(",", ":")).encode("ascii")
    return ProtobufWireInspection(
        fingerprint_sha256=hashlib.sha256(encoded).hexdigest(),
        field_count=budget.fields,
        numeric_field_count=budget.numeric,
        length_delimited_field_count=budget.length_delimited,
        utf8_like_value_count=budget.utf8_like,
        nested_message_count=budget.nested,
        maximum_depth_observed=budget.maximum_depth,
        opaque_length_delimited_count=budget.opaque,
        field_wire_shapes=_flatten_signature(signature),
    )


def _inspect_message(
    payload: bytes,
    *,
    depth: int,
    limits: ProtobufWireLimits,
    budget: _InspectionBudget,
) -> tuple[tuple[int, int, int, object | None], ...]:
    if depth > limits.maximum_recursion_depth:
        raise ProtobufWireInspectionError("protobuf-recursion-depth-exceeded")
    budget.maximum_depth = max(budget.maximum_depth, depth)
    offset = 0
    occurrences: dict[tuple[int, int, object | None], int] = {}
    while offset < len(payload):
        key, offset = _read_varint(payload, offset)
        field_number = key >> 3
        wire_type = key & 0x07
        if field_number == 0:
            raise ProtobufWireInspectionError("protobuf-field-number-zero")
        if field_number > limits.maximum_field_number:
            raise ProtobufWireInspectionError("protobuf-field-number-limit-exceeded")
        if wire_type not in {0, 1, 2, 5}:
            raise ProtobufWireInspectionError("protobuf-unsupported-wire-type")
        budget.fields += 1
        if budget.fields > limits.maximum_fields:
            raise ProtobufWireInspectionError("protobuf-field-count-limit-exceeded")
        nested_signature: object | None = None
        if wire_type == 0:
            _value, offset = _read_varint(payload, offset)
            budget.numeric += 1
        elif wire_type == 1:
            offset = _consume_fixed(payload, offset, 8)
            budget.numeric += 1
        elif wire_type == 5:
            offset = _consume_fixed(payload, offset, 4)
            budget.numeric += 1
        else:
            length, offset = _read_varint(payload, offset)
            if length > limits.maximum_nested_length:
                raise ProtobufWireInspectionError("protobuf-nested-length-limit-exceeded")
            end = offset + length
            if end > len(payload):
                raise ProtobufWireInspectionError("protobuf-truncated-length-delimited")
            value = payload[offset:end]
            offset = end
            budget.length_delimited += 1
            utf8_like = _is_utf8_like(value)
            if utf8_like:
                budget.utf8_like += 1
            nested_message = False
            if value and depth < limits.maximum_recursion_depth:
                nested_budget = _InspectionBudget()
                try:
                    candidate = _inspect_message(
                        value,
                        depth=depth + 1,
                        limits=limits,
                        budget=nested_budget,
                    )
                except ProtobufWireInspectionError:
                    candidate = ()
                if candidate:
                    nested_message = True
                    budget.fields += nested_budget.fields
                    if budget.fields > limits.maximum_fields:
                        raise ProtobufWireInspectionError("protobuf-field-count-limit-exceeded")
                    budget.numeric += nested_budget.numeric
                    budget.length_delimited += nested_budget.length_delimited
                    budget.utf8_like += nested_budget.utf8_like
                    budget.nested += nested_budget.nested + 1
                    budget.opaque += nested_budget.opaque
                    budget.maximum_depth = max(
                        budget.maximum_depth,
                        nested_budget.maximum_depth,
                    )
                    nested_signature = candidate
            if not utf8_like and not nested_message:
                budget.opaque += 1
        occurrence_key = (field_number, wire_type, nested_signature)
        occurrences[occurrence_key] = occurrences.get(occurrence_key, 0) + 1
    return tuple(
        (field_number, wire_type, count, nested_signature)
        for (field_number, wire_type, nested_signature), count in sorted(
            occurrences.items(),
            key=lambda item: (item[0][0], item[0][1], repr(item[0][2])),
        )
    )


def _read_varint(payload: bytes, offset: int) -> tuple[int, int]:
    value = 0
    for index in range(10):
        if offset >= len(payload):
            raise ProtobufWireInspectionError("protobuf-truncated-varint")
        byte = payload[offset]
        offset += 1
        if index == 9 and byte > 1:
            raise ProtobufWireInspectionError("protobuf-varint-overflow")
        value |= (byte & 0x7F) << (index * 7)
        if not byte & 0x80:
            return value, offset
    raise ProtobufWireInspectionError("protobuf-varint-overflow")


def _consume_fixed(payload: bytes, offset: int, length: int) -> int:
    end = offset + length
    if end > len(payload):
        raise ProtobufWireInspectionError("protobuf-truncated-fixed")
    return end


def _is_utf8_like(value: bytes) -> bool:
    if not value:
        return False
    try:
        decoded = value.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return all(character.isprintable() or character in "\r\n\t" for character in decoded)


def _flatten_signature(
    signature: tuple[tuple[int, int, int, object | None], ...],
    *,
    prefix: tuple[int, ...] = (),
) -> tuple[str, ...]:
    shapes: list[str] = []
    for field_number, wire_type, count, nested in signature:
        path = (*prefix, field_number)
        shapes.append(f"{'.'.join(str(item) for item in path)}:{wire_type}:{count}")
        if isinstance(nested, tuple):
            shapes.extend(_flatten_signature(nested, prefix=path))
    return tuple(shapes)
