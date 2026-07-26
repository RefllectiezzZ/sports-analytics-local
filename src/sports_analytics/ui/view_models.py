"""Deterministic view models for already verified analytical artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from sports_analytics.artifacts import TypedAnalyticalArtifact
from sports_analytics.data.types import JsonValue


@dataclass(frozen=True, slots=True)
class DataModeWarning:
    """Human-readable warning selected only from persisted provenance fields."""

    code: str
    title: str
    message: str


@dataclass(frozen=True, slots=True)
class ArtifactSummary:
    """Dashboard figures projected from persisted typed datasets."""

    artifact_id: str
    artifact_kind: str
    schema_version: str
    checksum_sha256: str
    relative_directory: str
    dataset_counts: tuple[tuple[str, int], ...]
    opportunity_count: int
    eligible_count: int
    rejected_count: int
    combination_count: int
    settlement_count: int
    provenances: tuple[str, ...]
    model_artifact_ids: tuple[str, ...]
    feature_artifact_ids: tuple[str, ...]
    filter_config_ids: tuple[str, ...]
    warning: DataModeWarning


@dataclass(frozen=True, slots=True)
class OpportunityDisplayRow:
    """One opportunity joined to its persisted decision and rejection audit."""

    opportunity_id: str
    evaluation_id: str
    selection_id: str
    canonical_event_id: str
    event_start_utc: str
    sport: str
    market_family: str
    market_key: str
    market_period: str
    participant_scope: str
    canonical_participant_id: str
    outcome: str
    source: str
    provider_type: str
    provider_id: str
    decimal_odds: float
    model_probability: float
    raw_implied_probability: float
    complete_market_raw_total: float
    normalized_implied_probability: float
    overround: float
    edge: float
    expected_value: float
    prediction_quality: str
    evaluation_mode: str
    decision_status: str
    accepted_rank: int | None
    rejection_codes: tuple[str, ...]
    prediction_id: str
    provenance: str
    quote_series_id: str
    quote_observation_id: str
    quoted_at_utc: str | None
    source_observed_at_utc: str
    decision_as_of_utc: str
    model_artifact_id: str
    model_checksum_sha256: str
    model_specification_version: str
    feature_artifact_id: str
    feature_manifest_checksum_sha256: str
    feature_specification_version: str
    feature_row_id: str
    dependency_keys: tuple[str, ...]
    participant_ids: tuple[str, ...]
    dependency_metadata_complete: bool
    dependency_metadata_provenance: str
    selection: dict[str, JsonValue]

    def table_row(self) -> dict[str, object]:
        """Return a stable, compact mapping suitable for a wide table."""
        return {
            "sport": self.sport,
            "canonical_event_id": self.canonical_event_id,
            "event_start_utc": self.event_start_utc,
            "market": self.market_key,
            "outcome": self.outcome,
            "participant": self.canonical_participant_id,
            "source": self.source,
            "provider": self.provider_id,
            "decimal_odds": self.decimal_odds,
            "model_probability": self.model_probability,
            "raw_implied_probability": self.raw_implied_probability,
            "complete_market_raw_total": self.complete_market_raw_total,
            "normalized_implied_probability": self.normalized_implied_probability,
            "overround": self.overround,
            "edge": self.edge,
            "expected_value": self.expected_value,
            "prediction_quality": self.prediction_quality,
            "evaluation_mode": self.evaluation_mode,
            "decision_status": self.decision_status,
            "accepted_rank": self.accepted_rank,
            "rejection_codes": ", ".join(self.rejection_codes),
            "opportunity_id": self.opportunity_id,
        }


@dataclass(frozen=True, slots=True)
class OpportunityFilters:
    """UI search constraints applied without changing persisted decisions."""

    search: str = ""
    sports: tuple[str, ...] = ()
    market_families: tuple[str, ...] = ()
    market_keys: tuple[str, ...] = ()
    market_periods: tuple[str, ...] = ()
    participant_scopes: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()
    providers: tuple[str, ...] = ()
    evaluation_modes: tuple[str, ...] = ()
    event_date_from: date | None = None
    event_date_to: date | None = None
    decision_status: str = "all"
    prediction_quality: str = "all"
    minimum_decimal_odds: float | None = None
    maximum_decimal_odds: float | None = None
    minimum_model_probability: float | None = None
    minimum_edge: float | None = None
    minimum_expected_value: float | None = None


@dataclass(frozen=True, slots=True)
class CombinationDisplayRow:
    """One persisted combination with its verified opportunity legs."""

    combination_id: str
    policy_id: str
    policy_version: str
    opportunity_ids: tuple[str, ...]
    legs: tuple[OpportunityDisplayRow, ...]
    dependencies: tuple[dict[str, JsonValue], ...]
    total_decimal_odds: float
    joint_probability: float
    expected_value: float
    common_decision_time_utc: str
    earliest_event_start_utc: str
    latest_event_start_utc: str
    eligible: bool
    rejection_reasons: tuple[str, ...]

    def table_row(self) -> dict[str, object]:
        return {
            "combination_id": self.combination_id,
            "legs": len(self.opportunity_ids),
            "sports": ", ".join(sorted({leg.sport for leg in self.legs})),
            "events": len({leg.canonical_event_id for leg in self.legs}),
            "total_decimal_odds": self.total_decimal_odds,
            "joint_probability": self.joint_probability,
            "expected_value": self.expected_value,
            "common_decision_time_utc": self.common_decision_time_utc,
            "event_interval_utc": (
                f"{self.earliest_event_start_utc} — {self.latest_event_start_utc}"
            ),
            "eligible": self.eligible,
            "rejection_reasons": ", ".join(self.rejection_reasons),
        }


@dataclass(frozen=True, slots=True)
class BacktestDisplay:
    """Persisted backtest datasets prepared for direct rendering."""

    aggregate_metrics: tuple[dict[str, JsonValue], ...]
    fold_metrics: tuple[dict[str, JsonValue], ...]
    settlements: tuple[dict[str, JsonValue], ...]
    combinations: tuple[CombinationDisplayRow, ...]
    cumulative_profit_units: tuple[float, ...]


def build_artifact_summary(artifact: TypedAnalyticalArtifact) -> ArtifactSummary:
    """Build a deterministic dashboard summary from persisted rows only."""
    datasets = _datasets(artifact)
    decisions = datasets.get("opportunity_decisions", ())
    predictions = datasets.get("predictions", ())
    opportunities = datasets.get("opportunities", ())
    provenances = _string_values(predictions, "provenance")
    evaluation_modes = _string_values(opportunities, "evaluation_mode")
    return ArtifactSummary(
        artifact_id=artifact.artifact_id,
        artifact_kind=artifact.artifact_kind,
        schema_version=artifact.schema_version,
        checksum_sha256=artifact.checksum_sha256,
        relative_directory=artifact.relative_directory,
        dataset_counts=tuple((dataset.name, dataset.row_count) for dataset in artifact.datasets),
        opportunity_count=len(opportunities),
        eligible_count=sum(row.get("eligible") is True for row in decisions),
        rejected_count=sum(row.get("eligible") is False for row in decisions),
        combination_count=len(datasets.get("combinations", ())),
        settlement_count=len(datasets.get("settlements", ())),
        provenances=provenances,
        model_artifact_ids=_string_values_from_nested(predictions, "lineage", "model_artifact_id"),
        feature_artifact_ids=_string_values_from_nested(
            predictions, "lineage", "feature_artifact_id"
        ),
        filter_config_ids=_string_values(decisions, "filter_config_id"),
        warning=select_provenance_warning(
            provenances=provenances,
            evaluation_modes=evaluation_modes,
        ),
    )


def build_opportunity_rows(
    artifact: TypedAnalyticalArtifact,
) -> tuple[OpportunityDisplayRow, ...]:
    """Join opportunities to persisted decisions without recalculating analytics."""
    datasets = _datasets(artifact)
    decision_by_id = {
        _string(row, "opportunity_id"): row for row in datasets.get("opportunity_decisions", ())
    }
    evaluation_by_quote = {
        (
            _string(row, "prediction_id"),
            _string(row, "quote_observation_id"),
            _string(row, "quote_series_id"),
        ): row
        for row in datasets.get("market_evaluations", ())
    }
    prediction_by_id = {
        _string(row, "prediction_id"): row for row in datasets.get("predictions", ())
    }
    rejection_codes_by_id: dict[str, set[str]] = {}
    for row in datasets.get("rejections", ()):
        opportunity_id = row.get("opportunity_id")
        codes = row.get("codes")
        if type(opportunity_id) is not str or not isinstance(codes, list):
            continue
        rejection_codes_by_id.setdefault(opportunity_id, set()).update(str(code) for code in codes)
    rows = [
        _opportunity_display_row(
            row,
            decision_by_id.get(_string(row, "opportunity_id")),
            rejection_codes_by_id,
            evaluation_by_quote.get(
                (
                    _string(row, "prediction_id"),
                    _string(row, "quote_observation_id"),
                    _string(row, "quote_series_id"),
                )
            ),
            prediction_by_id.get(_string(row, "prediction_id")),
        )
        for row in datasets.get("opportunities", ())
    ]
    return tuple(
        sorted(
            rows,
            key=lambda row: (
                row.event_start_utc,
                row.canonical_event_id,
                row.market_key,
                row.outcome,
                row.opportunity_id,
            ),
        )
    )


def filter_opportunities(
    rows: tuple[OpportunityDisplayRow, ...],
    filters: OpportunityFilters,
) -> tuple[OpportunityDisplayRow, ...]:
    """Apply deterministic browsing filters to persisted opportunity rows."""
    search = filters.search.strip().casefold()
    selected = []
    for row in rows:
        if filters.sports and row.sport not in filters.sports:
            continue
        if filters.market_families and row.market_family not in filters.market_families:
            continue
        if filters.market_keys and row.market_key not in filters.market_keys:
            continue
        if filters.market_periods and row.market_period not in filters.market_periods:
            continue
        if filters.participant_scopes and row.participant_scope not in filters.participant_scopes:
            continue
        if filters.sources and row.source not in filters.sources:
            continue
        if filters.providers and row.provider_id not in filters.providers:
            continue
        if filters.evaluation_modes and row.evaluation_mode not in filters.evaluation_modes:
            continue
        if filters.decision_status != "all" and row.decision_status != filters.decision_status:
            continue
        if (
            filters.prediction_quality != "all"
            and row.prediction_quality != filters.prediction_quality
        ):
            continue
        event_date = _parse_timestamp(row.event_start_utc).date()
        if filters.event_date_from is not None and event_date < filters.event_date_from:
            continue
        if filters.event_date_to is not None and event_date > filters.event_date_to:
            continue
        if (
            filters.minimum_decimal_odds is not None
            and row.decimal_odds < filters.minimum_decimal_odds
        ):
            continue
        if (
            filters.maximum_decimal_odds is not None
            and row.decimal_odds > filters.maximum_decimal_odds
        ):
            continue
        if (
            filters.minimum_model_probability is not None
            and row.model_probability < filters.minimum_model_probability
        ):
            continue
        if filters.minimum_edge is not None and row.edge < filters.minimum_edge:
            continue
        if (
            filters.minimum_expected_value is not None
            and row.expected_value < filters.minimum_expected_value
        ):
            continue
        if search and search not in _opportunity_search_text(row):
            continue
        selected.append(row)
    return tuple(selected)


def build_combination_rows(
    artifact: TypedAnalyticalArtifact,
    opportunities: tuple[OpportunityDisplayRow, ...] | None = None,
) -> tuple[CombinationDisplayRow, ...]:
    """Convert persisted combinations and link every leg to an opportunity row."""
    opportunity_rows = opportunities or build_opportunity_rows(artifact)
    by_id = {row.opportunity_id: row for row in opportunity_rows}
    converted: list[CombinationDisplayRow] = []
    for row in _datasets(artifact).get("combinations", ()):
        opportunity_ids = tuple(_string_list(row.get("opportunity_ids")))
        legs = tuple(by_id[item] for item in opportunity_ids if item in by_id)
        dependencies = tuple(
            dict(item) for item in _list(row, "dependencies") if isinstance(item, dict)
        )
        converted.append(
            CombinationDisplayRow(
                combination_id=_string(row, "combination_id"),
                policy_id=_string(row, "policy_id"),
                policy_version=_string(row, "policy_version"),
                opportunity_ids=opportunity_ids,
                legs=legs,
                dependencies=dependencies,
                total_decimal_odds=_float(row, "total_decimal_odds"),
                joint_probability=_float(row, "joint_probability"),
                expected_value=_float(row, "expected_value"),
                common_decision_time_utc=_string(row, "common_decision_time_utc"),
                earliest_event_start_utc=_string(row, "earliest_event_start_utc"),
                latest_event_start_utc=_string(row, "latest_event_start_utc"),
                eligible=row.get("eligible") is True,
                rejection_reasons=tuple(_string_list(row.get("rejection_reasons"))),
            )
        )
    return tuple(sorted(converted, key=lambda item: item.combination_id))


def filter_combinations_by_odds(
    rows: tuple[CombinationDisplayRow, ...],
    *,
    leg_minimum_odds: float,
    leg_maximum_odds: float,
    total_minimum_odds: float,
    total_maximum_odds: float,
) -> tuple[CombinationDisplayRow, ...]:
    """Apply independent leg and total-price bounds to persisted combinations."""
    return tuple(
        row
        for row in rows
        if total_minimum_odds <= row.total_decimal_odds <= total_maximum_odds
        and len(row.legs) == len(row.opportunity_ids)
        and all(leg_minimum_odds <= leg.decimal_odds <= leg_maximum_odds for leg in row.legs)
    )


def build_backtest_display(artifact: TypedAnalyticalArtifact) -> BacktestDisplay:
    """Expose persisted backtest metrics without calculating replacement values."""
    datasets = _datasets(artifact)
    aggregate = tuple(dict(row) for row in datasets.get("aggregate_metrics", ()))
    cumulative: tuple[float, ...] = ()
    if aggregate:
        values = aggregate[0].get("cumulative_profit_units")
        if isinstance(values, list):
            parsed: list[float] = []
            for value in values:
                if isinstance(value, bool) or not isinstance(value, int | float | str):
                    continue
                parsed.append(float(value))
            cumulative = tuple(parsed)
    return BacktestDisplay(
        aggregate_metrics=aggregate,
        fold_metrics=tuple(dict(row) for row in datasets.get("fold_metrics", ())),
        settlements=tuple(dict(row) for row in datasets.get("settlements", ())),
        combinations=build_combination_rows(artifact),
        cumulative_profit_units=cumulative,
    )


def dataset_rows_for_display(
    artifact: TypedAnalyticalArtifact,
    dataset_name: str,
) -> tuple[dict[str, JsonValue], ...]:
    """Return rows only from a dataset in an already verified typed artifact."""
    return tuple(dict(row) for row in artifact.dataset(dataset_name).rows)


def collect_audit_identifiers(
    artifact: TypedAnalyticalArtifact,
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Collect persisted identifiers by logical dataset in stable order."""
    identifiers: list[tuple[str, tuple[str, ...]]] = []
    for dataset in artifact.datasets:
        values = tuple(
            sorted(str(row[dataset.id_field]) for row in dataset.rows if dataset.id_field in row)
        )
        identifiers.append((dataset.name, values))
    return tuple(identifiers)


