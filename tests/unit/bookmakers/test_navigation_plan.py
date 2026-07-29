"""Provider-owned event-detail navigation plan security tests."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest

from sports_analytics.bookmakers.navigation import (
    EventNavigationCandidate,
    build_event_navigation_plan,
)
from sports_analytics.bookmakers.window import AcquisitionWindow
from sports_analytics.core.exceptions import PermanentSourceError

NOW = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
PATTERN = re.compile(r"/event/[a-z0-9-]{1,80}")


def _window() -> AcquisitionWindow:
    return AcquisitionWindow(
        window_start_utc=NOW,
        window_end_utc=NOW + timedelta(hours=24),
        maximum_events=2,
    )


def test_navigation_plan_filters_orders_bounds_and_hashes_paths() -> None:
    plan = build_event_navigation_plan(
        provider_id="provider-pt",
        sport="football",
        candidates=(
            EventNavigationCandidate(
                "event-b",
                NOW + timedelta(hours=2),
                "https://www.provider.test/event/event-b",
            ),
            EventNavigationCandidate(
                "event-a",
                NOW + timedelta(hours=1),
                "https://www.provider.test/event/event-a",
            ),
            EventNavigationCandidate(
                "event-c",
                NOW + timedelta(hours=30),
                "https://www.provider.test/event/event-c",
            ),
        ),
        acquisition_window=_window(),
        allowed_hostnames=frozenset({"www.provider.test"}),
        approved_event_path_pattern=PATTERN,
        approved_path_template="/event/{event-route-id}",
    )
    assert [target.source_event_id for target in plan.targets] == ["event-a", "event-b"]
    assert all(len(target.path_hash_sha256) == 64 for target in plan.targets)


def test_navigation_plan_fails_closed_instead_of_truncating_eligible_events() -> None:
    with pytest.raises(PermanentSourceError, match="exceed"):
        build_event_navigation_plan(
            provider_id="provider-pt",
            sport="football",
            candidates=tuple(
                EventNavigationCandidate(
                    f"event-{suffix}",
                    NOW + timedelta(hours=offset),
                    f"https://www.provider.test/event/event-{suffix}",
                )
                for suffix, offset in (("a", 1), ("b", 2), ("c", 3))
            ),
            acquisition_window=_window(),
            allowed_hostnames=frozenset({"www.provider.test"}),
            approved_event_path_pattern=PATTERN,
            approved_path_template="/event/{event-route-id}",
        )


@pytest.mark.parametrize(
    "url",
    [
        "https://other.test/event/event-a",
        "https://user:pass@www.provider.test/event/event-a",
        "https://www.provider.test/event/event-a?token=x",
        "https://www.provider.test/event/event-a#fragment",
        "https://www.provider.test/account/login",
        "http://www.provider.test/event/event-a",
    ],
)
def test_navigation_plan_rejects_unapproved_targets(url: str) -> None:
    with pytest.raises(PermanentSourceError):
        build_event_navigation_plan(
            provider_id="provider-pt",
            sport="football",
            candidates=(EventNavigationCandidate("event-a", NOW, url),),
            acquisition_window=_window(),
            allowed_hostnames=frozenset({"www.provider.test"}),
            approved_event_path_pattern=PATTERN,
            approved_path_template="/event/{event-route-id}",
        )
