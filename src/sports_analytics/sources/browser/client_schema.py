"""Passive bounded inspection of already-loaded approved public client scripts."""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from queue import Empty, Full, Queue
from typing import Any
from urllib.parse import urlsplit

from sports_analytics.core.exceptions import PermanentSourceError
from sports_analytics.sources.browser.cdp_streaming import CdpSession
from sports_analytics.sources.browser.contracts import (
    BrowserClientSchemaSummary,
    BrowserClientScriptInspection,
)

_SAFE_SYMBOL = re.compile(r"[A-Za-z_][A-Za-z0-9_.]{0,159}")
_FIELD_NUMBER = re.compile(r"(?:\bno\b|\bnumber\b|\bfieldNumber\b)\s*:\s*(\d{1,4})")
_KIND = re.compile(r"\bkind\s*:\s*[\"'](scalar|enum|message|map)[\"']")
_SCALAR_TYPE = re.compile(r"\bT\s*:\s*(\d{1,2})")
_REPEATED = re.compile(r"\b(?:repeat|repeated)\s*:\s*(?:true|1)")
_RESPONSE_TYPE = re.compile(
    r"\b(?:responseType|responseCtor)\s*[:=]\s*"
    r"(?:\(\)\s*=>\s*)?([A-Za-z_$][A-Za-z0-9_$.]{0,159})"
)


@dataclass(frozen=True, slots=True)
class ClientSchemaInspectionLimits:
    maximum_scripts_seen: int = 256
    maximum_scripts_inspected: int = 64
    maximum_source_chars_per_script: int = 6_000_000
    maximum_total_source_chars: int = 16_000_000
    maximum_matches_per_script: int = 128
    maximum_field_table_chars: int = 20_000
    maximum_field_entries_per_table: int = 64
    maximum_wall_time_ms: int = 12_000
    maximum_queue_size: int = 512

    def __post_init__(self) -> None:
        values = tuple(getattr(self, name) for name in self.__dataclass_fields__)
        if any(isinstance(value, bool) or value < 1 for value in values):
            msg = "client schema inspection limits must be positive integers"
            raise PermanentSourceError(msg)
        if self.maximum_total_source_chars < self.maximum_source_chars_per_script:
            msg = "total script source limit must cover one source"
            raise PermanentSourceError(msg)


@dataclass(frozen=True, slots=True)
class _ScriptObservation:
    script_id: str
    url: str
    source_length: int