def select_provenance_warning(
    *,
    provenances: tuple[str, ...],
    evaluation_modes: tuple[str, ...],
) -> DataModeWarning:
    """Select the strongest honest data-mode warning from persisted fields."""
    if any("closing-line-historical" in mode for mode in evaluation_modes):
        return DataModeWarning(
            code="closing-line-historical-benchmark",
            title="Closing-line historical benchmark",
            message=(
                "These prices are persisted historical closing benchmarks, not executable "
                "bookmaker offers and not current opportunities."
            ),
        )
    if "historical-replay" in provenances:
        return DataModeWarning(
            code="historical-replay",
            title="Historical replay",
            message=(
                "This artifact replays persisted historical inputs. It does not represent "
                "current fixtures, current odds, or an executable betting offer."
            ),
        )
    if "synthetic-contract" in provenances:
        return DataModeWarning(
            code="synthetic-contract",
            title="Synthetic contract data",
            message=(
                "This artifact contains synthetic contract data for analytical validation, "
                "not real-world or current betting opportunities."
            ),
        )
    return DataModeWarning(
        code="persisted-analysis",
        title="Persisted analytical artifact",
        message=(
            "The interface displays previously persisted analytical results. Verify the "
            "artifact provenance before interpreting any row."
        ),
    )


def unique_values(
    rows: tuple[OpportunityDisplayRow, ...],
    field: str,
) -> tuple[str, ...]:
    """Return sorted non-empty values for one opportunity display field."""
    values = {str(getattr(row, field)) for row in rows if str(getattr(row, field))}
    return tuple(sorted(values))


