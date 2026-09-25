"""Raw public flight logs carry GPS tracks and device ids of real people. None may be tracked.

The only log files allowed are the named, position-free ALFA excerpts; their content is
checked in test_alfa_fixtures.py."""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RAW_SUFFIXES = {".ulg", ".bin", ".tlog", ".gpx", ".kmz", ".mat", ".log", ".param"}
ALLOWED_EXCERPTS = {
    f"tests/fixtures/alfa/{name}.bin"
    for name in ("2018-07-30_16-30-14", "2018-07-30_16-46-36", "2018-07-30_17-28-50")
} | {"tests/fixtures/px4/flight_review_board_validation_2026-06-12_excerpt.ulg"}


def tracked_files() -> list[str]:
    if shutil.which("git") is None or not (ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.splitlines()


def test_no_raw_flight_log_is_tracked_or_stageable() -> None:
    offending = [
        f
        for f in tracked_files()
        if Path(f).suffix.lower() in RAW_SUFFIXES and f not in ALLOWED_EXCERPTS
    ]
    assert offending == []


def test_nothing_under_data_raw_is_tracked_or_stageable() -> None:
    assert [f for f in tracked_files() if f.startswith(("data/raw/", "data/local/"))] == []
