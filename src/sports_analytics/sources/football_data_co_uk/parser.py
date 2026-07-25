"""Strict Football-Data.co.uk CSV decoding and structural parsing."""

from __future__ import annotations

import csv
import io
from collections.abc import Iterator
from dataclasses import dataclass

from sports_analytics.core.exceptions import ParserError
from sports_analytics.sources.football_data_co_uk.columns import (
    MAX_FIELD_LENGTH,
    MAX_LINE_LENGTH,
    MAX_ROW_COUNT,
    OPTIONAL_COLUMNS,
    REQUIRED_COLUMNS,
    SUPPORTED_OPTIONAL_AND_ODDS,
)


@dataclass(frozen=True, slots=True)
class ParsedFootballCsv:
    """Structured result of a strict Football-Data CSV parse."""

    encoding: str
    headers: tuple[str, ...]
    recognized_headers: tuple[str, ...]
    unknown_headers: tuple[str, ...]
    missing_optional_headers: tuple[str, ...]
    rows: tuple[dict[str, str], ...]
    row_count: int
    exact_duplicate_count: int
    warnings: tuple[str, ...]


def decode_csv_bytes(content: bytes) -> tuple[str, str]:
    """Decode CSV bytes as UTF-8 (BOM optional) or CP1252 fallback.

    Returns ``(text, encoding_name)``. Does not silently replace undecodable bytes.
    """
    if not content:
        msg = "CSV content is empty"
        raise ParserError(msg)
    if b"\x00" in content:
        msg = "CSV content must not contain NUL characters"
        raise ParserError(msg)
    if content.startswith(b"\xef\xbb\xbf"):
        try:
            return content.decode("utf-8-sig"), "utf-8-sig"
        except UnicodeDecodeError as exc:
            msg = "CSV content has a UTF-8 BOM but is not valid UTF-8"
            raise ParserError(msg) from exc
    try:
        return content.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    try:
        return content.decode("cp1252"), "cp1252"
    except UnicodeDecodeError as exc:
        msg = "CSV content is not valid UTF-8 or CP1252"
        raise ParserError(msg) from exc


def parse_football_data_csv(
    content: bytes,
    *,
    expected_division_code: str,
) -> ParsedFootballCsv:
    """Parse Football-Data CSV bytes with strict structural validation.

    Uses ``csv.reader(..., strict=True)`` and a bounded ``csv.field_size_limit``.
    Every ``csv.Error`` from reader construction, header retrieval, and each
    iterator advance is converted to ``ParserError``. The previous process-global
    field-size limit is always restored.
    """
    text, encoding = decode_csv_bytes(content)
    if any(len(line) > MAX_LINE_LENGTH for line in text.splitlines()):
        msg = f"CSV logical line exceeds maximum length of {MAX_LINE_LENGTH}"
        raise ParserError(msg)

    previous_limit = csv.field_size_limit()
    try:
        csv.field_size_limit(MAX_FIELD_LENGTH)
        try:
            reader = csv.reader(io.StringIO(text), strict=True)
        except csv.Error as exc:
            msg = "malformed CSV reader construction"
            raise ParserError(msg) from exc

        rows_iter = _iter_csv_rows(reader)
        try:
            header_row = next(rows_iter)
        except StopIteration as exc:
            msg = "CSV content has no header row"
            raise ParserError(msg) from exc

        headers = tuple(cell.strip() for cell in header_row)
        if not headers:
            msg = "CSV header row is empty"
            raise ParserError(msg)
        if any(name == "" for name in headers):
            msg = "CSV header contains an empty column name"
            raise ParserError(msg)
        if len(set(headers)) != len(headers):
            msg = "CSV header contains duplicate column names"
            raise ParserError(msg)
        for name in headers:
            if len(name) > MAX_FIELD_LENGTH:
                msg = f"CSV header name exceeds maximum length of {MAX_FIELD_LENGTH}"
                raise ParserError(msg)

        missing_required = [name for name in REQUIRED_COLUMNS if name not in headers]
        if missing_required:
            msg = f"CSV missing required columns: {', '.join(missing_required)}"
            raise ParserError(msg)

        recognized = tuple(
            name
            for name in headers
            if name in REQUIRED_COLUMNS or name in SUPPORTED_OPTIONAL_AND_ODDS
        )
        unknown = tuple(sorted(name for name in headers if name not in set(recognized)))
        missing_optional = tuple(sorted(name for name in OPTIONAL_COLUMNS if name not in headers))

        rows: list[dict[str, str]] = []
        signatures: dict[tuple[tuple[str, str], ...], int] = {}
        exact_duplicate_count = 0
        warnings: list[str] = []
        data_row_number = 1  # header consumed
        for raw_row in rows_iter:
            data_row_number += 1
            if len(raw_row) == 1 and raw_row[0].strip() == "":
                continue
            if all(cell.strip() == "" for cell in raw_row):
                continue
            if len(raw_row) > len(headers):
                msg = f"row {data_row_number}: wider than header"
                raise ParserError(msg)
            # Pad short rows with empty strings for optional trailing columns.
            padded = list(raw_row) + [""] * (len(headers) - len(raw_row))
            record = {headers[index]: padded[index] for index in range(len(headers))}
            for field_name, value in record.items():
                if len(value) > MAX_FIELD_LENGTH:
                    msg = (
                        f"row {data_row_number}: field {field_name} exceeds maximum "
                        f"length of {MAX_FIELD_LENGTH}"
                    )
                    raise ParserError(msg)
                if "\x00" in value:
                    msg = f"row {data_row_number}: field {field_name} contains NUL"
                    raise ParserError(msg)
            division = record["Div"].strip()
            if division != expected_division_code:
                msg = (
                    f"row {data_row_number}: Div {division!r} does not match expected "
                    f"division {expected_division_code!r}"
                )
                raise ParserError(msg)
            signature = tuple(sorted((key, record[key]) for key in headers))
            if signature in signatures:
                exact_duplicate_count += 1
            else:
                signatures[signature] = data_row_number
            rows.append(record)
            if len(rows) > MAX_ROW_COUNT:
                msg = f"CSV exceeds maximum row count of {MAX_ROW_COUNT}"
                raise ParserError(msg)

        if unknown:
            warnings.append(f"unknown_headers={len(unknown)}")
        if exact_duplicate_count:
            warnings.append(f"exact_duplicate_rows={exact_duplicate_count}")

        return ParsedFootballCsv(
            encoding=encoding,
            headers=headers,
            recognized_headers=recognized,
            unknown_headers=unknown,
            missing_optional_headers=missing_optional,
            rows=tuple(rows),
            row_count=len(rows),
            exact_duplicate_count=exact_duplicate_count,
            warnings=tuple(sorted(warnings)),
        )
    finally:
        csv.field_size_limit(previous_limit)


def _iter_csv_rows(reader: Iterator[list[str]]) -> Iterator[list[str]]:
    """Advance a CSV reader while converting every ``csv.Error`` to ``ParserError``.

    Errors raised by the iterator itself (including multiline quoted records and
    header retrieval) are caught here so callers never observe raw ``csv.Error``.
    """
    while True:
        try:
            row = next(reader)
        except StopIteration:
            return
        except csv.Error as exc:
            # Do not include raw row content in the error message.
            msg = "malformed CSV record"
            raise ParserError(msg) from exc
        yield row