def short_identifier(value: str, *, edge: int = 8) -> str:
    """Shorten a long identifier visually while preserving both ends."""
    if len(value) <= edge * 2 + 1:
        return value
    return f"{value[:edge]}…{value[-edge:]}"


def format_percent(value: float) -> str:
    """Format one persisted decimal ratio consistently."""
    return f"{value:.2%}"


def format_odds(value: float) -> str:
    """Format persisted decimal odds consistently."""
    return f"{value:.2f}"


def _datasets(
    artifact: TypedAnalyticalArtifact,
) -> dict[str, tuple[dict[str, JsonValue], ...]]:
    return {dataset.name: dataset.rows for dataset in artifact.datasets}


def _opportunity_display_row(
    row: dict[str, JsonValue],
    decision: dict[str, JsonValue] | None,
    rejection_codes_by_id: dict[str, set[str]],
    evaluation: dict[str, JsonValue] | None,
    prediction: dict[str, JsonValue] | None,
) -> OpportunityDisplayRow:
    selection_value = row.get("selection")
    selection = dict(selection_value) if isinstance(selection_value, dict) else {}
    opportunity_id = _string(row, "opportunity_id")
    decision_codes = (
        tuple(_string_list(decision.get("rejection_codes"))) if decision is not None else ()
    )
    persisted_codes = rejection_codes_by_id.get(opportunity_id, set())
    codes = tuple(sorted(set(decision_codes) | persisted_codes))
    eligible = decision is not None and decision.get("eligible") is True
    accepted_rank = decision.get("accepted_rank") if decision is not None else None
    evaluation_row = evaluation or {}
    prediction_row = prediction or {}
    return OpportunityDisplayRow(
        opportunity_id=opportunity_id,
        evaluation_id=_string(evaluation_row, "evaluation_id"),
        selection_id=_string(evaluation_row, "selection_id"),
        canonical_event_id=_string(row, "canonical_event_id"),
        event_start_utc=_string(row, "event_start_utc"),
        sport=_selection_string(selection, "sport_code"),
        market_family=_selection_string(selection, "market_family"),
        market_key=_selection_string(selection, "market_key"),
        market_period=_selection_string(selection, "market_period"),
        participant_scope=_selection_string(selection, "participant_scope"),
        canonical_participant_id=_selection_string(
            selection, "canonical_participant_id", empty="—"
        ),
        outcome=_selection_string(selection, "outcome_key"),
        source=_string(row, "source_name"),
        provider_type=_string(row, "provider_type"),
        provider_id=_string(row, "provider_id"),
        decimal_odds=_float(row, "decimal_odds"),
        model_probability=_float(row, "model_probability"),
        raw_implied_probability=_float(row, "raw_implied_probability"),
        complete_market_raw_total=_float(evaluation_row, "complete_market_raw_total"),
        normalized_implied_probability=_float(row, "normalized_implied_probability"),
        overround=_float(row, "overround"),
        edge=_float(row, "edge"),
        expected_value=_float(row, "expected_value"),
        prediction_quality=("passed" if row.get("prediction_quality_passed") is True else "failed"),
        evaluation_mode=_string(row, "evaluation_mode"),
        decision_status="eligible" if eligible else "rejected",
        accepted_rank=accepted_rank if type(accepted_rank) is int else None,
        rejection_codes=codes,
        prediction_id=_string(row, "prediction_id"),
        provenance=_string(prediction_row, "provenance"),
        quote_series_id=_string(row, "quote_series_id"),
        quote_observation_id=_string(row, "quote_observation_id"),
        quoted_at_utc=_optional_string(row.get("quoted_at_utc")),
        source_observed_at_utc=_string(row, "source_observed_at_utc"),
        decision_as_of_utc=_string(row, "decision_as_of_utc"),
        model_artifact_id=_string(row, "model_artifact_id"),
        model_checksum_sha256=_string(row, "model_checksum_sha256"),
        model_specification_version=_string(row, "model_specification_version"),
        feature_artifact_id=_string(row, "feature_artifact_id"),
        feature_manifest_checksum_sha256=_string(row, "feature_manifest_checksum_sha256"),
        feature_specification_version=_string(row, "feature_specification_version"),
        feature_row_id=_string(row, "feature_row_id"),
        dependency_keys=tuple(_string_list(row.get("dependency_keys"))),
        participant_ids=tuple(_string_list(row.get("participant_ids"))),
        dependency_metadata_complete=row.get("dependency_metadata_complete") is True,
        dependency_metadata_provenance=_optional_string(row.get("dependency_metadata_provenance"))
        or "",
        selection=selection,
    )