class PassiveClientSchemaInspector:
    """Inspect approved script sources already present in the active page."""

    def __init__(
        self,
        *,
        session: CdpSession,
        allowed_hostnames: frozenset[str],
        target_rpc_symbols: tuple[str, ...],
        limits: ClientSchemaInspectionLimits | None = None,
        monotonic_clock: Callable[[], float] | None = None,
    ) -> None:
        if not allowed_hostnames:
            msg = "client schema inspection requires exact approved hostnames"
            raise PermanentSourceError(msg)
        if not target_rpc_symbols or any(
            _SAFE_SYMBOL.fullmatch(item) is None for item in target_rpc_symbols
        ):
            msg = "target RPC symbols must be safe exact identifiers"
            raise PermanentSourceError(msg)
        self._session = session
        self._allowed_hostnames = frozenset(item.casefold() for item in allowed_hostnames)
        self._target_rpc_symbols = tuple(sorted(set(target_rpc_symbols)))
        self._limits = limits or ClientSchemaInspectionLimits()
        self._clock = monotonic_clock or time.monotonic
        self._started_at = self._clock()
        self._queue: Queue[_ScriptObservation] = Queue(maxsize=self._limits.maximum_queue_size)
        self._seen_script_ids: set[str] = set()
        self._inspections: list[BrowserClientScriptInspection] = []
        self._rejections: dict[str, int] = {}
        self._bounds_hit: set[str] = set()
        self._scripts_seen = 0
        self._total_source_chars = 0
        self._closed = False

    def attach(self) -> None:
        """Enable Debugger before navigation so parsed scripts are observable."""
        self._session.on("Debugger.scriptParsed", self._on_script_parsed)
        self._session.send("Debugger.enable")

    def _on_script_parsed(self, event: dict[str, Any]) -> None:
        if self._closed:
            return
        self._scripts_seen += 1
        if self._scripts_seen > self._limits.maximum_scripts_seen:
            self._bounds_hit.add("script-seen-limit")
            self._reject("script-seen-limit")
            return
        script_id = event.get("scriptId")
        url = event.get("url")
        length = event.get("length")
        if (
            not isinstance(script_id, str)
            or not script_id
            or not isinstance(url, str)
            or not url
            or isinstance(length, bool)
            or not isinstance(length, int)
            or length < 1
        ):
            self._reject("invalid-script-metadata")
            return
        try:
            self._queue.put_nowait(
                _ScriptObservation(script_id=script_id, url=url, source_length=length)
            )
        except Full:
            self._bounds_hit.add("script-queue-limit")
            self._reject("script-queue-limit")

    def drain(self) -> None:
        """Process queued script metadata outside CDP callbacks."""
        if self._closed:
            return
        while True:
            try:
                item = self._queue.get_nowait()
            except Empty:
                return
            if self._elapsed_ms() >= self._limits.maximum_wall_time_ms:
                self._bounds_hit.add("wall-time-limit")
                self._reject("wall-time-limit")
                self._discard_queue("wall-time-limit")
                return
            self._inspect(item)

    def close(self) -> BrowserClientSchemaSummary:
        if not self._closed:
            self.drain()
            self._closed = True
            try:
                self._session.send("Debugger.disable")
            except Exception:  # noqa: BLE001 - cleanup is best-effort
                pass
        return BrowserClientSchemaSummary(
            scripts_seen=self._scripts_seen,
            scripts_inspected=len(self._inspections),
            scripts_rejected=sum(self._rejections.values()),
            total_source_chars_inspected=self._total_source_chars,
            rejection_counts=tuple(self._rejections.items()),
            bounds_hit=tuple(self._bounds_hit),
            inspections=tuple(self._inspections),
        )

    def _inspect(self, item: _ScriptObservation) -> None:
        if item.script_id in self._seen_script_ids:
            self._reject("duplicate-script")
            return
        self._seen_script_ids.add(item.script_id)
        approved = _approve_script_url(item.url, self._allowed_hostnames)
        if approved is None:
            self._reject("unapproved-script")
            return
        if len(self._inspections) >= self._limits.maximum_scripts_inspected:
            self._bounds_hit.add("script-inspection-limit")
            self._reject("script-inspection-limit")
            return
        if item.source_length > self._limits.maximum_source_chars_per_script:
            self._bounds_hit.add("per-script-source-limit")
            self._reject("per-script-source-limit")
            return
        if self._total_source_chars + item.source_length > self._limits.maximum_total_source_chars:
            self._bounds_hit.add("total-source-limit")
            self._reject("total-source-limit")
            return
        try:
            result = self._session.send(
                "Debugger.getScriptSource",
                {"scriptId": item.script_id},
            )
        except Exception:  # noqa: BLE001 - diagnostic failure is isolated
            self._reject("script-source-unavailable")
            return
        source = result.get("scriptSource") if isinstance(result, dict) else None
        if not isinstance(source, str) or len(source) != item.source_length:
            self._reject("script-source-length-mismatch")
            return
        if len(source) > self._limits.maximum_source_chars_per_script:
            self._bounds_hit.add("per-script-source-limit")
            self._reject("per-script-source-limit")
            return
        self._total_source_chars += len(source)
        inspection = _inspect_source(
            source,
            hostname=approved[0],
            path_hash=approved[1],
            rpc_symbols=self._target_rpc_symbols,
            limits=self._limits,
        )
        self._inspections.append(inspection)

    def _elapsed_ms(self) -> float:
        return (self._clock() - self._started_at) * 1000

    def _reject(self, reason: str) -> None:
        self._rejections[reason] = self._rejections.get(reason, 0) + 1

    def _discard_queue(self, reason: str) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except Empty:
                return
            self._reject(reason)


