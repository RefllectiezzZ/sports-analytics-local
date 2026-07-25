"""Tests for strict Football-Data.co.uk CSV parsing."""

from __future__ import annotations

from pathlib import Path

import pytest

from sports_analytics.core.exceptions import ParserError
from sports_analytics.sources.football_data_co_uk.parser import parse_football_data_csv


def _fixture_bytes(name: str) -> bytes:
    return (Path(__file__).parents[2] / "fixtures" / "football_data_co_uk" / name).read_bytes()


def test_parse_synthetic_premier_league_fixture_reports_headers_and_warnings() -> None:
    parsed = parse_football_data_csv(
        _fixture_bytes("epl_2023_2024_synthetic.csv"),
        expected_division_code="E0",
    )

    assert parsed.encoding == "utf-8"
    assert parsed.row_count == 3
    assert parsed.exact_duplicate_count == 0
    assert parsed.headers[0:4] == ("Div", "Date", "Time", "HomeTeam")
    assert "B365H" in parsed.recognized_headers
    assert parsed.unknown_headers == ("WeirdBookH",)
    assert parsed.warnings == ("unknown_headers=1",)
    assert parsed.rows[0]["HomeTeam"] == "Northbridge FC"
    assert parsed.rows[2]["Time"] == ""


def test_parse_primeira_liga_fixture_accepts_expected_division() -> None:
    parsed = parse_football_data_csv(
        _fixture_bytes("prt_2023_2024_synthetic.csv"),
        expected_division_code="P1",
    )

    assert parsed.row_count == 2
    assert parsed.unknown_headers == ()
    assert parsed.rows[0]["Div"] == "P1"
    assert parsed.rows[1]["FTR"] == "D"


def test_parse_accepts_utf8_bom() -> None:
    content = (
        "\ufeffDiv,Date,HomeTeam,AwayTeam,FTHG,FTAG,FTR\n"
        "E0,01/05/2024,Northbridge FC,Southport Athletic,,,\n"
    ).encode()

    parsed = parse_football_data_csv(content, expected_division_code="E0")

    assert parsed.encoding == "utf-8-sig"
    assert parsed.row_count == 1


def test_parse_falls_back_to_cp1252_when_utf8_fails() -> None:
    content = (
        "Div,Date,HomeTeam,AwayTeam,FTHG,FTAG,FTR\n"
        "E0,01/05/2024,Caf\xe9 FC,Southport Athletic,,,\n"
    ).encode("cp1252")

    parsed = parse_football_data_csv(content, expected_division_code="E0")

    assert parsed.encoding == "cp1252"
    assert parsed.rows[0]["HomeTeam"] == "Caf\u00e9 FC"


def test_parse_ignores_blank_rows() -> None:
    content = (
        b"Div,Date,HomeTeam,AwayTeam,FTHG,FTAG,FTR\n"
        b"\n"
        b",,,,,,\n"
        b"E0,01/05/2024,Northbridge FC,Southport Athletic,,,\n"
    )

    parsed = parse_football_data_csv(content, expected_division_code="E0")

    assert parsed.row_count == 1


def test_parse_pads_short_rows_with_empty_strings() -> None:
    content = (
        b"Div,Date,HomeTeam,AwayTeam,FTHG,FTAG,FTR,Time\n"
        b"E0,01/05/2024,Northbridge FC,Southport Athletic,,,\n"
    )

    parsed = parse_football_data_csv(content, expected_division_code="E0")

    assert parsed.rows[0]["Time"] == ""


def test_parse_counts_exact_duplicate_rows() -> None:
    row = b"E0,01/05/2024,Northbridge FC,Southport Athletic,,,\n"
    content = b"Div,Date,HomeTeam,AwayTeam,FTHG,FTAG,FTR\n" + row + row

    parsed = parse_football_data_csv(content, expected_division_code="E0")

    assert parsed.row_count == 2
    assert parsed.exact_duplicate_count == 1
    assert parsed.warnings == ("exact_duplicate_rows=1",)


def test_parse_rejects_empty_content() -> None:
    with pytest.raises(ParserError, match="empty"):
        parse_football_data_csv(b"", expected_division_code="E0")


def test_parse_rejects_nul_bytes() -> None:
    with pytest.raises(ParserError, match="NUL"):
        parse_football_data_csv(b"Div\x00,Date\n", expected_division_code="E0")


def test_parse_rejects_missing_required_columns() -> None:
    content = b"Div,Date,HomeTeam,AwayTeam,FTHG,FTAG\nE0,01/05/2024,A,B,,\n"

    with pytest.raises(ParserError, match="missing required columns: FTR"):
        parse_football_data_csv(content, expected_division_code="E0")


def test_parse_rejects_duplicate_header_names() -> None:
    content = b"Div,Date,HomeTeam,AwayTeam,FTHG,FTAG,FTR,FTR\nE0,01/05/2024,A,B,,,\n"

    with pytest.raises(ParserError, match="duplicate column names"):
        parse_football_data_csv(content, expected_division_code="E0")


def test_parse_rejects_rows_wider_than_header() -> None:
    content = b"Div,Date,HomeTeam,AwayTeam,FTHG,FTAG,FTR\nE0,01/05/2024,A,B,,,,extra\n"

    with pytest.raises(ParserError, match="wider than header"):
        parse_football_data_csv(content, expected_division_code="E0")


def test_parse_rejects_unexpected_division_code() -> None:
    content = b"Div,Date,HomeTeam,AwayTeam,FTHG,FTAG,FTR\nP1,01/05/2024,A,B,,,\n"

    with pytest.raises(ParserError, match="does not match expected division"):
        parse_football_data_csv(content, expected_division_code="E0")
