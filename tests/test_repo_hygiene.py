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
# The words the README's scope bullet (the "No ... functions" line) reserves, which nothing
# else in the repository may use, identifiers included: the same 16 hex digits of the SHA-256
# of the lower-case word, and of a two-word term with its separator removed, so this file
# spells none of them. The recipe above adds one.
RESERVED_HASHES = frozenset(
    {"239f59ed55e737c7", "4f5757942fb3e216", "495b93a386a436dc", "40c4d254311f6a07"}
)
# Where a reserved word legitimately stands, by file and by the digest of the exact text
# that holds it, never a whole file: the README's scope bullet (the digest of that one
# line) and, inside the pinned PX4 binary excerpt, the upstream field and parameter names
# that carry one (the digest of each identifier). Anything else anywhere fails.
RESERVED_ALLOWED: dict[str, frozenset[str]] = {
    "README.md": frozenset({"bb2e9a6e56fff150"}),
    "tests/fixtures/px4/flight_review_board_validation_2026-06-12_excerpt.ulg": frozenset(
        {
            "cce86690be9840c7",
            "d8e0ab2ec4783c34",
            "ea67852dfd99d493",
            "dce6f21b9250022c",
            "6061633512fc21dd",
        }
    ),
}
PRINTABLE = re.compile(rb"[\x20-\x7e]{4,}")
IDENTIFIER = re.compile(r"[A-Za-z0-9_]+")
CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
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
        runs = PRINTABLE.findall(path.read_bytes())
        words = re.findall(r"[a-z0-9]+", " ".join(r.decode("ascii") for r in runs).lower())
        candidates = set(words) | {a + b for a, b in pairwise(words)}
        hits.extend(f"{name}: {c}" for c in sorted(candidates) if _digest(c) in PRODUCT_HASHES)
    assert hits == []


def _digest(word: str) -> str:
    return hashlib.sha256(word.encode()).hexdigest()[:16]


def test_nothing_under_data_raw_is_tracked_or_stageable() -> None:
    assert [f for f in tracked_files() if f.startswith(("data/raw/", "data/local/"))] == []


def identifier_words(identifier: str) -> list[str]:
    """The lower-case words of a snake_case, camelCase or UPPER_CASE identifier, digits apart:
    'voltage5v_v' is voltage, 5, v, v; 'HTTPServerLog' is http, server, log. A dotted name
    is several identifiers already."""
    return re.findall(r"[a-z]+|[0-9]+", CAMEL.sub(" ", identifier).lower())


def test_identifier_words_split() -> None:
    assert identifier_words("voltage5v_v") == ["voltage", "5", "v", "v"]
    assert identifier_words("HTTPServerLog") == ["http", "server", "log"]
    assert identifier_words("rail_valid_FLAG") == ["rail", "valid", "flag"]


def test_no_reserved_scope_word_is_used() -> None:
    """Every tracked file, binary fixtures as their printable strings, uses none of the
    reserved words outside RESERVED_ALLOWED: as a word, inside an identifier, or as two
    adjacent words (a hyphenated term)."""
    hits: dict[str, None] = {}
    for name in tracked_files():
        path = ROOT / name
        if not path.is_file():
            continue
        allowed = RESERVED_ALLOWED.get(name, frozenset())
        data = path.read_bytes()
        for run in PRINTABLE.finditer(data):
            text = run.group().decode("ascii")
            if _digest(text.strip()) in allowed:
                continue
            line = data.count(b"\n", 0, run.start()) + 1
            words = [(w, i) for i in IDENTIFIER.findall(text) for w in identifier_words(i)]
            for j, (word, ident) in enumerate(words):
                scopes = [(word, [ident])]
                if j + 1 < len(words):
                    next_word, next_ident = words[j + 1]
                    scopes.append((word + next_word, [ident, next_ident]))
                for candidate, idents in scopes:
                    if _digest(candidate) in RESERVED_HASHES and not all(
                        _digest(i) in allowed for i in idents
                    ):
                        hits[f"{name}:{line}: {' '.join(dict.fromkeys(idents))}"] = None
    assert list(hits) == []
