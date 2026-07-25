"""Sport-agnostic Arrow helpers shared by canonical dataset schemas.

This module intentionally contains no sport, competition, or market semantics so
the snapshot infrastructure stays reusable across sports and sources.
"""

from __future__ import annotations

import hashlib
from typing import Final

import pyarrow as pa

from sports_analytics.data.codec import dumps_canonical_json
from sports_analytics.data.types import JsonValue

PROJECT_NAME: Final[str] = "sports-analytics-local"

# Decimal price policy shared by every market quote dataset:
# precision 10 / scale 4 represents 1.0100 .. 999999.9999 exactly.
PRICE_DECIMAL_PRECISION: Final[int] = 10
PRICE_DECIMAL_SCALE: Final[int] = 4

# Line values (totals, handicaps, spreads) need one decimal for quarter lines.
LINE_DECIMAL_PRECISION: Final[int] = 8
LINE_DECIMAL_SCALE: Final[int] = 2


def dictionary_string() -> pa.DataType:
    """Return the shared low-cardinality string encoding used by canonical datasets."""
    return pa.dictionary(pa.int8(), pa.string())


def price_decimal() -> pa.DataType:
    """Return the shared decimal type used for decimal odds."""
    return pa.decimal128(PRICE_DECIMAL_PRECISION, PRICE_DECIMAL_SCALE)


def line_decimal() -> pa.DataType:
    """Return the shared decimal type used for market line values."""
    return pa.decimal128(LINE_DECIMAL_PRECISION, LINE_DECIMAL_SCALE)


def utc_timestamp() -> pa.DataType:
    """Return the shared microsecond UTC timestamp type."""
    return pa.timestamp("us", tz="UTC")


def dataset_metadata(
    *,
    dataset_name: str,
    schema_version: str,
    domain: str,
    extra: dict[str, str] | None = None,
) -> dict[bytes, bytes]:
    """Build deterministic Arrow schema metadata for a canonical dataset.

    ``domain`` records the analytical domain that owns the dataset (for example a
    sport code or ``markets``) without giving this module sport-specific
    knowledge.
    """
    metadata: dict[str, str] = {
        "dataset": dataset_name,
        "schema_version": schema_version,
        "domain": domain,
        "project_name": PROJECT_NAME,
    }
    if extra:
        for key, value in extra.items():
            if key in metadata:
                msg = f"duplicate Arrow metadata key: {key}"
                raise ValueError(msg)
            metadata[key] = value
    return {key.encode("utf-8"): metadata[key].encode("utf-8") for key in sorted(metadata)}


def schema_fingerprint(schema: pa.Schema) -> str:
    """Return a deterministic SHA-256 fingerprint of a logical Arrow schema.

    The fingerprint covers field name, order, type, and nullability plus schema
    metadata, so any contract change produces a new fingerprint.
    """
    fields: list[JsonValue] = [
        {
            "name": field.name,
            "nullable": field.nullable,
            "type": str(field.type),
        }
        for field in schema
    ]
    metadata = {
        key.decode("utf-8"): value.decode("utf-8") for key, value in (schema.metadata or {}).items()
    }
    payload: dict[str, JsonValue] = {
        "fields": fields,
        "metadata": {key: metadata[key] for key in sorted(metadata)},
    }
    canonical = dumps_canonical_json(payload)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
