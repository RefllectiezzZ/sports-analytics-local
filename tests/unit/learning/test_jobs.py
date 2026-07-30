from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pytest

from sports_analytics.core.exceptions import PermanentJobError
from sports_analytics.jobs.context import JobExecutionContext
from sports_analytics.learning.jobs import (
    evaluate_retraining_trigger_handler,
    refresh_monitoring_handler,
    run_challenger_cycle_handler,
    settle_new_results_handler,
)
from sports_analytics.learning.lifecycle import RetrainingPolicy

NOW = datetime(2026, 7, 29, tzinfo=UTC)


def _context() -> JobExecutionContext:
    return JobExecutionContext(
        job_id="11111111-1111-4111-8111-111111111111",
        worker_id="22222222-2222-4222-8222-222222222222",
        attempt=1,
        maximum_attempts=1,
        claimed_at=NOW,
        lease_expires_at=NOW + timedelta(minutes=5),
        logger=logging.getLogger("test.lifecycle.jobs"),
    )


def _payload() -> dict[str, object]:
    return {
        "snapshot_refs": [{"snapshot_id": "a" * 64, "checksum_sha256": "b" * 64}],
        "artifact_refs": [{"artifact_id": "c" * 64, "checksum_sha256": "d" * 64}],
        "scope": {"sport_code": "football", "competition_id": "competition"},
        "policy_id": RetrainingPolicy().policy_id,
        "cutoff_utc": "2026-07-29T00:00:00.000000Z",
    }


@pytest.mark.parametrize(
    "handler",
    [
        settle_new_results_handler,
        refresh_monitoring_handler,
        evaluate_retraining_trigger_handler,
        run_challenger_cycle_handler,
    ],
)
def test_closed_loop_jobs_accept_only_id_checksum_payloads(handler) -> None:
    payload = _payload()
    payload["analysis_relative_directory"] = "caller/controlled/path"
    with pytest.raises(PermanentJobError, match="fields are not exact"):
        handler(_context(), payload)

    payload.pop("analysis_relative_directory")
    with pytest.raises(PermanentJobError, match="requires"):
        handler(_context(), payload)
