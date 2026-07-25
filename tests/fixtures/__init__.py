"""Shared synthetic Football-Data.co.uk fixtures for offline tests."""

from __future__ import annotations

from pathlib import Path

FIXTURES_DIR = Path(__file__).resolve().parent / "football_data_co_uk"


def read_fixture(name: str) -> bytes:
    """Return fixture bytes from the synthetic football-data fixtures directory."""
    path = FIXTURES_DIR / name
    return path.read_bytes()


MINIMAL_EPL_CSV = (
    "Div,Date,HomeTeam,AwayTeam,FTHG,FTAG,FTR\n"
    "E0,12/08/2023,Northbridge FC,Southport Athletic,2,1,H\n"
)

MINIMAL_PRT_CSV = (
    "Div,Date,Time,HomeTeam,AwayTeam,FTHG,FTAG,FTR,B365H,B365D,B365A\n"
    "P1,11/08/2023,20:15,Lisboa Azul,Porto Verde,1,0,H,2.10,3.25,3.60\n"
)
