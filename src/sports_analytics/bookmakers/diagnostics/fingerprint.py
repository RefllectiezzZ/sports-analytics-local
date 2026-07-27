"""Structural JSON fingerprinting for diagnostic evidence."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from sports_analytics.bookmakers.diagnostics.redaction import redact_structure


def structural_fingerprint(value: Any) -> str:
    """Return a stable SHA-256 fingerprint of a JSON structure's shape."""
    shape = _shape(value)
    encoded = json.dumps(shape, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _shape(value: Any, *, depth: int = 0) -> Any:
    if depth > 10:
        return "object"
    if isinstance(value, dict):
        redacted = redact_structure(value, depth=depth)
        if not isinstance(redacted, dict):
            return "object"
        return {key: _shape(item, depth=depth + 1) for key, item in sorted(redacted.items())}
    if isinstance(value, list):
        if not value:
            return []
        return [_shape(value[0], depth=depth + 1)]
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if value is None:
        return "null"
    return "string"