def _approve_script_url(
    url: str,
    allowed_hostnames: frozenset[str],
) -> tuple[str, str] | None:
    try:
        parsed = urlsplit(url)
        hostname = (parsed.hostname or "").casefold()
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.casefold() != "https"
        or port not in {None, 443}
        or hostname not in allowed_hostnames
        or not parsed.path
    ):
        return None
    path_hash = hashlib.sha256(parsed.path.encode("utf-8")).hexdigest()
    return hostname, path_hash


def _inspect_source(
    source: str,
    *,
    hostname: str,
    path_hash: str,
    rpc_symbols: tuple[str, ...],
    limits: ClientSchemaInspectionLimits,
) -> BrowserClientScriptInspection:
    runtime_families = _runtime_families(source)
    message_aliases, generated_sequences = _message_type_descriptors(
        source,
        limits=limits,
    )
    matched_rpc: list[str] = []
    response_types: set[str] = set()
    associations: set[str] = set()
    match_count = 0
    for symbol in rpc_symbols:
        pattern = re.compile(rf"(?<![A-Za-z0-9_$]){re.escape(symbol)}(?![A-Za-z0-9_$])")
        for match in pattern.finditer(source):
            match_count += 1
            if match_count > limits.maximum_matches_per_script:
                break
            matched_rpc.append(symbol)
            window = source[max(0, match.start() - 800) : min(len(source), match.end() + 800)]
            for response_match in _RESPONSE_TYPE.finditer(window):
                candidate = _safe_type_name(response_match.group(1))
                if candidate is None:
                    continue
                candidate = message_aliases.get(candidate, candidate)
                response_types.add(candidate)
                associations.add(f"{symbol}->{candidate}")
            for candidate in _descriptor_response_types(window, rpc_symbol=symbol):
                candidate = message_aliases.get(candidate, candidate)
                response_types.add(candidate)
                associations.add(f"{symbol}->{candidate}")
        if match_count > limits.maximum_matches_per_script:
            break
    field_sequences = tuple(
        sorted(set(_field_table_sequences(source, limits=limits)) | set(generated_sequences))
    )
    descriptors: set[str] = set()
    for value in sorted(associations | set(field_sequences)):
        descriptors.add(hashlib.sha256(value.encode("utf-8")).hexdigest())
    return BrowserClientScriptInspection(
        hostname=hostname,
        sanitized_path_hash=path_hash,
        source_hash_sha256=hashlib.sha256(source.encode("utf-8")).hexdigest(),
        source_char_count=len(source),
        runtime_families=runtime_families,
        rpc_symbols=tuple(matched_rpc),
        response_type_candidates=tuple(response_types),
        method_response_associations=tuple(associations),
        field_number_type_sequences=field_sequences,
        descriptor_fingerprints=tuple(descriptors),
    )


def _runtime_families(source: str) -> tuple[str, ...]:
    markers = {
        "binary-reader-generated": (
            "BinaryReader",
            "readMessage",
            "readString",
            "readInt64",
            "readUint64",
            "readEnum",
        ),
        "connect-web": ("createPromiseClient", "createConnectTransport"),
        "google-protobuf": ("jspb.BinaryReader", "jspb.Message"),
        "grpc-web": ("grpc.web", "GrpcWebClientBase"),
        "protobuf-ts": ("internalBinaryRead", "reflectionInfo_"),
        "protobufjs": ("$protobuf.Reader", "protobufjs"),
    }
    return tuple(
        family
        for family, candidates in markers.items()
        if any(candidate in source for candidate in candidates)
    )


def _descriptor_response_types(window: str, *, rpc_symbol: str) -> tuple[str, ...]:
    candidates: set[str] = set()
    method_descriptor = re.compile(
        rf"MethodDescriptor\s*\(\s*[\"'][^\"']*{re.escape(rpc_symbol)}[\"']"
        r"\s*,\s*[^,]+,\s*[^,]+,\s*([A-Za-z_$][A-Za-z0-9_$.]{0,159})\s*,"
    )
    for match in method_descriptor.finditer(window):
        candidate = _safe_type_name(match.group(1))
        if candidate is not None:
            candidates.add(candidate)
    named_object = re.compile(
        rf"(?:name|methodName)\s*:\s*[\"']{re.escape(rpc_symbol)}[\"']"
        r"[\s\S]{0,500}?\bO\s*:\s*([A-Za-z_$][A-Za-z0-9_$.]{0,159})"
    )
    for match in named_object.finditer(window):
        candidate = _safe_type_name(match.group(1))
        if candidate is not None:
            candidates.add(candidate)
    return tuple(sorted(candidates))


