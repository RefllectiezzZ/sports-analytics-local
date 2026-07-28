"""Fail-closed diagnostic JSON publication."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping, Sequence
from hashlib import sha256
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit
from uuid import uuid4

from sports_analytics.core.exceptions import ConfigurationError

_SECRET_MATERIAL: Final[re.Pattern[str]] = re.compile(
    r"(?i)(authorization|bearer|cookie|set-cookie|password|passwd|secret|token|"
    r"access_token|refresh_token|api-key|api_key|signature|credential|session|jwt)"
)
_AUTHORIZATION_VALUE: Final[re.Pattern[str]] = re.compile(
    r"(?i)^\s*(?:authorization\s*:|bearer\s+\S+)"
)
_EMBEDDED_SCHEME_URL: Final[re.Pattern[str]] = re.compile(
    r"(?i)(?<![a-z0-9+.-])[a-z][a-z0-9+.-]*://[^\s<>\"']+"
)
_EMBEDDED_PROTOCOL_RELATIVE_URL: Final[re.Pattern[str]] = re.compile(
    r"(?i)(?<![:/])//[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?(?::\d+)?(?:[/?#][^\s<>\"']*)?"
)
_EMBEDDED_WWW_URL: Final[re.Pattern[str]] = re.compile(
    r"(?i)(?<![a-z0-9_.-])www\.[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?"
    r"\.[a-z]{2,}(?::\d+)?(?:[/?#][^\s<>\"']*)?"
)
_REDACTION_MARKER: Final[re.Pattern[str]] = re.compile(
    r"<(?:redacted-[a-z0-9_-]+|truncated[^>]*)>",
    re.IGNORECASE,
)
_SAFE_JSON_PATH: Final[re.Pattern[str]] = re.compile(r"^\$(?:\[(?:\d+|key-\d+-[0-9a-f]{10})\])*$")
_SAFE_REDACTION_MARKERS: Final[frozenset[str]] = frozenset(
    {
        "<redacted-path>",
        "<redacted-email>",
        "<redacted-credential>",
        "<truncated>",
    }
)
_SAFE_SCHEMA_KEYS: Final[frozenset[str]] = frozenset({"cookie_banner_dismissed"})
_PROHIBITED_URL_FIELD: Final[re.Pattern[str]] = re.compile(r"(?i)(?:^|_)(?:url|uri)$")


class UnsafeDiagnosticError(ConfigurationError):
    """A safe-path-only diagnostic rejection."""


def scan_diagnostic_payload(value: Any, *, path: str = "$", parent_key: str | None = None) -> None:
    """Recursively reject secrets and every complete or protocol-relative URL."""
    if isinstance(value, Mapping):
        for index, (key, item) in enumerate(value.items()):
            key_text = str(key)
            child_path = _mapping_path(path, index=index, key_text=key_text)
            separated_key = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key_text)
            normalized_key = re.sub(
                r"[^a-z0-9]+",
                "_",
                separated_key.casefold(),
            ).strip("_")
            if _PROHIBITED_URL_FIELD.search(normalized_key):
                _reject(child_path, "prohibited-provenance-field")
            if _SECRET_MATERIAL.search(key_text) and key_text not in _SAFE_SCHEMA_KEYS:
                _reject(child_path, "secret-like-key")
            _scan_string(key_text, path=child_path, parent_key=None, scan_secret=False)
            scan_diagnostic_payload(item, path=child_path, parent_key=key_text)
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, item in enumerate(value):
            scan_diagnostic_payload(item, path=f"{path}[{index}]", parent_key=parent_key)
        return
    if isinstance(value, (bytes, bytearray)):
        _reject(path, "non-json-bytes")
    if not isinstance(value, str):
        return
    _scan_string(value, path=path, parent_key=parent_key, scan_secret=True)


def _scan_string(
    value: str,
    *,
    path: str,
    parent_key: str | None,
    scan_secret: bool,
) -> None:
    """Reject unsafe material in one mapping key or scalar string."""
    markers = _REDACTION_MARKER.findall(value)
    if any(marker not in _SAFE_REDACTION_MARKERS for marker in markers):
        _reject(path, "unapproved-redaction-marker")
    value_without_markers = value
    for marker in _SAFE_REDACTION_MARKERS:
        value_without_markers = value_without_markers.replace(marker, "")
    stripped = value_without_markers.strip()
    if not stripped:
        return
    if parent_key == "hostname" and _is_bare_hostname(stripped):
        return
    if scan_secret and _SECRET_MATERIAL.search(value_without_markers):
        _reject(path, "secret-like-string")
    if _AUTHORIZATION_VALUE.search(value_without_markers):
        _reject(path, "authorization-looking-string")
    if parent_key == "approved_path_template" and _is_static_path_template(stripped):
        return
    if _EMBEDDED_SCHEME_URL.search(stripped):
        _reject(path, "embedded-url-with-scheme")
    if _EMBEDDED_PROTOCOL_RELATIVE_URL.search(stripped):
        _reject(path, "protocol-relative-url")
    if _EMBEDDED_WWW_URL.search(stripped):
        _reject(path, "www-style-url")
    try:
        parsed = urlsplit(stripped)
    except ValueError:
        _reject(path, "malformed-url-like-string")
    url_like = (
        bool(parsed.scheme)
        or bool(parsed.netloc)
        or parsed.username is not None
        or parsed.password is not None
        or stripped.startswith(("/", "www."))
    )
    if url_like and parsed.query:
        _reject(path, "url-query-string")
    if url_like and parsed.fragment:
        _reject(path, "url-fragment")
    if parsed.scheme:
        _reject(path, "url-with-scheme")
    if parsed.netloc:
        _reject(path, "url-with-network-location")
    if parsed.username is not None or parsed.password is not None:
        _reject(path, "url-embedded-credentials")


def publish_diagnostic_json(path: Path, payload: Any) -> None:
    """Scan and atomically publish JSON, leaving no partial target."""
    scan_diagnostic_payload(payload)
    encoded = json.dumps(payload, sort_keys=True, indent=2)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(encoded, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _reject(path: str, classification: str) -> None:
    safe_path = path if _SAFE_JSON_PATH.fullmatch(path) else _opaque_path(path)
    safe_path = safe_path[:240]
    msg = f"diagnostic publication rejected at {safe_path}: {classification}"
    raise UnsafeDiagnosticError(msg)


def _mapping_path(path: str, *, index: int, key_text: str) -> str:
    digest = sha256(key_text.encode("utf-8", errors="replace")).hexdigest()[:10]
    return f"{path}[key-{index}-{digest}]"


def _opaque_path(path: str) -> str:
    digest = sha256(path.encode("utf-8", errors="replace")).hexdigest()[:10]
    return f"$[key-0-{digest}]"


def _is_static_path_template(value: str) -> bool:
    if not value.startswith("/") or value.startswith("//"):
        return False
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return (
        not parsed.scheme
        and not parsed.netloc
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
    )


def _is_bare_hostname(value: str) -> bool:
    return re.fullmatch(r"(?i)[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?", value) is not None
