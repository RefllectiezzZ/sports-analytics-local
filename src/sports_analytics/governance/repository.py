"""Transactional SQLite model registry, promotion, and rollback operations."""

from __future__ import annotations

import sqlite3
from datetime import datetime

from sports_analytics.artifacts import AnalyticalArtifact
from sports_analytics.core.exceptions import (
    DatabaseIntegrityError,
    GovernanceError,
    RepositoryError,
)
from sports_analytics.data.codec import (
    dumps_canonical_json,
    ensure_json_value,
    format_utc_timestamp,
    loads_canonical_json,
    parse_utc_timestamp,
)
from sports_analytics.data.database import require_active_transaction
from sports_analytics.data.types import JsonValue, validate_identifier
from sports_analytics.governance.contracts import (
    GovernanceDecision,
    GovernanceDecisionKind,
    ModelLifecycleStatus,
    ModelRegistryEntry,
    ModelRole,
)
from sports_analytics.models.artifacts import ModelArtifact
from sports_analytics.models.football_scores import (
    FOOTBALL_SCORE_ARTIFACT_SCHEMA,
    FOOTBALL_SCORE_ARTIFACT_TYPE,
    FOOTBALL_SCORE_MODEL_VERSION,
    FootballScoreModel,
)
from sports_analytics.models.identity import content_addressed_id
from sports_analytics.sports.contracts import require_utc


