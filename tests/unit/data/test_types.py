"""Tests for canonical JSON, timestamps, and typed validators."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from sports_analytics.core.exceptions import RepositoryError
from sports_analytics.data.codec import (
    dumps_canonical_json,
    format_utc_timestamp,
    loads_canonical_json,
    parse_utc_timestamp,
)
from sports_analytics.data.types import (
    normalize_uuid,
    validate_identifier,
    validate_relative_snapshot_path,
    validate_sha256_checksum,
)


def test_canonical_json_sorts_keys_and_preserves_unicode() -> None:
    text = dumps_canonical_json({"b": 1, "a": "café", "n": [3, 2]})
    assert text == '{"a":"café","b":1,"n":[3,2]}'
    assert loads_canonical_json(text) == {"a": "café", "b": 1, "n": [3, 2]}


def test_canonical_json_rejects_nan_and_infinity() -> None:
    with pytest.raises(RepositoryError):
        dumps_canonical_json(float("nan"))
    with pytest.raises(RepositoryError):
        dumps_canonical_json(float("inf"))


def test_canonical_json_rejects_unsupported_objects() -> None:
    with pytest.raises(RepositoryError):
        dumps_canonical_json({"x": object()})  # type: ignore[dict-item]


def test_malformed_json_raises_repository_error() -> None:
    with pytest.raises(RepositoryError, match="malformed"):
        loads_canonical_json("{not-json")


def test_aware_non_utc_normalizes_to_utc() -> None:
    eastern = timezone(timedelta(hours=-5))
    value = datetime(2026, 7, 24, 14, 30, 0, 123456, tzinfo=eastern)
    text = format_utc_timestamp(value)
    assert text == "2026-07-24T19:30:00.123456Z"
    parsed = parse_utc_timestamp(text)
    assert parsed == datetime(2026, 7, 24, 19, 30, 0, 123456, tzinfo=UTC)


def test_naive_datetime_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        format_utc_timestamp(datetime(2026, 7, 24, 19, 30, 0))


def test_timestamp_round_trip_includes_microseconds() -> None:
    value = datetime(2026, 7, 24, 19, 30, 0, tzinfo=UTC)
    text = format_utc_timestamp(value)
    assert text.endswith(".000000Z")
    assert parse_utc_timestamp(text) == value


def test_malformed_timestamp_rejected() -> None:
    with pytest.raises(ValueError):
        parse_utc_timestamp("2026-07-24 19:30:00")


def test_uuid_normalization_and_rejection() -> None:
    assert normalize_uuid("AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA") == (
        "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    )
    with pytest.raises(RepositoryError):
        normalize_uuid("not-a-uuid")


def test_identifier_and_path_validation() -> None:
    assert validate_identifier("job.refresh", field_name="job_type") == "job.refresh"
    with pytest.raises(RepositoryError):
        validate_identifier(" Bad", field_name="actor")
    assert validate_relative_snapshot_path("raw/2026/data.parquet") == "raw/2026/data.parquet"
    with pytest.raises(RepositoryError):
        validate_relative_snapshot_path("/abs/path")
    with pytest.raises(RepositoryError):
        validate_relative_snapshot_path("../escape")
    assert validate_sha256_checksum("a" * 64) == "a" * 64
    with pytest.raises(RepositoryError):
        validate_sha256_checksum("A" * 64)
