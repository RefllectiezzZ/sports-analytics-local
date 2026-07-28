"""Sanitize diagnostic evidence before persistence or reporting."""

from __future__ import annotations

import re
from typing import Any

_ABSOLUTE_PATH = re.compile(r"(?:[A-Za-z]:\\|/)[^\s\"']+")
_TOKEN_LIKE = re.compile(
    r"(?i)(authorization|cookie|set-cookie|bearer|session|token|password|secret|api[_-]?key)"
)
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def redact_text(value: str) -> str:
    """Remove credentials, tokens, emails, and absolute paths from text."""
    sanitized = _ABSOLUTE_PATH.sub("<redacted-path>", value)
    sanitized = _EMAIL.sub("<redacted-email>", sanitized)
    sanitized = _TOKEN_LIKE.sub("<redacted-credential>", sanitized)
    return sanitized


def redact_structure(value: Any, *, depth: int = 0) -> Any:
    """Recursively redact sensitive strings from JSON-like structures."""
    if depth > 12:
        return "<truncated>"
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {
            redact_text(str(key)): redact_structure(item, depth=depth + 1)
            for key, item in value.items()
            if not _TOKEN_LIKE.search(str(key))
        }
    if isinstance(value, list):
        return [redact_structure(item, depth=depth + 1) for item in value[:50]]
    return value


def sanitize_sample_payload(payload: dict[str, Any], *, maximum_keys: int = 20) -> dict[str, Any]:
    """Return a fixed, value-free summary of one JSON object."""
    return {
        "root_kind": "object",
        "top_level_key_count": min(len(payload), maximum_keys),
        "sample_truncated": len(payload) > maximum_keys,
    }