def _field_table_sequences(
    source: str,
    *,
    limits: ClientSchemaInspectionLimits,
) -> tuple[str, ...]:
    sequences: set[str] = set()
    for fields_match in re.finditer(r"\bfields\s*[:=]\s*\[", source):
        block = _bounded_bracket_block(
            source,
            opening_index=fields_match.end() - 1,
            maximum_chars=limits.maximum_field_table_chars,
        )
        if block is None:
            continue
        entries: list[str] = []
        for number_match in _FIELD_NUMBER.finditer(block):
            if len(entries) >= limits.maximum_field_entries_per_table:
                break
            vicinity = block[number_match.start() : min(len(block), number_match.end() + 240)]
            kind_match = _KIND.search(vicinity)
            kind = kind_match.group(1) if kind_match else "unknown"
            scalar_match = _SCALAR_TYPE.search(vicinity)
            if kind == "scalar" and scalar_match:
                kind = f"scalar-{scalar_match.group(1)}"
            cardinality = "repeated" if _REPEATED.search(vicinity) else "optional"
            entries.append(f"{int(number_match.group(1))}:{kind}:{cardinality}")
        if entries:
            normalized = ",".join(entries)
            if len(normalized) <= 256:
                sequences.add(normalized)
            else:
                sequences.add("sha256:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest())
    return tuple(sorted(sequences))


def _message_type_descriptors(
    source: str,
    *,
    limits: ClientSchemaInspectionLimits,
) -> tuple[dict[str, str], tuple[str, ...]]:
    aliases: dict[str, str] = {}
    sequences: set[str] = set()
    pattern = re.compile(
        r"([A-Za-z_$][A-Za-z0-9_$]{0,79})\s*=\s*new\s+"
        r"[A-Za-z_$][A-Za-z0-9_$.]{0,119}MessageType\s*\(\s*"
        r"[\"']([A-Za-z_][A-Za-z0-9_.]{0,159})[\"']\s*,\s*\["
    )
    for match in pattern.finditer(source):
        alias = _safe_type_name(match.group(1))
        type_name = _safe_type_name(match.group(2))
        if alias is None or type_name is None:
            continue
        aliases[alias] = type_name
        block = _bounded_bracket_block(
            source,
            opening_index=match.end() - 1,
            maximum_chars=limits.maximum_field_table_chars,
        )
        if block is None:
            continue
        entries: list[str] = []
        for number_match in _FIELD_NUMBER.finditer(block):
            if len(entries) >= limits.maximum_field_entries_per_table:
                break
            vicinity = block[number_match.start() : min(len(block), number_match.end() + 240)]
            kind_match = _KIND.search(vicinity)
            kind = kind_match.group(1) if kind_match else "unknown"
            scalar_match = _SCALAR_TYPE.search(vicinity)
            if kind == "scalar" and scalar_match:
                kind = f"scalar-{scalar_match.group(1)}"
            cardinality = "repeated" if _REPEATED.search(vicinity) else "optional"
            entries.append(f"{int(number_match.group(1))}:{kind}:{cardinality}")
        if entries:
            normalized = f"{type_name}=" + ",".join(entries)
            if len(normalized) <= 256:
                sequences.add(normalized)
            else:
                sequences.add(
                    f"{type_name}=sha256:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()
                )
    return aliases, tuple(sorted(sequences))


def _bounded_bracket_block(
    source: str,
    *,
    opening_index: int,
    maximum_chars: int,
) -> str | None:
    if opening_index >= len(source) or source[opening_index] != "[":
        return None
    depth = 0
    end_limit = min(len(source), opening_index + maximum_chars)
    for index in range(opening_index, end_limit):
        char = source[index]
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                return source[opening_index : index + 1]
    return None


def _safe_type_name(value: str) -> str | None:
    normalized = value.replace("$", "")
    if _SAFE_SYMBOL.fullmatch(normalized) is None:
        return None
    return normalized
