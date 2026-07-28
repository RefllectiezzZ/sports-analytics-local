"""Typed, provider-owned two-stage event navigation planning."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from urllib.parse import urlsplit, urlunsplit

from sports_analytics.bookmakers.window import AcquisitionWindow
from sports_analytics.core.exceptions import PermanentSourceError
from sports_analytics.data.types import validate_identifier
from sports_analytics.sources.browser.limits import BrowserAcquisitionLimits
from sports_analytics.sources.browser.safety import validate_provider_navigation_url
from sports_analytics.sports.contracts import require_utc

MAX_EVENT_PATH_LENGTH = 500


@dataclass(frozen=True, slots=True)
class EventNavigationCandidate:
    """Ephemeral provider-derived event target discovered on an inventory surface."""

    source_event_id: str
    scheduled_start_utc: datetime
    provider_url: str

    def __post_init__(self) -> None:
        if not self.source_event_id.strip() or len(self.source_event_id) > 200:
            msg = "source_event_id must be non-empty and bounded"
            raise PermanentSourceError(msg)
        object.__setattr__(
            self,
            "scheduled_start_utc",
            require_utc(self.scheduled_start_utc, field_name="scheduled_start_utc"),
        )


@dataclass(frozen=True, slots=True)
class EventNavigationTarget:
    """Approved target with no persisted complete URL."""

    source_event_id: str
    scheduled_start_utc: datetime
    page_route_id: str
    approved_path_template: str
    path_hash_sha256: str
    ephemeral_url: str

    def __post_init__(self) -> None:
        validate_identifier(self.page_route_id, field_name="page_route_id")
        if not self.approved_path_template.startswith("/") or any(
            marker in self.approved_path_template for marker in ("?", "#")
        ):
            msg = "approved_path_template must be a static absolute path"
            raise PermanentSourceError(msg)
        if not re.fullmatch(r"[0-9a-f]{64}", self.path_hash_sha256):
            msg = "path_hash_sha256 must be a lowercase SHA-256 digest"
            raise PermanentSourceError(msg)


@dataclass(frozen=True, slots=True)
class EventNavigationPlan:
    """Deterministically ordered bounded event-detail plan."""

    provider_id: str
    sport: str
    acquisition_window: AcquisitionWindow
    targets: tuple[EventNavigationTarget, ...]
    load_policy: BrowserAcquisitionLimits = BrowserAcquisitionLimits()

    def __post_init__(self) -> None:
        validate_identifier(self.provider_id, field_name="provider_id")
        validate_identifier(self.sport, field_name="sport")
        if len(self.targets) > self.acquisition_window.maximum_events:
            msg = "event navigation plan exceeds acquisition maximum_events"
            raise PermanentSourceError(msg)
        identities = [item.source_event_id for item in self.targets]
        if len(identities) != len(set(identities)):
            msg = "event navigation plan contains duplicate source events"
            raise PermanentSourceError(msg)
        expected = tuple(
            sorted(
                self.targets,
                key=lambda item: (item.scheduled_start_utc, item.source_event_id),
            )
        )
        if self.targets != expected:
            msg = "event navigation plan targets must be deterministically ordered"
            raise PermanentSourceError(msg)


class NavigationPlanExecutor(Protocol):
    """Injectable Stage-B executor; offline tests need no Playwright."""

    def execute(self, plan: EventNavigationPlan) -> object:
        """Traverse an approved plan and return typed browser evidence."""


def build_event_navigation_plan(
    *,
    provider_id: str,
    sport: str,
    candidates: tuple[EventNavigationCandidate, ...],
    acquisition_window: AcquisitionWindow,
    allowed_hostnames: frozenset[str],
    approved_event_path_pattern: re.Pattern[str],
    approved_path_template: str,
) -> EventNavigationPlan:
    """Filter inventory candidates and approve provider-owned event paths."""
    targets: list[EventNavigationTarget] = []
    seen: set[str] = set()
    for candidate in sorted(
        candidates,
        key=lambda item: (item.scheduled_start_utc, item.source_event_id),
    ):
        if not acquisition_window.contains(candidate.scheduled_start_utc):
            continue
        if candidate.source_event_id in seen:
            msg = "duplicate source event navigation target"
            raise PermanentSourceError(msg)
        seen.add(candidate.source_event_id)
        approved = validate_provider_navigation_url(
            candidate.provider_url,
            allowed_hostnames=allowed_hostnames,
        )
        split = urlsplit(approved.url)
        if split.username is not None or split.password is not None:
            msg = "event navigation URL must not contain credentials"
            raise PermanentSourceError(msg)
        if split.query or split.fragment:
            msg = "event navigation URL must not contain query or fragment"
            raise PermanentSourceError(msg)
        if len(split.path) > MAX_EVENT_PATH_LENGTH:
            msg = "event navigation path exceeds the fixed bound"
            raise PermanentSourceError(msg)
        if approved_event_path_pattern.fullmatch(split.path) is None:
            msg = "event navigation path does not match the reviewed provider grammar"
            raise PermanentSourceError(msg)
        clean_url = urlunsplit(("https", split.netloc, split.path, "", ""))
        digest = hashlib.sha256(split.path.encode("utf-8")).hexdigest()
        targets.append(
            EventNavigationTarget(
                source_event_id=candidate.source_event_id,
                scheduled_start_utc=candidate.scheduled_start_utc,
                page_route_id=f"event-detail-{len(targets) + 1}",
                approved_path_template=approved_path_template,
                path_hash_sha256=digest,
                ephemeral_url=clean_url,
            )
        )
        if len(targets) == acquisition_window.maximum_events:
            break
    return EventNavigationPlan(
        provider_id=provider_id,
        sport=sport,
        acquisition_window=acquisition_window,
        targets=tuple(targets),
    )
