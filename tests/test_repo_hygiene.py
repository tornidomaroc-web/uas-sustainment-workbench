"""Raw public flight logs carry GPS tracks and device ids of real people. None may be tracked.

The only log files allowed are the named, position-free ALFA excerpts; their content is
checked in test_alfa_fixtures.py."""

import hashlib
import re
import shutil
import subprocess
from itertools import pairwise
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
# Makers and products of civil aircraft, drones and flight-controller boards, stored as the
# first 16 hex digits of the SHA-256 of the lower-case name with spaces, dashes and
# underscores removed, so this file names none of them. Not exhaustive: it holds the names
# once found here and the most common ones. To add one:
#   python -c "import hashlib; print(hashlib.sha256(b'name').hexdigest()[:16])"
PRODUCT_HASHES = frozenset(
    {
        "384585e6cc15335f",
        "1439517ee945202a",
        "69f1e20ffdfa1206",
        "30452f10d5fcecdb",
        "933e3d170e5134b1",
        "d4690300ba7d6a12",
        "7170d6c415bead47",
        "cc82154c6586e5cf",
        "b2237cd7a1c7bd39",
        "7eb827c33968d887",
        "0b4430f8220792ae",
        "9b824b8ee161ee2a",
        "fa208e72f8f6208a",
        "74ba29f25ae4b1c4",
        "a0c2ede4f4f84e06",
        "feeac41c55a861b0",
        "7c8706ecae87936e",
        "3781b820e0947c59",
        "1e57df8db704b74d",
        "b46e71a4bba2477f",
        "5f5943d29c13ac61",
        "d6062bb36f0e8016",
        "aa27ef319f12037e",
        "0a0ecbb7a6ea8511",
    }
)
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


def test_no_real_aircraft_or_drone_product_is_named() -> None:
    """The repository names no real platform. PX4 and ArduPilot are open autopilot software
    and stay. Binary fixtures are searched as their printable strings."""
    hits: list[str] = []
    for name in tracked_files():
        path = ROOT / name
        if not path.is_file():
            continue
        runs = re.findall(rb"[\x20-\x7e]{4,}", path.read_bytes())
        words = re.findall(r"[a-z0-9]+", " ".join(r.decode("ascii") for r in runs).lower())
        candidates = set(words) | {a + b for a, b in pairwise(words)}
        hits.extend(f"{name}: {c}" for c in sorted(candidates) if _digest(c) in PRODUCT_HASHES)
    assert hits == []


def _digest(word: str) -> str:
    return hashlib.sha256(word.encode()).hexdigest()[:16]


def test_nothing_under_data_raw_is_tracked_or_stageable() -> None:
    assert [f for f in tracked_files() if f.startswith(("data/raw/", "data/local/"))] == []
