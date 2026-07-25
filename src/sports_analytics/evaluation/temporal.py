"""Deterministic rolling-origin temporal validation (no shuffled splits)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol

from sports_analytics.core.exceptions import EvaluationError
from sports_analytics.features.contracts import OutcomeSpace


class TemporalExample(Protocol):
    """Minimal example contract required for chronological fold construction."""

    @property
    def example_id(self) -> str: ...

    @property
    def event_date(self) -> date: ...

    @property
    def target_label(self) -> str: ...


@dataclass(frozen=True, slots=True)
class TemporalSplitConfig:
    """Configurable minimum region sizes for rolling-origin folds."""

    min_train_rows: int = 60
    min_calibration_rows: int = 20
    min_test_rows: int = 20
    step_rows: int = 20
    maximum_folds: int = 8

    def __post_init__(self) -> None:
        for name in (
            "min_train_rows",
            "min_calibration_rows",
            "min_test_rows",
            "step_rows",
            "maximum_folds",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or value < 1:
                msg = f"{name} must be a positive integer"
                raise EvaluationError(msg)

    def to_json(self) -> dict[str, int]:
        """Return a canonical JSON-compatible representation."""
        return {
            "min_train_rows": self.min_train_rows,
            "min_calibration_rows": self.min_calibration_rows,
            "min_test_rows": self.min_test_rows,
            "step_rows": self.step_rows,
            "maximum_folds": self.maximum_folds,
        }


@dataclass(frozen=True, slots=True)
class FoldRegion:
    """One chronological region inside a fold."""

    name: str
    start_date: date
    end_date: date
    event_ids: tuple[str, ...]
    class_counts: dict[str, int]


@dataclass(frozen=True, slots=True)
class TemporalFold:
    """One rolling-origin fold with train/calibration/test regions."""

    fold_id: str
    train: FoldRegion
    calibration: FoldRegion
    test: FoldRegion


def build_rolling_origin_folds(
    examples: tuple[TemporalExample, ...],
    *,
    outcome_space: OutcomeSpace,
    config: TemporalSplitConfig | None = None,
) -> tuple[TemporalFold, ...]:
    """Build date-aligned rolling-origin folds.

    When more valid folds exist than ``maximum_folds``, the most recent folds
    are retained. Returned folds remain ordered chronologically and
    ``folds[-1]`` is always the most recent valid fold.
    """
    split = config or TemporalSplitConfig()
    ordered_labels = outcome_space.ordered_labels
    if not examples:
        msg = "cannot build folds from an empty feature dataset"
        raise EvaluationError(msg)

    ordered = tuple(
        sorted(
            examples,
            key=lambda item: (item.event_date.isoformat(), item.example_id),
        )
    )
    dates = [item.event_date for item in ordered]
    for index in range(1, len(dates)):
        if dates[index] < dates[index - 1]:
            msg = "feature rows violate chronological ordering"
            raise EvaluationError(msg)

    unique_dates = tuple(sorted({item.event_date for item in ordered}))
    date_to_indices: dict[date, list[int]] = {item: [] for item in unique_dates}
    for index, item in enumerate(ordered):
        date_to_indices[item.event_date].append(index)

    min_total = split.min_train_rows + split.min_calibration_rows + split.min_test_rows
    if len(ordered) < min_total:
        msg = (
            "insufficient chronological training history for temporal folds: "
            f"need at least {min_total} rows, found {len(ordered)}"
        )
        raise EvaluationError(msg)

    earliest_test_end = 0
    cumulative = 0
    for date_index, current_date in enumerate(unique_dates):
        cumulative += len(date_to_indices[current_date])
        if cumulative >= min_total:
            earliest_test_end = date_index
            break
    else:
        msg = "unable to locate a valid chronological fold end"
        raise EvaluationError(msg)

    candidate_folds: list[TemporalFold] = []
    date_index = earliest_test_end
    while date_index < len(unique_dates):
        fold = _try_build_fold(
            ordered=ordered,
            unique_dates=unique_dates,
            date_to_indices=date_to_indices,
            test_end_date_index=date_index,
            config=split,
            fold_number=len(candidate_folds) + 1,
            ordered_labels=ordered_labels,
        )
        if fold is not None:
            candidate_folds.append(fold)
            advanced_rows = 0
            next_index = date_index + 1
            while next_index < len(unique_dates) and advanced_rows < split.step_rows:
                advanced_rows += len(date_to_indices[unique_dates[next_index]])
                next_index += 1
            date_index = max(next_index, date_index + 1)
        else:
            date_index += 1

    if not candidate_folds:
        msg = (
            "no valid rolling-origin folds could be constructed; "
            "check minimum region sizes and class coverage"
        )
        raise EvaluationError(msg)

    retained = candidate_folds[-split.maximum_folds :]
    return tuple(
        TemporalFold(
            fold_id=f"fold-{index:03d}",
            train=fold.train,
            calibration=fold.calibration,
            test=fold.test,
        )
        for index, fold in enumerate(retained, start=1)
    )


def assign_fold_rows(
    examples: tuple[TemporalExample, ...],
    folds: tuple[TemporalFold, ...],
) -> list[dict[str, object]]:
    """Create folds.parquet rows with exact event dates."""
    by_id = {item.example_id: item for item in examples}
    rows: list[dict[str, object]] = []
    for fold in folds:
        for region in (fold.train, fold.calibration, fold.test):
            for event_id in region.event_ids:
                item = by_id[event_id]
                rows.append(
                    {
                        "fold_id": fold.fold_id,
                        "region": region.name,
                        "canonical_event_id": event_id,
                        "event_date": item.event_date,
                    }
                )
    rows.sort(
        key=lambda row: (
            str(row["fold_id"]),
            str(row["region"]),
            (
                row["event_date"].isoformat()
                if isinstance(row["event_date"], date)
                else str(row["event_date"])
            ),
            str(row["canonical_event_id"]),
        )
    )
    return rows


def fold_summaries(folds: tuple[TemporalFold, ...]) -> list[dict[str, object]]:
    """Return canonical fold summaries for manifest publication."""
    summaries: list[dict[str, object]] = []
    for fold in folds:
        summaries.append(
            {
                "fold_id": fold.fold_id,
                "train": {
                    "start_date": fold.train.start_date.isoformat(),
                    "end_date": fold.train.end_date.isoformat(),
                    "event_count": len(fold.train.event_ids),
                    "class_counts": dict(fold.train.class_counts),
                },
                "calibration": {
                    "start_date": fold.calibration.start_date.isoformat(),
                    "end_date": fold.calibration.end_date.isoformat(),
                    "event_count": len(fold.calibration.event_ids),
                    "class_counts": dict(fold.calibration.class_counts),
                },
                "test": {
                    "start_date": fold.test.start_date.isoformat(),
                    "end_date": fold.test.end_date.isoformat(),
                    "event_count": len(fold.test.event_ids),
                    "class_counts": dict(fold.test.class_counts),
                },
            }
        )
    return summaries


def _try_build_fold(
    *,
    ordered: tuple[TemporalExample, ...],
    unique_dates: tuple[date, ...],
    date_to_indices: dict[date, list[int]],
    test_end_date_index: int,
    config: TemporalSplitConfig,
    fold_number: int,
    ordered_labels: tuple[str, ...],
) -> TemporalFold | None:
    test_indices, test_date_start = _grow_region_backward(
        unique_dates=unique_dates,
        date_to_indices=date_to_indices,
        end_date_index=test_end_date_index,
        minimum_rows=config.min_test_rows,
    )
    if test_indices is None or test_date_start is None:
        return None
    calibration_end = test_date_start - 1
    if calibration_end < 0:
        return None
    calibration_indices, calibration_date_start = _grow_region_backward(
        unique_dates=unique_dates,
        date_to_indices=date_to_indices,
        end_date_index=calibration_end,
        minimum_rows=config.min_calibration_rows,
    )
    if calibration_indices is None or calibration_date_start is None:
        return None
    if calibration_date_start == 0:
        return None
    train_indices: list[int] = []
    for date_index in range(0, calibration_date_start):
        train_indices.extend(date_to_indices[unique_dates[date_index]])
    if len(train_indices) < config.min_train_rows:
        return None

    train_region = _region_from_indices("train", ordered, train_indices, ordered_labels)
    calibration_region = _region_from_indices(
        "calibration",
        ordered,
        calibration_indices,
        ordered_labels,
    )
    test_region = _region_from_indices("test", ordered, test_indices, ordered_labels)

    date_sets = (
        {ordered[i].event_date for i in train_indices},
        {ordered[i].event_date for i in calibration_indices},
        {ordered[i].event_date for i in test_indices},
    )
    if date_sets[0] & date_sets[1] or date_sets[0] & date_sets[2] or date_sets[1] & date_sets[2]:
        msg = "fold regions share calendar dates"
        raise EvaluationError(msg)

    if set(train_region.class_counts).intersection(ordered_labels) != set(ordered_labels):
        return None
    if any(train_region.class_counts[label] < 1 for label in ordered_labels):
        return None
    if sum(calibration_region.class_counts.values()) < config.min_calibration_rows:
        return None
    if sum(test_region.class_counts.values()) < config.min_test_rows:
        return None

    if not (
        train_region.end_date < calibration_region.start_date
        and calibration_region.end_date < test_region.start_date
    ):
        msg = "fold regions violate chronological ordering"
        raise EvaluationError(msg)

    return TemporalFold(
        fold_id=f"fold-{fold_number:03d}",
        train=train_region,
        calibration=calibration_region,
        test=test_region,
    )


def _grow_region_backward(
    *,
    unique_dates: tuple[date, ...],
    date_to_indices: dict[date, list[int]],
    end_date_index: int,
    minimum_rows: int,
) -> tuple[list[int] | None, int | None]:
    indices: list[int] = []
    start_index = end_date_index
    while start_index >= 0 and len(indices) < minimum_rows:
        batch = date_to_indices[unique_dates[start_index]]
        indices = batch + indices
        if len(indices) >= minimum_rows:
            return indices, start_index
        start_index -= 1
    return None, None


def _region_from_indices(
    name: str,
    ordered: tuple[TemporalExample, ...],
    indices: list[int],
    ordered_labels: tuple[str, ...],
) -> FoldRegion:
    events = [ordered[index] for index in indices]
    class_counts = {label: 0 for label in ordered_labels}
    for item in events:
        class_counts[item.target_label] += 1
    return FoldRegion(
        name=name,
        start_date=events[0].event_date,
        end_date=events[-1].event_date,
        event_ids=tuple(item.example_id for item in events),
        class_counts=class_counts,
    )
