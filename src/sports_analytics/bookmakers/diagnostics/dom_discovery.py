"""Sanitized DOM structural discovery for bookmaker probe diagnostics."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
from typing import Any

from sports_analytics.bookmakers.diagnostics.redaction import redact_text

_MAX_CANDIDATES = 40
_MAX_TEXT_LENGTH = 80
_MAX_CLASS_TOKENS = 12
_HEXISH = re.compile(r"(?i)^(?:[a-f0-9]{8,}|[a-f0-9]{6,}[-_][a-f0-9-]{4,})$")
_LONG_NUMERIC = re.compile(r"^\d{5,}$")
_HASHED_CLASS = re.compile(r"(?i)(?:^|[-_])[a-f0-9]{6,}$")
_INTEREST_PATTERN = re.compile(
    r"(?i)(event|market|odd|price|selection|fixture|match|quote|bet|outcome|handicap)"
)


@dataclass(frozen=True, slots=True)
class DomDiscoveryCandidate:
    """One sanitized DOM node candidate for event/market/price discovery."""

    tag: str
    class_tokens: tuple[str, ...]
    data_testid: str | None
    data_qa: str | None
    aria_label: str | None
    short_text: str | None
    child_count: int
    structural_fingerprint: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def sanitize_class_token(token: str) -> str | None:
    """Strip random hex / long numeric class tokens; return None when discarded."""
    cleaned = token.strip()
    if not cleaned or len(cleaned) > 64:
        return None
    if _HEXISH.match(cleaned) or _LONG_NUMERIC.match(cleaned):
        return None
    if _HASHED_CLASS.search(cleaned) and not _INTEREST_PATTERN.search(cleaned):
        return None
    return cleaned


def discover_dom_candidates(
    html_fragment: str,
    *,
    maximum_candidates: int = _MAX_CANDIDATES,
) -> tuple[DomDiscoveryCandidate, ...]:
    """Extract bounded sanitized DOM candidates suggesting event/market/price UI."""
    if not html_fragment or "<" not in html_fragment:
        return ()
    parser = _DomCandidateParser(maximum_candidates=maximum_candidates)
    try:
        parser.feed(html_fragment)
        parser.close()
    except Exception:  # noqa: BLE001 - best-effort diagnostic parse
        return ()
    return tuple(parser.candidates[:maximum_candidates])


def _candidate_fingerprint(
    *,
    tag: str,
    class_tokens: tuple[str, ...],
    data_testid: str | None,
    data_qa: str | None,
    aria_label: str | None,
    child_count: int,
) -> str:
    shape = {
        "tag": tag,
        "class_tokens": list(class_tokens),
        "data_testid": data_testid,
        "data_qa": data_qa,
        "aria_label": aria_label,
        "child_count": child_count,
    }
    encoded = json.dumps(shape, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _looks_interesting(
    *,
    class_tokens: tuple[str, ...],
    data_testid: str | None,
    data_qa: str | None,
    aria_label: str | None,
    short_text: str | None,
) -> bool:
    haystacks = [
        *class_tokens,
        data_testid or "",
        data_qa or "",
        aria_label or "",
        short_text or "",
    ]
    return any(_INTEREST_PATTERN.search(part) for part in haystacks if part)


class _DomCandidateParser(HTMLParser):
    def __init__(self, *, maximum_candidates: int) -> None:
        super().__init__(convert_charrefs=True)
        self.maximum_candidates = maximum_candidates
        self.candidates: list[DomDiscoveryCandidate] = []
        self._stack: list[dict[str, Any]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {key.lower(): (value or "") for key, value in attrs}
        raw_classes = attr_map.get("class", "").split()
        class_tokens = tuple(
            token
            for token in (sanitize_class_token(item) for item in raw_classes)
            if token is not None
        )[:_MAX_CLASS_TOKENS]
        data_testid = (
            redact_text(attr_map["data-testid"])[:80] if "data-testid" in attr_map else None
        )
        data_qa = redact_text(attr_map["data-qa"])[:80] if "data-qa" in attr_map else None
        aria_label = (
            redact_text(attr_map["aria-label"])[:80] if "aria-label" in attr_map else None
        )
        node = {
            "tag": tag.lower(),
            "class_tokens": class_tokens,
            "data_testid": data_testid,
            "data_qa": data_qa,
            "aria_label": aria_label,
            "text_parts": [],
            "child_count": 0,
        }
        if self._stack:
            self._stack[-1]["child_count"] += 1
        self._stack.append(node)

    def handle_data(self, data: str) -> None:
        if not self._stack:
            return
        text = " ".join(data.split())
        if text:
            self._stack[-1]["text_parts"].append(text)

    def handle_endtag(self, tag: str) -> None:
        del tag
        if not self._stack:
            return
        node = self._stack.pop()
        if len(self.candidates) >= self.maximum_candidates:
            return
        short_text = None
        joined = " ".join(node["text_parts"]).strip()
        if joined:
            short_text = redact_text(joined[:_MAX_TEXT_LENGTH])
        class_tokens = tuple(node["class_tokens"])
        if not _looks_interesting(
            class_tokens=class_tokens,
            data_testid=node["data_testid"],
            data_qa=node["data_qa"],
            aria_label=node["aria_label"],
            short_text=short_text,
        ):
            return
        fingerprint = _candidate_fingerprint(
            tag=node["tag"],
            class_tokens=class_tokens,
            data_testid=node["data_testid"],
            data_qa=node["data_qa"],
            aria_label=node["aria_label"],
            child_count=int(node["child_count"]),
        )
        self.candidates.append(
            DomDiscoveryCandidate(
                tag=node["tag"],
                class_tokens=class_tokens,
                data_testid=node["data_testid"],
                data_qa=node["data_qa"],
                aria_label=node["aria_label"],
                short_text=short_text,
                child_count=int(node["child_count"]),
                structural_fingerprint=fingerprint,
            )
        )
