"""Sanitized DOM structural discovery for bookmaker probe diagnostics."""

from __future__ import annotations

import hashlib
import json
import re
from html.parser import HTMLParser
from typing import Any

from sports_analytics.sources.browser.contracts import (
    DOM_HYDRATION_MARKERS,
    DOM_STRUCTURAL_MARKERS,
    SAFE_DOM_TAGS,
    BrowserDomCandidate,
)

_MAX_CANDIDATES = 40
_MAX_INPUT_CHARS = 65_536
_MAX_TEXT_BUFFER_CHARS = 8_192
_STRUCTURAL_SIGNAL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("card", re.compile("card", re.IGNORECASE)),
    ("event", re.compile("event", re.IGNORECASE)),
    ("fixture", re.compile("fixture", re.IGNORECASE)),
    ("handicap", re.compile("handicap", re.IGNORECASE)),
    ("market", re.compile("market", re.IGNORECASE)),
    ("match", re.compile("match", re.IGNORECASE)),
    ("odds", re.compile(r"odds?", re.IGNORECASE)),
    ("outcome", re.compile("outcome", re.IGNORECASE)),
    ("price", re.compile("price", re.IGNORECASE)),
    ("quote", re.compile("quote", re.IGNORECASE)),
    ("selection", re.compile("selection", re.IGNORECASE)),
)
_HYDRATION_SIGNAL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("ng-state", re.compile(r"ng-(?:transfer-)?state", re.IGNORECASE)),
    ("transfer-state", re.compile(r"transfer-?state", re.IGNORECASE)),
    ("hydration", re.compile("hydration", re.IGNORECASE)),
)
_DECIMAL_ODDS = re.compile(r"^(?:1(?:[.,]\d{1,3})|[2-9]\d?(?:[.,]\d{1,3}))$")
_EXCLUDED_PATTERN = re.compile(
    r"(?i)(account|login|log-in|register|registration|sign[-_ ]?up|deposit|withdraw|"
    r"payment|bet[-_ ]?slip|personal)"
)


def canonicalize_structural_markers(*values: str) -> tuple[str, ...]:
    """Map arbitrary ephemeral signals to a fixed sorted marker vocabulary."""
    markers = {
        marker
        for marker, pattern in _STRUCTURAL_SIGNAL_PATTERNS
        if any(pattern.search(value) is not None for value in values)
    }
    if not markers <= DOM_STRUCTURAL_MARKERS:
        return ()
    return tuple(sorted(markers))


def _canonical_hydration_marker(*values: str) -> str | None:
    for marker, pattern in _HYDRATION_SIGNAL_PATTERNS:
        if any(pattern.search(value) is not None for value in values):
            return marker if marker in DOM_HYDRATION_MARKERS else None
    return None


def discover_dom_candidates(
    html_fragment: str,
    *,
    maximum_candidates: int = _MAX_CANDIDATES,
) -> tuple[BrowserDomCandidate, ...]:
    """Extract bounded structural-only candidates from ephemeral page HTML."""
    if not html_fragment or "<" not in html_fragment:
        return ()
    parser = _DomCandidateParser(maximum_candidates=maximum_candidates)
    try:
        parser.feed(html_fragment[:_MAX_INPUT_CHARS])
        parser.close()
    except Exception:  # noqa: BLE001 - best-effort diagnostic parse
        return ()
    return tuple(parser.candidates[:maximum_candidates])


