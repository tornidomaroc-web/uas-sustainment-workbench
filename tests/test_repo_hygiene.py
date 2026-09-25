"""Raw public flight logs carry GPS tracks and device ids of real people. None may be tracked."""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RAW_SUFFIXES = {".ulg", ".bin", ".tlog", ".gpx", ".kmz", ".mat", ".log", ".param"}


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
    offending = [f for f in tracked_files() if Path(f).suffix.lower() in RAW_SUFFIXES]
    assert offending == []


def test_nothing_under_data_raw_is_tracked_or_stageable() -> None:
    assert [f for f in tracked_files() if f.startswith(("data/raw/", "data/local/"))] == []