def _opportunity_search_text(row: OpportunityDisplayRow) -> str:
    values: tuple[object, ...] = (
        row.opportunity_id,
        row.canonical_event_id,
        row.sport,
        row.market_family,
        row.market_key,
        row.market_period,
        row.participant_scope,
        row.canonical_participant_id,
        row.outcome,
        row.source,
        row.provider_type,
        row.provider_id,
        row.evaluation_mode,
        row.decision_status,
        *row.rejection_codes,
    )
    return " ".join(str(value) for value in values).casefold()


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _string(row: dict[str, JsonValue], field: str) -> str:
    value = row.get(field)
    return value if type(value) is str else ""


def _optional_string(value: JsonValue | None) -> str | None:
    return value if type(value) is str else None


def _float(row: dict[str, JsonValue], field: str) -> float:
    value = row.get(field)
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return 0.0
    return float(value)


def _list(row: dict[str, JsonValue], field: str) -> list[JsonValue]:
    value = row.get(field)
    return value if isinstance(value, list) else []


def _string_list(value: JsonValue | None) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if type(item) is str]


def _selection_string(
    selection: dict[str, JsonValue],
    field: str,
    *,
    empty: str = "",
) -> str:
    value = selection.get(field)
    return value if type(value) is str and value else empty


def _string_values(
    rows: tuple[dict[str, JsonValue], ...],
    field: str,
) -> tuple[str, ...]:
    return tuple(sorted({value for row in rows if type(value := row.get(field)) is str and value}))


def _string_values_from_nested(
    rows: tuple[dict[str, JsonValue], ...],
    parent: str,
    field: str,
) -> tuple[str, ...]:
    values: set[str] = set()
    for row in rows:
        nested = row.get(parent)
        if not isinstance(nested, dict):
            continue
        value = nested.get(field)
        if type(value) is str and value:
            values.add(value)
    return tuple(sorted(values))