def _candidate_fingerprint(
    *,
    tag: str,
    structural_markers: tuple[str, ...],
    hydration_marker: str | None,
    child_count: int,
    candidate_classification: str,
    content_shape_fingerprint: str | None,
) -> str:
    shape = {
        "tag": tag,
        "structural_markers": list(structural_markers),
        "hydration_marker": hydration_marker,
        "child_count": child_count,
        "candidate_classification": candidate_classification,
        "content_shape_fingerprint": content_shape_fingerprint,
    }
    encoded = json.dumps(shape, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _ancestor_fingerprint(stack: list[dict[str, Any]]) -> str:
    ancestors = [
        {
            "tag": node["tag"],
            "structural_markers": list(node["structural_markers"]),
            "child_count": int(node["child_count"]),
        }
        for node in stack[-3:]
    ]
    encoded = json.dumps(ancestors, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _json_shape(value: Any, *, depth: int = 0) -> Any:
    if depth >= 6:
        return "depth-limit"
    if isinstance(value, dict):
        child_shapes = sorted(
            json.dumps(_json_shape(item, depth=depth + 1), sort_keys=True)
            for item in list(value.values())[:16]
        )
        return {"kind": "object", "size": min(len(value), 16), "values": child_shapes}
    if isinstance(value, list):
        return {
            "kind": "array",
            "size": min(len(value), 16),
            "items": [_json_shape(item, depth=depth + 1) for item in value[:16]],
        }
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    return "string"


def _shape_fingerprint(value: Any) -> str:
    encoded = json.dumps(
        _json_shape(value),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class _DomCandidateParser(HTMLParser):
    def __init__(self, *, maximum_candidates: int) -> None:
        super().__init__(convert_charrefs=True)
        self.maximum_candidates = maximum_candidates
        self.candidates: list[BrowserDomCandidate] = []
        self._stack: list[dict[str, Any]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {key.lower(): (value or "") for key, value in attrs}
        structural_attribute_values = (
            attr_map.get("class", ""),
            attr_map.get("id", ""),
            attr_map.get("data-testid", ""),
            attr_map.get("data-qa", ""),
        )
        structural_markers = canonicalize_structural_markers(*structural_attribute_values)
        hydration_marker = _canonical_hydration_marker(
            attr_map.get("id", ""),
            attr_map.get("data-testid", ""),
            attr_map.get("data-qa", ""),
        )
        structured_state_candidate = (
            tag.lower() == "script"
            and attr_map.get("type", "").casefold() == "application/json"
            and hydration_marker is not None
        )
        if structured_state_candidate:
            structural_markers = ()
        node: dict[str, Any] = {
            "tag": tag.lower(),
            "structural_markers": structural_markers,
            "hydration_marker": hydration_marker if structured_state_candidate else None,
            "text_parts": [],
            "child_count": 0,
            "structured_state_candidate": structured_state_candidate,
            "excluded": (
                bool(self._stack and self._stack[-1]["excluded"])
                or (
                    tag.lower() in {"script", "input", "textarea", "form"}
                    and not structured_state_candidate
                )
                or any(_EXCLUDED_PATTERN.search(value) for value in attr_map.values())
            ),
        }
        if self._stack:
            self._stack[-1]["child_count"] += 1
        self._stack.append(node)

    def handle_data(self, data: str) -> None:
        if not self._stack or self._stack[-1]["excluded"]:
            return
        text = " ".join(data.split())
        buffered = sum(len(item) for item in self._stack[-1]["text_parts"])
        if text and buffered < _MAX_TEXT_BUFFER_CHARS:
            self._stack[-1]["text_parts"].append(text)

    def handle_endtag(self, tag: str) -> None:
        del tag
        if not self._stack:
            return
        node = self._stack.pop()
        if self._stack and not node["excluded"] and node["text_parts"] and node["tag"] != "script":
            self._stack[-1]["text_parts"].extend(node["text_parts"][:4])
        if len(self.candidates) >= self.maximum_candidates:
            return
        if node["excluded"]:
            return
        joined = " ".join(node["text_parts"]).strip()
        structured_public_state = False
        content_shape_fingerprint: str | None = None
        if node["structured_state_candidate"]:
            try:
                structured = json.loads(joined)
            except (json.JSONDecodeError, TypeError):
                return
            structured_public_state = isinstance(structured, (dict, list))
            if not structured_public_state:
                return
            content_shape_fingerprint = _shape_fingerprint(structured)
        structural_markers = (
            ()
            if structured_public_state
            else tuple(
                sorted(
                    set(node["structural_markers"]) | set(canonicalize_structural_markers(joined))
                )
            )
        )
        odds_text = joined if _DECIMAL_ODDS.fullmatch(joined) is not None else None
        if not structured_public_state and not odds_text and not structural_markers:
            return
        if node["tag"] not in SAFE_DOM_TAGS:
            return
        candidate_classification = (
            "hydration-structure"
            if structured_public_state
            else ("decimal-odds" if odds_text is not None else "structural-interest")
        )
        fingerprint = _candidate_fingerprint(
            tag=node["tag"],
            structural_markers=structural_markers,
            hydration_marker=node["hydration_marker"],
            child_count=int(node["child_count"]),
            candidate_classification=candidate_classification,
            content_shape_fingerprint=content_shape_fingerprint,
        )
        self.candidates.append(
            BrowserDomCandidate(
                tag=node["tag"],
                structural_markers=structural_markers,
                hydration_marker=node["hydration_marker"],
                child_count=int(node["child_count"]),
                candidate_classification=candidate_classification,
                decimal_odds_text=odds_text,
                structural_fingerprint=fingerprint,
                ancestor_structural_fingerprint=_ancestor_fingerprint(self._stack),
                content_shape_fingerprint=content_shape_fingerprint,
            )
        )