class ModelGovernanceRepository:
    """Repository methods never commit; callers own one explicit transaction."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def register_verified_model(
        self,
        *,
        artifact: ModelArtifact,
        relative_path: str,
        registered_at: datetime,
        actor: str,
        role: ModelRole = ModelRole.CHALLENGER,
        provenance: JsonValue,
    ) -> ModelRegistryEntry:
        require_active_transaction(
            self._connection,
            operation="ModelGovernanceRepository.register_verified_model",
        )
        if not isinstance(artifact, ModelArtifact):
            raise GovernanceError("model registration requires a verified ModelArtifact")
        model_id = str(artifact.document.get("artifact_id", ""))
        if not model_id:
            raise GovernanceError("verified model artifact has no content identity")
        return self._register_verified_identity(
            model_artifact_id=model_id,
            model_checksum_sha256=artifact.checksum_sha256,
            model_relative_path=relative_path,
            model_specification_version=artifact.specification.model_specification_version,
            feature_specification_version=artifact.specification.feature_specification_version,
            sport_code=artifact.specification.sport_code,
            market_key=artifact.specification.market_key,
            registered_at=registered_at,
            actor=actor,
            role=role,
            provenance=provenance,
        )

    def register_verified_score_model(
        self,
        *,
        artifact: AnalyticalArtifact,
        model: FootballScoreModel,
        relative_path: str,
        market_key: str,
        registered_at: datetime,
        actor: str,
        role: ModelRole = ModelRole.CHALLENGER,
        provenance: JsonValue,
    ) -> ModelRegistryEntry:
        """Register a strictly reloaded football score-model artifact."""
        if (
            not isinstance(artifact, AnalyticalArtifact)
            or artifact.artifact_type != FOOTBALL_SCORE_ARTIFACT_TYPE
            or artifact.schema_version != FOOTBALL_SCORE_ARTIFACT_SCHEMA
            or not isinstance(model, FootballScoreModel)
        ):
            raise GovernanceError(
                "score model registration requires a verified score-model artifact"
            )
        if not model.diagnostics.converged:
            raise GovernanceError("unconverged score models cannot enter governance")
        return self._register_verified_identity(
            model_artifact_id=artifact.artifact_id,
            model_checksum_sha256=artifact.checksum_sha256,
            model_relative_path=relative_path,
            model_specification_version=FOOTBALL_SCORE_MODEL_VERSION,
            feature_specification_version="score-history-v1",
            sport_code="football",
            market_key=market_key,
            registered_at=registered_at,
            actor=actor,
            role=role,
            provenance=provenance,
        )

    def _register_verified_identity(
        self,
        *,
        model_artifact_id: str,
        model_checksum_sha256: str,
        model_relative_path: str,
        model_specification_version: str,
        feature_specification_version: str,
        sport_code: str,
        market_key: str,
        registered_at: datetime,
        actor: str,
        role: ModelRole,
        provenance: JsonValue,
    ) -> ModelRegistryEntry:
        require_active_transaction(
            self._connection,
            operation="ModelGovernanceRepository.register_verified_model",
        )
        if role is ModelRole.ARCHIVED:
            raise GovernanceError("new verified models cannot be registered as archived")
        model_id = model_artifact_id
        existing = self.get_model(model_id)
        if existing is not None:
            if (
                existing.model_checksum_sha256 != model_checksum_sha256
                or existing.model_relative_path != model_relative_path
            ):
                raise GovernanceError("registered model identity cannot change checksum or path")
            return existing
        registered = require_utc(registered_at, field_name="registered_at")
        lifecycle = (
            ModelLifecycleStatus.PROMOTED
            if role is ModelRole.CHAMPION
            else ModelLifecycleStatus.ELIGIBLE
        )
        try:
            self._connection.execute(
                """
                INSERT INTO model_registry_entries (
                    model_artifact_id, model_checksum_sha256, model_relative_path,
                    model_specification_version, feature_specification_version,
                    sport_code, market_key, role, lifecycle_status, registered_at,
                    actor, provenance_json, superseded_model_artifact_id, version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 1)
                """,
                (
                    model_id,
                    model_checksum_sha256,
                    model_relative_path.replace("\\", "/"),
                    model_specification_version,
                    feature_specification_version,
                    sport_code,
                    market_key,
                    role.value,
                    lifecycle.value,
                    format_utc_timestamp(registered),
                    validate_identifier(actor, field_name="actor"),
                    dumps_canonical_json(ensure_json_value(provenance)),
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise DatabaseIntegrityError(
                "model registration violates identity or one-champion scope"
            ) from exc
        return self._require_model(model_id)

    def get_model(self, model_artifact_id: str) -> ModelRegistryEntry | None:
        try:
            row = self._connection.execute(
                """
                SELECT * FROM model_registry_entries
                WHERE model_artifact_id = ?
                """,
                (model_artifact_id,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise RepositoryError("failed to read model registry entry") from exc
        return None if row is None else self._entry(row)

    def list_models(self) -> tuple[ModelRegistryEntry, ...]:
        try:
            rows = self._connection.execute(
                """
                SELECT * FROM model_registry_entries
                ORDER BY sport_code, market_key, role, model_artifact_id
                """
            ).fetchall()
        except sqlite3.Error as exc:
            raise RepositoryError("failed to list model registry entries") from exc
        return tuple(self._entry(row) for row in rows)

    def record_decision(
        self,
        *,
        decision: GovernanceDecision,
        actor: str,
        created_at: datetime,
    ) -> GovernanceDecision:
        require_active_transaction(
            self._connection,
            operation="ModelGovernanceRepository.record_decision",
        )
        existing = self._decision_row(decision.decision_id)
        if existing is not None:
            if str(existing["decision_json"]) != dumps_canonical_json(decision.to_json()):
                raise GovernanceError("promotion decision identity conflicts with stored evidence")
            return decision
        evidence_fingerprint = content_addressed_id(
            identity_type="governance-evidence-pair-v1",
            payload={
                "champion": decision.champion_evidence.to_json(),
                "challenger": decision.challenger_evidence.to_json(),
            },
        )
        try:
            self._connection.execute(
                """
                INSERT INTO promotion_decisions (
                    id, schema_version, policy_id, policy_version,
                    champion_model_artifact_id, challenger_model_artifact_id,
                    champion_version, challenger_version, evidence_fingerprint,
                    decision, as_of_utc, decision_json, actor, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision.decision_id,
                    decision.schema_version,
                    decision.policy_id,
                    decision.policy_version,
                    decision.champion_model_artifact_id,
                    decision.challenger_model_artifact_id,
                    decision.champion_registry_version,
                    decision.challenger_registry_version,
                    evidence_fingerprint,
                    decision.decision.value,
                    format_utc_timestamp(decision.as_of_utc),
                    dumps_canonical_json(decision.to_json()),
                    validate_identifier(actor, field_name="actor"),
                    format_utc_timestamp(require_utc(created_at, field_name="created_at")),
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise DatabaseIntegrityError("promotion decision conflicts with stored state") from exc
        return decision

    def apply_promotion(
        self,
        *,
        decision_id: str,
        actor: str,
        occurred_at: datetime,
    ) -> str:
        require_active_transaction(
            self._connection,
            operation="ModelGovernanceRepository.apply_promotion",
        )
        decision = self._decision_row(decision_id)
        if decision is None:
            raise GovernanceError("promotion decision is not registered")
        if str(decision["decision"]) != GovernanceDecisionKind.PROMOTE.value:
            raise GovernanceError("only a verified promote decision can be applied")
        applied = self._connection.execute(
            """
            SELECT id FROM model_role_transitions
            WHERE transition_type = 'promotion' AND decision_id = ?
            """,
            (decision_id,),
        ).fetchone()
        if applied is not None:
            return str(applied["id"])
        champion = self._require_model(str(decision["champion_model_artifact_id"]))
        challenger = self._require_model(str(decision["challenger_model_artifact_id"]))
        if (
            champion.version != int(decision["champion_version"])
            or challenger.version != int(decision["challenger_version"])
            or champion.role is not ModelRole.CHAMPION
            or challenger.role is not ModelRole.CHALLENGER
            or champion.scope != challenger.scope
        ):
            raise GovernanceError("promotion decision is stale relative to current registry state")
        timestamp = require_utc(occurred_at, field_name="occurred_at")
        transition_id = content_addressed_id(
            identity_type="model-role-transition-v1",
            payload={
                "transition_type": "promotion",
                "decision_id": decision_id,
                "previous_champion": champion.model_artifact_id,
                "new_champion": challenger.model_artifact_id,
                "occurred_at": format_utc_timestamp(timestamp),
            },
        )
        try:
            self._connection.execute(
                """
                UPDATE model_registry_entries
                SET role = 'challenger', lifecycle_status = 'demoted',
                    superseded_model_artifact_id = ?, version = version + 1
                WHERE model_artifact_id = ? AND version = ?
                """,
                (challenger.model_artifact_id, champion.model_artifact_id, champion.version),
            )
            self._connection.execute(
                """
                UPDATE model_registry_entries
                SET role = 'champion', lifecycle_status = 'promoted',
                    superseded_model_artifact_id = ?, version = version + 1
                WHERE model_artifact_id = ? AND version = ?
                """,
                (champion.model_artifact_id, challenger.model_artifact_id, challenger.version),
            )
            self._connection.execute(
                """
                INSERT INTO model_role_transitions (
                    id, transition_type, decision_id, scope_sport_code,
                    scope_market_key, previous_champion_model_artifact_id,
                    new_champion_model_artifact_id, rollback_of_transition_id,
                    actor, occurred_at, details_json
                ) VALUES (?, 'promotion', ?, ?, ?, ?, ?, NULL, ?, ?, ?)
                """,
                (
                    transition_id,
                    decision_id,
                    champion.sport_code,
                    champion.market_key,
                    champion.model_artifact_id,
                    challenger.model_artifact_id,
                    validate_identifier(actor, field_name="actor"),
                    format_utc_timestamp(timestamp),
                    dumps_canonical_json({"decision_id": decision_id}),
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise DatabaseIntegrityError("promotion could not be applied atomically") from exc
        return transition_id

    def apply_initial_promotion(
        self,
        *,
        decision_id: str,
        actor: str,
        occurred_at: datetime,
    ) -> str:
        """Apply the first approved promotion through the normal audited transition."""
        require_active_transaction(
            self._connection,
            operation="ModelGovernanceRepository.apply_initial_promotion",
        )
        applied = self._connection.execute(
            """
            SELECT id FROM model_role_transitions
            WHERE transition_type = 'promotion' AND decision_id = ?
            """,
            (decision_id,),
        ).fetchone()
        if applied is not None:
            return str(applied["id"])
        decision = self._decision_row(decision_id)
        if decision is None:
            raise GovernanceError("promotion decision is not registered")
        if str(decision["decision"]) != GovernanceDecisionKind.PROMOTE.value:
            raise GovernanceError("only a verified promote decision can be applied")
        incumbent = self._require_model(str(decision["champion_model_artifact_id"]))
        challenger = self._require_model(str(decision["challenger_model_artifact_id"]))
        if (
            incumbent.version != int(decision["champion_version"])
            or challenger.version != int(decision["challenger_version"])
            or incumbent.role is not ModelRole.CHALLENGER
            or challenger.role is not ModelRole.CHALLENGER
            or incumbent.lifecycle_status is not ModelLifecycleStatus.ELIGIBLE
            or challenger.lifecycle_status is not ModelLifecycleStatus.ELIGIBLE
            or incumbent.scope != challenger.scope
        ):
            raise GovernanceError("initial promotion decision is stale relative to registry state")
        active = self._connection.execute(
            """
            SELECT model_artifact_id FROM model_registry_entries
            WHERE sport_code = ? AND market_key = ?
              AND role = 'champion'
              AND lifecycle_status NOT IN ('archived', 'rejected')
            """,
            incumbent.scope,
        ).fetchone()
        if active is not None:
            raise GovernanceError("initial promotion requires an empty champion scope")
        staged = self._connection.execute(
            """
            UPDATE model_registry_entries
            SET role = 'champion', lifecycle_status = 'promoted'
            WHERE model_artifact_id = ? AND version = ?
              AND role = 'challenger' AND lifecycle_status = 'eligible'
            """,
            (incumbent.model_artifact_id, incumbent.version),
        )
        if staged.rowcount != 1:
            raise GovernanceError("initial incumbent could not be staged atomically")
        return self.apply_promotion(
            decision_id=decision_id,
            actor=actor,
            occurred_at=occurred_at,
        )

    def rollback_transition(
        self,
        *,
        transition_id: str,
        actor: str,
        occurred_at: datetime,
    ) -> str:
        require_active_transaction(
            self._connection,
            operation="ModelGovernanceRepository.rollback_transition",
        )
        row = self._connection.execute(
            "SELECT * FROM model_role_transitions WHERE id = ?",
            (transition_id,),
        ).fetchone()
        if row is None or str(row["transition_type"]) != "promotion":
            raise GovernanceError("rollback requires an existing audited promotion")
        replay = self._connection.execute(
            "SELECT id FROM model_role_transitions WHERE rollback_of_transition_id = ?",
            (transition_id,),
        ).fetchone()
        if replay is not None:
            return str(replay["id"])
        previous = self._require_model(str(row["previous_champion_model_artifact_id"]))
        current = self._require_model(str(row["new_champion_model_artifact_id"]))
        if previous.role is not ModelRole.CHALLENGER or current.role is not ModelRole.CHAMPION:
            raise GovernanceError("promotion transition is stale and cannot be rolled back")
        timestamp = require_utc(occurred_at, field_name="occurred_at")
        rollback_id = content_addressed_id(
            identity_type="model-role-transition-v1",
            payload={
                "transition_type": "rollback",
                "rollback_of_transition_id": transition_id,
                "previous_champion": current.model_artifact_id,
                "new_champion": previous.model_artifact_id,
                "occurred_at": format_utc_timestamp(timestamp),
            },
        )
        self._connection.execute(
            """
            UPDATE model_registry_entries
            SET role = 'challenger', lifecycle_status = 'demoted',
                superseded_model_artifact_id = ?, version = version + 1
            WHERE model_artifact_id = ?
            """,
            (previous.model_artifact_id, current.model_artifact_id),
        )
        self._connection.execute(
            """
            UPDATE model_registry_entries
            SET role = 'champion', lifecycle_status = 'promoted',
                superseded_model_artifact_id = ?, version = version + 1
            WHERE model_artifact_id = ?
            """,
            (current.model_artifact_id, previous.model_artifact_id),
        )
        self._connection.execute(
            """
            INSERT INTO model_role_transitions (
                id, transition_type, decision_id, scope_sport_code,
                scope_market_key, previous_champion_model_artifact_id,
                new_champion_model_artifact_id, rollback_of_transition_id,
                actor, occurred_at, details_json
            ) VALUES (?, 'rollback', NULL, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                rollback_id,
                previous.sport_code,
                previous.market_key,
                current.model_artifact_id,
                previous.model_artifact_id,
                transition_id,
                validate_identifier(actor, field_name="actor"),
                format_utc_timestamp(timestamp),
                dumps_canonical_json({"rollback_of_transition_id": transition_id}),
            ),
        )
        return rollback_id

    def list_history(self) -> tuple[dict[str, JsonValue], ...]:
        rows = self._connection.execute(
            """
            SELECT * FROM model_role_transitions
            ORDER BY occurred_at, id
            """
        ).fetchall()
        return tuple(
            {
                "transition_id": str(row["id"]),
                "transition_type": str(row["transition_type"]),
                "decision_id": None if row["decision_id"] is None else str(row["decision_id"]),
                "sport_code": str(row["scope_sport_code"]),
                "market_key": str(row["scope_market_key"]),
                "previous_champion_model_artifact_id": str(
                    row["previous_champion_model_artifact_id"]
                ),
                "new_champion_model_artifact_id": str(row["new_champion_model_artifact_id"]),
                "rollback_of_transition_id": (
                    None
                    if row["rollback_of_transition_id"] is None
                    else str(row["rollback_of_transition_id"])
                ),
                "actor": str(row["actor"]),
                "occurred_at": str(row["occurred_at"]),
                "details": loads_canonical_json(str(row["details_json"])),
            }
            for row in rows
        )

    def _require_model(self, model_id: str) -> ModelRegistryEntry:
        model = self.get_model(model_id)
        if model is None:
            raise GovernanceError("model is absent from registry")
        return model

    def _decision_row(self, decision_id: str) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self._connection.execute(
            "SELECT * FROM promotion_decisions WHERE id = ?",
            (decision_id,),
        ).fetchone()
        return row

    @staticmethod
    def _entry(row: sqlite3.Row) -> ModelRegistryEntry:
        return ModelRegistryEntry(
            model_artifact_id=str(row["model_artifact_id"]),
            model_checksum_sha256=str(row["model_checksum_sha256"]),
            model_relative_path=str(row["model_relative_path"]),
            model_specification_version=str(row["model_specification_version"]),
            feature_specification_version=str(row["feature_specification_version"]),
            sport_code=str(row["sport_code"]),
            market_key=str(row["market_key"]),
            registered_at=parse_utc_timestamp(str(row["registered_at"])),
            role=ModelRole(str(row["role"])),
            lifecycle_status=ModelLifecycleStatus(str(row["lifecycle_status"])),
            actor=str(row["actor"]),
            provenance=loads_canonical_json(str(row["provenance_json"])),
            superseded_model_artifact_id=(
                None
                if row["superseded_model_artifact_id"] is None
                else str(row["superseded_model_artifact_id"])
            ),
            version=int(row["version"]),
        )
