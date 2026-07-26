"""Repositories for bookmaker acquisition operational tables."""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime
from typing import Any

from sports_analytics.core.exceptions import DatabaseIntegrityError, RepositoryError
from sports_analytics.data.codec import (
    dumps_canonical_json,
    format_utc_timestamp,
    loads_canonical_json,
    parse_utc_timestamp,
    utc_now,
)
from sports_analytics.data.database import require_active_transaction
from sports_analytics.data.types import (
    JsonValue,
    normalize_uuid,
    validate_identifier,
    validate_plain_text,
    validate_sha256_checksum,
    validate_strict_int,
)
from sports_analytics.sports.contracts import require_utc


class BookmakerRepository:
    """Typed bookmaker acquisition repository. Does not own connections."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def update_acquisition_run_status(
        self,
        *,
        run_id: str,
        status: str,
        finished_at: datetime,
        failure_classification: str,
        snapshot_id: str | None = None,
        block_reason: str | None = None,
    ) -> None:
        """Update terminal fields on an existing acquisition run."""
        require_active_transaction(
            self._connection,
            operation="BookmakerRepository.update_acquisition_run_status",
        )
        normalized_run = normalize_uuid(run_id)
        status_value = validate_identifier(status, field_name="status")
        classification = validate_plain_text(
            failure_classification,
            field_name="failure_classification",
        )
        finished = format_utc_timestamp(require_utc(finished_at, field_name="finished_at"))
        snap = None if snapshot_id is None else normalize_uuid(snapshot_id)
        block = (
            None
            if block_reason is None
            else validate_identifier(block_reason, field_name="block_reason")
        )
        self._connection.execute(
            """
            UPDATE bookmaker_acquisition_runs
            SET status = ?, finished_at = ?, failure_classification = ?,
                snapshot_id = COALESCE(?, snapshot_id), block_reason = ?
            WHERE id = ?
            """,
            (status_value, finished, classification, snap, block, normalized_run),
        )

    def insert_acquisition_run(
        self,
        *,
        provider_id: str,
        sport: str,
        acquisition_cycle_id: str,
        adapter_version: str,
        status: str,
        observed_at: datetime,
        started_at: datetime,
        finished_at: datetime,
        failure_classification: str,
        warnings: JsonValue,
        snapshot_id: str | None = None,
        block_reason: str | None = None,
        run_id: str | uuid.UUID | None = None,
    ) -> str:
        """Insert one acquisition run; ignore exact identity replays."""
        require_active_transaction(
            self._connection,
            operation="BookmakerRepository.insert_acquisition_run",
        )
        normalized_id = normalize_uuid(run_id)
        provider = validate_identifier(provider_id, field_name="provider_id")
        sport_code = validate_identifier(sport, field_name="sport")
        cycle = validate_identifier(acquisition_cycle_id, field_name="acquisition_cycle_id")
        adapter = validate_identifier(adapter_version, field_name="adapter_version")
        status_value = validate_identifier(status, field_name="status")
        classification = validate_plain_text(
            failure_classification,
            field_name="failure_classification",
        )
        warnings_json = dumps_canonical_json(warnings)
        observed = format_utc_timestamp(require_utc(observed_at, field_name="observed_at"))
        started = format_utc_timestamp(require_utc(started_at, field_name="started_at"))
        finished = format_utc_timestamp(require_utc(finished_at, field_name="finished_at"))
        block = (
            None
            if block_reason is None
            else validate_identifier(block_reason, field_name="block_reason")
        )
        snap = None if snapshot_id is None else normalize_uuid(snapshot_id)
        existing = self._connection.execute(
            """
            SELECT id, status, snapshot_id, block_reason, failure_classification, warnings_json
            FROM bookmaker_acquisition_runs
            WHERE provider_id = ? AND acquisition_cycle_id = ? AND sport = ?
            """,
            (provider, cycle, sport_code),
        ).fetchone()
        if existing is not None:
            if (
                str(existing["status"]) != status_value
                or (None if existing["snapshot_id"] is None else str(existing["snapshot_id"]))
                != snap
                or (None if existing["block_reason"] is None else str(existing["block_reason"]))
                != block
                or str(existing["failure_classification"]) != classification
                or str(existing["warnings_json"]) != warnings_json
            ):
                raise DatabaseIntegrityError(
                    "bookmaker acquisition run identity conflicts on replay"
                )
            return str(existing["id"])
        try:
            self._connection.execute(
                """
                INSERT INTO bookmaker_acquisition_runs (
                    id, provider_id, sport, acquisition_cycle_id, adapter_version,
                    status, observed_at, started_at, finished_at, snapshot_id,
                    block_reason, failure_classification, warnings_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized_id,
                    provider,
                    sport_code,
                    cycle,
                    adapter,
                    status_value,
                    observed,
                    started,
                    finished,
                    snap,
                    block,
                    classification,
                    warnings_json,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise DatabaseIntegrityError("bookmaker acquisition run insert conflict") from exc
        return normalized_id

    def insert_acquisition_attempt(
        self,
        *,
        run_id: str,
        attempt_number: int,
        started_at: datetime,
        finished_at: datetime,
        outcome: str,
        failure_classification: str | None = None,
        detail_code: str | None = None,
        attempt_id: str | uuid.UUID | None = None,
    ) -> str:
        """Insert one acquisition attempt; ignore exact identity replays."""
        require_active_transaction(
            self._connection,
            operation="BookmakerRepository.insert_acquisition_attempt",
        )
        normalized_id = normalize_uuid(attempt_id)
        normalized_run = normalize_uuid(run_id)
        number = validate_strict_int(attempt_number, field_name="attempt_number", minimum=1)
        outcome_value = validate_identifier(outcome, field_name="outcome")
        classification = (
            None
            if failure_classification is None
            else validate_plain_text(failure_classification, field_name="failure_classification")
        )
        detail = (
            None
            if detail_code is None
            else validate_identifier(detail_code, field_name="detail_code")
        )
        started = format_utc_timestamp(require_utc(started_at, field_name="started_at"))
        finished = format_utc_timestamp(require_utc(finished_at, field_name="finished_at"))
        existing = self._connection.execute(
            """
            SELECT id, outcome, failure_classification, detail_code
            FROM bookmaker_acquisition_attempts
            WHERE run_id = ? AND attempt_number = ?
            """,
            (normalized_run, number),
        ).fetchone()
        if existing is not None:
            if (
                str(existing["outcome"]) != outcome_value
                or (
                    None
                    if existing["failure_classification"] is None
                    else str(existing["failure_classification"])
                )
                != classification
                or (None if existing["detail_code"] is None else str(existing["detail_code"]))
                != detail
            ):
                raise DatabaseIntegrityError(
                    "bookmaker acquisition attempt identity conflicts on replay"
                )
            return str(existing["id"])
        try:
            self._connection.execute(
                """
                INSERT INTO bookmaker_acquisition_attempts (
                    id, run_id, attempt_number, started_at, finished_at,
                    outcome, failure_classification, detail_code
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized_id,
                    normalized_run,
                    number,
                    started,
                    finished,
                    outcome_value,
                    classification,
                    detail,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise DatabaseIntegrityError("bookmaker acquisition attempt insert conflict") from exc
        return normalized_id

    def upsert_scheduler_anchor(
        self,
        *,
        provider_id: str,
        sport: str,
        first_due_at: datetime,
        anchor_set_at: datetime,
    ) -> None:
        """Persist a restart-stable first-cycle due time for one provider/sport."""
        require_active_transaction(
            self._connection,
            operation="BookmakerRepository.upsert_scheduler_anchor",
        )
        provider = validate_identifier(provider_id, field_name="provider_id")
        sport_code = validate_identifier(sport, field_name="sport")
        first_due = format_utc_timestamp(require_utc(first_due_at, field_name="first_due_at"))
        set_at = format_utc_timestamp(require_utc(anchor_set_at, field_name="anchor_set_at"))
        self._connection.execute(
            """
            INSERT INTO bookmaker_scheduler_anchors (
                provider_id, sport, first_due_at, anchor_set_at
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(provider_id, sport) DO NOTHING
            """,
            (provider, sport_code, first_due, set_at),
        )

    def get_scheduler_anchor(
        self,
        *,
        provider_id: str,
        sport: str,
    ) -> dict[str, JsonValue] | None:
        """Return the persisted scheduler anchor for one provider/sport."""
        provider = validate_identifier(provider_id, field_name="provider_id")
        sport_code = validate_identifier(sport, field_name="sport")
        row = self._connection.execute(
            """
            SELECT provider_id, sport, first_due_at, anchor_set_at
            FROM bookmaker_scheduler_anchors
            WHERE provider_id = ? AND sport = ?
            """,
            (provider, sport_code),
        ).fetchone()
        if row is None:
            return None
        return {
            "provider_id": str(row["provider_id"]),
            "sport": str(row["sport"]),
            "first_due_at_utc": str(row["first_due_at"]),
            "anchor_set_at_utc": str(row["anchor_set_at"]),
        }

    def get_acquisition_run(
        self,
        *,
        provider_id: str,
        sport: str,
        acquisition_cycle_id: str,
    ) -> dict[str, JsonValue] | None:
        """Return one acquisition run by natural key."""
        provider = validate_identifier(provider_id, field_name="provider_id")
        sport_code = validate_identifier(sport, field_name="sport")
        cycle = validate_identifier(acquisition_cycle_id, field_name="acquisition_cycle_id")
        row = self._connection.execute(
            """
            SELECT id, provider_id, sport, acquisition_cycle_id, adapter_version,
                   status, observed_at, started_at, finished_at, snapshot_id,
                   block_reason, failure_classification, warnings_json
            FROM bookmaker_acquisition_runs
            WHERE provider_id = ? AND sport = ? AND acquisition_cycle_id = ?
            """,
            (provider, sport_code, cycle),
        ).fetchone()
        if row is None:
            return None
        return {
            "id": str(row["id"]),
            "provider_id": str(row["provider_id"]),
            "sport": str(row["sport"]),
            "acquisition_cycle_id": str(row["acquisition_cycle_id"]),
            "adapter_version": str(row["adapter_version"]),
            "status": str(row["status"]),
            "observed_at_utc": str(row["observed_at"]),
            "started_at_utc": str(row["started_at"]),
            "finished_at_utc": str(row["finished_at"]),
            "snapshot_id": (None if row["snapshot_id"] is None else str(row["snapshot_id"])),
            "block_reason": (None if row["block_reason"] is None else str(row["block_reason"])),
            "failure_classification": str(row["failure_classification"]),
            "warnings": loads_canonical_json(str(row["warnings_json"])),
        }

    def upsert_provider_status(
        self,
        *,
        provider_id: str,
        sport: str,
        status: str,
        updated_at: datetime,
        last_attempted_at: datetime | None = None,
        last_successful_at: datetime | None = None,
        last_valid_snapshot_id: str | None = None,
        snapshot_age_seconds: int | None = None,
        events_observed: int = 0,
        valid_quotes_observed: int = 0,
        unresolved_events: int = 0,
        rejected_markets: int = 0,
        warnings: JsonValue | None = None,
        block_failure_classification: str | None = None,
        next_eligible_at: datetime | None = None,
        adapter_version: str | None = None,
        preserve_last_valid_snapshot: bool = False,
    ) -> dict[str, JsonValue]:
        """Upsert current provider status, optionally preserving last valid snapshot."""
        require_active_transaction(
            self._connection,
            operation="BookmakerRepository.upsert_provider_status",
        )
        provider = validate_identifier(provider_id, field_name="provider_id")
        sport_code = validate_identifier(sport, field_name="sport")
        status_value = validate_identifier(status, field_name="status")
        updated = format_utc_timestamp(require_utc(updated_at, field_name="updated_at"))
        warnings_json = dumps_canonical_json([] if warnings is None else warnings)
        events = validate_strict_int(events_observed, field_name="events_observed", minimum=0)
        quotes = validate_strict_int(
            valid_quotes_observed, field_name="valid_quotes_observed", minimum=0
        )
        unresolved = validate_strict_int(
            unresolved_events, field_name="unresolved_events", minimum=0
        )
        rejected = validate_strict_int(rejected_markets, field_name="rejected_markets", minimum=0)
        age = (
            None
            if snapshot_age_seconds is None
            else validate_strict_int(
                snapshot_age_seconds, field_name="snapshot_age_seconds", minimum=0
            )
        )
        block = (
            None
            if block_failure_classification is None
            else validate_plain_text(
                block_failure_classification,
                field_name="block_failure_classification",
            )
        )
        adapter = (
            None
            if adapter_version is None
            else validate_identifier(adapter_version, field_name="adapter_version")
        )
        attempted = (
            None
            if last_attempted_at is None
            else format_utc_timestamp(
                require_utc(last_attempted_at, field_name="last_attempted_at")
            )
        )
        successful = (
            None
            if last_successful_at is None
            else format_utc_timestamp(
                require_utc(last_successful_at, field_name="last_successful_at")
            )
        )
        next_eligible = (
            None
            if next_eligible_at is None
            else format_utc_timestamp(require_utc(next_eligible_at, field_name="next_eligible_at"))
        )
        snapshot = (
            None if last_valid_snapshot_id is None else normalize_uuid(last_valid_snapshot_id)
        )
        existing = self.get_provider_status(provider, sport_code)
        if preserve_last_valid_snapshot and existing is not None:
            existing_snap = existing.get("last_valid_snapshot_id")
            if isinstance(existing_snap, str) and existing_snap:
                snapshot = existing_snap
            existing_age = existing.get("snapshot_age_seconds")
            if age is None and isinstance(existing_age, int):
                age = existing_age

        self._connection.execute(
            """
            INSERT INTO bookmaker_provider_status (
                provider_id, sport, status, last_attempted_at, last_successful_at,
                last_valid_snapshot_id, snapshot_age_seconds, events_observed,
                valid_quotes_observed, unresolved_events, rejected_markets,
                warnings_json, block_failure_classification, next_eligible_at,
                adapter_version, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(provider_id, sport) DO UPDATE SET
                status = excluded.status,
                last_attempted_at = excluded.last_attempted_at,
                last_successful_at = COALESCE(
                    excluded.last_successful_at,
                    bookmaker_provider_status.last_successful_at
                ),
                last_valid_snapshot_id = COALESCE(
                    excluded.last_valid_snapshot_id,
                    bookmaker_provider_status.last_valid_snapshot_id
                ),
                snapshot_age_seconds = COALESCE(
                    excluded.snapshot_age_seconds,
                    bookmaker_provider_status.snapshot_age_seconds
                ),
                events_observed = excluded.events_observed,
                valid_quotes_observed = excluded.valid_quotes_observed,
                unresolved_events = excluded.unresolved_events,
                rejected_markets = excluded.rejected_markets,
                warnings_json = excluded.warnings_json,
                block_failure_classification = excluded.block_failure_classification,
                next_eligible_at = excluded.next_eligible_at,
                adapter_version = COALESCE(
                    excluded.adapter_version,
                    bookmaker_provider_status.adapter_version
                ),
                updated_at = excluded.updated_at
            """,
            (
                provider,
                sport_code,
                status_value,
                attempted,
                successful,
                snapshot,
                age,
                events,
                quotes,
                unresolved,
                rejected,
                warnings_json,
                block,
                next_eligible,
                adapter,
                updated,
            ),
        )
        status_row = self.get_provider_status(provider, sport_code)
        if status_row is None:
            raise RepositoryError("provider status upsert did not persist")
        return status_row

    def register_snapshot(
        self,
        *,
        snapshot_id: str,
        provider_id: str,
        sport: str,
        schema_version: str,
        checksum_sha256: str,
        relative_path: str,
        observed_at: datetime,
        registered_at: datetime,
        acquisition_cycle_id: str,
    ) -> str:
        """Register a bookmaker snapshot; ignore exact identity replays."""
        require_active_transaction(
            self._connection,
            operation="BookmakerRepository.register_snapshot",
        )
        snap = normalize_uuid(snapshot_id)
        provider = validate_identifier(provider_id, field_name="provider_id")
        sport_code = validate_identifier(sport, field_name="sport")
        schema = validate_identifier(schema_version, field_name="schema_version")
        checksum = validate_sha256_checksum(checksum_sha256)
        path = validate_plain_text(relative_path, field_name="relative_path")
        if path.startswith(("/", "\\")) or ".." in path.split("/"):
            msg = "relative_path must be a non-absolute relative path"
            raise RepositoryError(msg)
        cycle = validate_identifier(acquisition_cycle_id, field_name="acquisition_cycle_id")
        observed = format_utc_timestamp(require_utc(observed_at, field_name="observed_at"))
        registered = format_utc_timestamp(require_utc(registered_at, field_name="registered_at"))
        existing = self._connection.execute(
            """
            SELECT snapshot_id, checksum_sha256, relative_path, acquisition_cycle_id
            FROM bookmaker_snapshot_registrations
            WHERE snapshot_id = ?
            """,
            (snap,),
        ).fetchone()
        if existing is not None:
            if (
                str(existing["checksum_sha256"]) != checksum
                or str(existing["relative_path"]) != path
                or str(existing["acquisition_cycle_id"]) != cycle
            ):
                raise DatabaseIntegrityError(
                    "bookmaker snapshot registration identity conflicts on replay"
                )
            return snap
        try:
            self._connection.execute(
                """
                INSERT INTO bookmaker_snapshot_registrations (
                    snapshot_id, provider_id, sport, schema_version, checksum_sha256,
                    relative_path, observed_at, registered_at, acquisition_cycle_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snap,
                    provider,
                    sport_code,
                    schema,
                    checksum,
                    path,
                    observed,
                    registered,
                    cycle,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise DatabaseIntegrityError("bookmaker snapshot registration insert conflict") from exc
        return snap

    def insert_scheduler_cycle(
        self,
        *,
        provider_id: str,
        sport: str,
        scheduled_for: datetime,
        enqueued_at: datetime,
        job_id: str,
        suppressed_duplicate: bool = False,
        cycle_id: str | uuid.UUID | None = None,
    ) -> tuple[str, bool]:
        """Insert a scheduler cycle row.

        Returns ``(cycle_id, inserted)``. Exact unique key collisions are treated
        as suppressed duplicates (idempotent replay) when the stored job matches.
        """
        require_active_transaction(
            self._connection,
            operation="BookmakerRepository.insert_scheduler_cycle",
        )
        normalized_id = normalize_uuid(cycle_id)
        provider = validate_identifier(provider_id, field_name="provider_id")
        sport_code = validate_identifier(sport, field_name="sport")
        scheduled = format_utc_timestamp(require_utc(scheduled_for, field_name="scheduled_for"))
        enqueued = format_utc_timestamp(require_utc(enqueued_at, field_name="enqueued_at"))
        job = normalize_uuid(job_id)
        suppressed = 1 if suppressed_duplicate else 0
        existing = self._connection.execute(
            """
            SELECT id, job_id, suppressed_duplicate
            FROM bookmaker_scheduler_cycles
            WHERE provider_id = ? AND sport = ? AND scheduled_for = ?
            """,
            (provider, sport_code, scheduled),
        ).fetchone()
        if existing is not None:
            return str(existing["id"]), False
        try:
            self._connection.execute(
                """
                INSERT INTO bookmaker_scheduler_cycles (
                    id, provider_id, sport, scheduled_for, enqueued_at,
                    job_id, suppressed_duplicate
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized_id,
                    provider,
                    sport_code,
                    scheduled,
                    enqueued,
                    job,
                    suppressed,
                ),
            )
        except sqlite3.IntegrityError:
            again = self._connection.execute(
                """
                SELECT id FROM bookmaker_scheduler_cycles
                WHERE provider_id = ? AND sport = ? AND scheduled_for = ?
                """,
                (provider, sport_code, scheduled),
            ).fetchone()
            if again is None:
                raise DatabaseIntegrityError("bookmaker scheduler cycle insert conflict") from None
            return str(again["id"]), False
        return normalized_id, True

    def insert_fallback_decision(
        self,
        *,
        preferred_provider: str,
        reason_code: str,
        attempted: JsonValue,
        created_at: datetime,
        selected_provider: str | None = None,
        cached_used: bool = False,
        cached_age_seconds: int | None = None,
        decision_id: str | uuid.UUID | None = None,
    ) -> str:
        """Insert one fallback decision row."""
        require_active_transaction(
            self._connection,
            operation="BookmakerRepository.insert_fallback_decision",
        )
        normalized_id = normalize_uuid(decision_id)
        preferred = validate_identifier(preferred_provider, field_name="preferred_provider")
        reason = validate_identifier(reason_code, field_name="reason_code")
        selected = (
            None
            if selected_provider is None
            else validate_identifier(selected_provider, field_name="selected_provider")
        )
        age = (
            None
            if cached_age_seconds is None
            else validate_strict_int(cached_age_seconds, field_name="cached_age_seconds", minimum=0)
        )
        created = format_utc_timestamp(require_utc(created_at, field_name="created_at"))
        try:
            self._connection.execute(
                """
                INSERT INTO bookmaker_fallback_decisions (
                    id, preferred_provider, selected_provider, cached_used,
                    cached_age_seconds, reason_code, attempted_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized_id,
                    preferred,
                    selected,
                    1 if cached_used else 0,
                    age,
                    reason,
                    dumps_canonical_json(attempted),
                    created,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise DatabaseIntegrityError("bookmaker fallback decision insert conflict") from exc
        return normalized_id

    def insert_drift_finding(
        self,
        *,
        provider_id: str,
        code: str,
        severity: str,
        message: str,
        observed_at: datetime,
        run_id: str | None = None,
        finding_id: str | uuid.UUID | None = None,
    ) -> str:
        """Insert one parser/provider drift finding."""
        require_active_transaction(
            self._connection,
            operation="BookmakerRepository.insert_drift_finding",
        )
        normalized_id = normalize_uuid(finding_id)
        provider = validate_identifier(provider_id, field_name="provider_id")
        code_value = validate_identifier(code, field_name="code")
        severity_value = validate_identifier(severity, field_name="severity")
        text = validate_plain_text(message, field_name="message")
        observed = format_utc_timestamp(require_utc(observed_at, field_name="observed_at"))
        run = None if run_id is None else normalize_uuid(run_id)
        try:
            self._connection.execute(
                """
                INSERT INTO bookmaker_drift_findings (
                    id, provider_id, run_id, code, severity, message, observed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (normalized_id, provider, run, code_value, severity_value, text, observed),
            )
        except sqlite3.IntegrityError as exc:
            raise DatabaseIntegrityError("bookmaker drift finding insert conflict") from exc
        return normalized_id

    def get_provider_status(
        self,
        provider_id: str,
        sport: str,
    ) -> dict[str, JsonValue] | None:
        """Return current provider/sport status as a JSON-compatible mapping."""
        provider = validate_identifier(provider_id, field_name="provider_id")
        sport_code = validate_identifier(sport, field_name="sport")
        row = self._connection.execute(
            "SELECT * FROM bookmaker_provider_status WHERE provider_id = ? AND sport = ?",
            (provider, sport_code),
        ).fetchone()
        if row is None:
            return None
        return self._provider_status_from_row(row)

    def list_provider_statuses(self) -> tuple[dict[str, JsonValue], ...]:
        """Return all provider/sport statuses in deterministic order."""
        rows = self._connection.execute(
            """
            SELECT * FROM bookmaker_provider_status
            ORDER BY provider_id, sport
            """
        ).fetchall()
        return tuple(self._provider_status_from_row(row) for row in rows)

    def list_snapshot_registrations(
        self,
        *,
        provider_id: str | None = None,
        sport: str | None = None,
    ) -> tuple[dict[str, JsonValue], ...]:
        """List bookmaker snapshot registrations without absolute paths."""
        clauses: list[str] = []
        params: list[Any] = []
        if provider_id is not None:
            clauses.append("provider_id = ?")
            params.append(validate_identifier(provider_id, field_name="provider_id"))
        if sport is not None:
            clauses.append("sport = ?")
            params.append(validate_identifier(sport, field_name="sport"))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._connection.execute(
            f"""
            SELECT snapshot_id, provider_id, sport, schema_version, checksum_sha256,
                   relative_path, observed_at, registered_at, acquisition_cycle_id
            FROM bookmaker_snapshot_registrations
            {where}
            ORDER BY observed_at, provider_id, sport, snapshot_id
            """,
            params,
        ).fetchall()
        return tuple(
            {
                "snapshot_id": str(row["snapshot_id"]),
                "provider_id": str(row["provider_id"]),
                "sport": str(row["sport"]),
                "schema_version": str(row["schema_version"]),
                "checksum_sha256": str(row["checksum_sha256"]),
                "relative_path": str(row["relative_path"]),
                "observed_at_utc": str(row["observed_at"]),
                "registered_at_utc": str(row["registered_at"]),
                "acquisition_cycle_id": str(row["acquisition_cycle_id"]),
            }
            for row in rows
        )

    def get_snapshot_registration(self, snapshot_id: str) -> dict[str, JsonValue] | None:
        """Return one snapshot registration by id."""
        snap = normalize_uuid(snapshot_id)
        row = self._connection.execute(
            """
            SELECT snapshot_id, provider_id, sport, schema_version, checksum_sha256,
                   relative_path, observed_at, registered_at, acquisition_cycle_id
            FROM bookmaker_snapshot_registrations
            WHERE snapshot_id = ?
            """,
            (snap,),
        ).fetchone()
        if row is None:
            return None
        return {
            "snapshot_id": str(row["snapshot_id"]),
            "provider_id": str(row["provider_id"]),
            "sport": str(row["sport"]),
            "schema_version": str(row["schema_version"]),
            "checksum_sha256": str(row["checksum_sha256"]),
            "relative_path": str(row["relative_path"]),
            "observed_at_utc": str(row["observed_at"]),
            "registered_at_utc": str(row["registered_at"]),
            "acquisition_cycle_id": str(row["acquisition_cycle_id"]),
        }

    def latest_scheduler_cycle(
        self,
        *,
        provider_id: str,
        sport: str,
    ) -> dict[str, JsonValue] | None:
        """Return the latest scheduler cycle for a provider/sport pair."""
        provider = validate_identifier(provider_id, field_name="provider_id")
        sport_code = validate_identifier(sport, field_name="sport")
        row = self._connection.execute(
            """
            SELECT id, provider_id, sport, scheduled_for, enqueued_at, job_id,
                   suppressed_duplicate
            FROM bookmaker_scheduler_cycles
            WHERE provider_id = ? AND sport = ?
            ORDER BY scheduled_for DESC, id DESC
            LIMIT 1
            """,
            (provider, sport_code),
        ).fetchone()
        if row is None:
            return None
        return {
            "id": str(row["id"]),
            "provider_id": str(row["provider_id"]),
            "sport": str(row["sport"]),
            "scheduled_for_utc": str(row["scheduled_for"]),
            "enqueued_at_utc": str(row["enqueued_at"]),
            "job_id": str(row["job_id"]),
            "suppressed_duplicate": int(row["suppressed_duplicate"]) == 1,
        }

    @staticmethod
    def _provider_status_from_row(row: sqlite3.Row) -> dict[str, JsonValue]:
        warnings_raw = loads_canonical_json(str(row["warnings_json"]))
        return {
            "provider_id": str(row["provider_id"]),
            "sport": str(row["sport"]),
            "status": str(row["status"]),
            "last_attempted_at_utc": (
                None if row["last_attempted_at"] is None else str(row["last_attempted_at"])
            ),
            "last_successful_at_utc": (
                None if row["last_successful_at"] is None else str(row["last_successful_at"])
            ),
            "last_valid_snapshot_id": (
                None
                if row["last_valid_snapshot_id"] is None
                else str(row["last_valid_snapshot_id"])
            ),
            "snapshot_age_seconds": (
                None if row["snapshot_age_seconds"] is None else int(row["snapshot_age_seconds"])
            ),
            "events_observed": int(row["events_observed"]),
            "valid_quotes_observed": int(row["valid_quotes_observed"]),
            "unresolved_events": int(row["unresolved_events"]),
            "rejected_markets": int(row["rejected_markets"]),
            "warnings": warnings_raw,
            "block_failure_classification": (
                None
                if row["block_failure_classification"] is None
                else str(row["block_failure_classification"])
            ),
            "next_eligible_at_utc": (
                None if row["next_eligible_at"] is None else str(row["next_eligible_at"])
            ),
            "adapter_version": (
                None if row["adapter_version"] is None else str(row["adapter_version"])
            ),
            "updated_at_utc": str(row["updated_at"]),
        }


def parse_optional_utc(text: str | None) -> datetime | None:
    """Parse an optional stored UTC timestamp."""
    if text is None:
        return None
    return parse_utc_timestamp(text)


# Re-export for callers that need a clock without importing codec directly.
__all__ = ["BookmakerRepository", "parse_optional_utc", "utc_now"]
