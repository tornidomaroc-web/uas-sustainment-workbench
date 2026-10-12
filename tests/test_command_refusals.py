"""No `uasw` command ends in a traceback on ordinary bad input or state (issue #72).

A seed was many commits: refused or failed partway it left a partial store, and a second seed
on any store that held data ended in a `DuplicateFlight` traceback, so a partial store could
not be repaired by seeding again, and `uasw serve` did not seed it either since it had
flights. A seed is now one transaction, refused whole, and refused with one line on a store
that holds anything.

A sweep of every command with ordinary bad input or state found more tracebacks: a store
file that is not a database, is truncated, is a directory or sits under a file; a read-only
store written to; a date that is not a date; an output path that cannot be written. Each is
now one `refused (NNN)` line and exit 1, as the refusals the commands already gave.

The command-line cases run real `uasw` processes. Assertions are about exit codes, the
refusal's status and the rows in the store, never an operating system's error text, a path
or a line ending.
"""

from __future__ import annotations

import importlib
import os
import sqlite3
import stat
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from uas_workbench.ledger.project import project
from uas_workbench.service.store import Store

STORE = importlib.import_module("uas_workbench.service.store")
JOURNAL = importlib.import_module("uas_workbench.service.journal")
CLI = importlib.import_module("uas_workbench.service.cli")
FIXTURES = Path(__file__).parent / "fixtures"
LOG = next((FIXTURES / "px4").glob("*.ulg"))
SEEDED_HEAD = "c7726f26f1d1fddd45c679b1207d1eb0cebdf9c14b682f06c28d2d48d4384991"
WHO = ["--by", "A. Tester, maintenance", "--statement", "work done as described"]


def uasw(cwd: Path, *args: str) -> tuple[int, list[str], str]:
    """Run one `uasw` command in a process of its own: the exit code, the lines on stderr
    that are not the JSON log, and the whole of stderr."""
    env = {k: v for k, v in os.environ.items() if k != "UASW_WRITE_TOKEN"}
    r = subprocess.run(
        [sys.executable, "-m", "uas_workbench.service.cli", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    lines = [ln for ln in r.stderr.strip().splitlines() if not ln.startswith("{")]
    return r.returncode, lines, r.stderr


def counts(path: Path) -> tuple[int, int, int, int, str | None]:
    """Aircraft, flights, entries, journal links and the last link's hash, read on a
    connection of its own."""
    c = sqlite3.connect(str(path))
    try:
        found = [
            int(c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
            for t in ("aircraft", "flights", "entries", "journal")
        ]
        last = c.execute("SELECT link_hash FROM journal ORDER BY seq DESC LIMIT 1").fetchone()
    finally:
        c.close()
    return found[0], found[1], found[2], found[3], (str(last[0]) if last else None)


def assert_kept_equal_fresh(store: Store) -> None:
    assert store.journal_state() == store.verify_journal()
    assert store.journal_state().ok
    assert store.readable() is None
    assert store.projection() == project(store.entries())


@pytest.fixture
def seeded(tmp_path: Path) -> Path:
    db = tmp_path / "fleet.sqlite"
    code, _, err = uasw(tmp_path, "--db", str(db), "seed", "--fixtures", str(FIXTURES))
    assert code == 0, err
    return db


# ---- seed -----------------------------------------------------------------------------------


def test_a_second_seed_refuses_with_one_line_and_writes_nothing(
    tmp_path: Path, seeded: Path
) -> None:
    before = counts(seeded)
    assert before[4] == SEEDED_HEAD
    code, lines, err = uasw(tmp_path, "--db", str(seeded), "seed", "--fixtures", str(FIXTURES))
    assert "Traceback" not in err
    assert code == 1
    aircraft, flights, entries = before[:3]
    assert lines == [
        f"refused (409): the store already holds {aircraft} aircraft, {flights} flights and "
        f"{entries} entries; a seed writes only to an empty store, so nothing was written"
    ]
    assert counts(seeded) == before


@pytest.fixture(params=["memory", "file"])
def empty(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Store]:
    store = Store(":memory:" if request.param == "memory" else str(tmp_path / "fleet.sqlite"))
    yield store
    store.close()


def held(store: Store) -> tuple[int, int, int]:
    return len(store.aircraft()), store.flight_count(), store.entry_count()


def test_a_seed_that_fails_midway_leaves_the_store_as_it_was(
    empty: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A link that cannot be written partway through the seed (the 20th of about a hundred)
    rolls the whole seed back: no aircraft, flight or entry of it stays, the kept answers
    equal fresh ones, and the next seed writes the whole fleet with the pinned head."""
    link = JOURNAL.append_link
    calls = [0]

    def failing(*args: Any, **kwargs: Any) -> Any:
        calls[0] += 1
        if calls[0] == 20:
            raise OSError("the link could not be written")
        return link(*args, **kwargs)

    assert_kept_equal_fresh(empty)
    with monkeypatch.context() as m:
        m.setattr(JOURNAL, "append_link", failing)
        with pytest.raises(OSError):
            CLI.seed(empty, FIXTURES)
    assert held(empty) == (0, 0, 0)
    assert empty.verify_journal().length == 0
    assert_kept_equal_fresh(empty)
    CLI.seed(empty, FIXTURES)
    assert empty.verify_journal().head == SEEDED_HEAD
    assert_kept_equal_fresh(empty)


def test_a_seed_meeting_another_writer_midway_lands_whole_or_not_at_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Partway through the seed, when the store encodes the fifth flight record, a second
    connection tries to take the write lock and holds it if it can. A seed in one transaction
    holds the lock already, so the second connection cannot take it and the seed lands whole;
    seeding in many commits, the record was encoded between two of them, the second connection
    took the lock and the seed stopped with part of the fleet written."""
    monkeypatch.setattr(STORE, "BUSY_TIMEOUT_MS", 300)
    db = tmp_path / "fleet.sqlite"
    store = Store(str(db))
    encode = STORE.record_to_json
    calls = [0]
    holders: list[sqlite3.Connection] = []

    def contended(*args: Any, **kwargs: Any) -> Any:
        calls[0] += 1
        if calls[0] == 5:
            other = sqlite3.connect(str(db), isolation_level=None, timeout=0)
            try:
                other.execute("BEGIN IMMEDIATE")
                holders.append(other)
            except sqlite3.OperationalError:
                other.close()
        return encode(*args, **kwargs)

    monkeypatch.setattr(STORE, "record_to_json", contended)
    try:
        try:
            CLI.seed(store, FIXTURES)
            outcome = "seeded"
        except STORE.StoreBusy:
            outcome = "refused"
    finally:
        for other in holders:
            other.execute("ROLLBACK")
            other.close()
    monkeypatch.setattr(STORE, "record_to_json", encode)
    whole = held(store)
    assert (outcome, whole[0] > 0) in {("seeded", True), ("refused", False)}, (outcome, whole)
    if outcome == "seeded":
        assert store.verify_journal().head == SEEDED_HEAD
    else:
        assert whole == (0, 0, 0)
    assert_kept_equal_fresh(store)
    store.close()


def test_seed_writes_nothing_to_a_store_that_holds_anything(empty: Store) -> None:
    """What `uasw serve` relies on: it seeds a store only when the store is empty, so a
    store that holds one aircraft is left as it is, where it was seeded when it had no
    flight."""
    CLI.seed(empty, FIXTURES)
    whole = held(empty)
    head = empty.verify_journal().head
    assert CLI.seed(empty, FIXTURES) == whole
    assert held(empty) == whole and empty.verify_journal().head == head


# ---- the sweep: one refusal line, never a traceback -----------------------------------------


def _case_paths(tmp_path: Path, seeded: Path) -> dict[str, str]:
    notdb = tmp_path / "notdb.sqlite"
    notdb.write_text("this is not a database\n")
    trunc = tmp_path / "trunc.sqlite"
    trunc.write_bytes(seeded.read_bytes()[:3000])
    adir = tmp_path / "adir"
    adir.mkdir()
    afile = tmp_path / "afile"
    afile.write_text("x")
    return {
        "good": str(seeded),
        "notdb": str(notdb),
        "trunc": str(trunc),
        "adir": str(adir),
        "afile": str(afile),
        "underfile": str(afile / "fleet.sqlite"),
        "out": str(tmp_path / "out.html"),
        "outunder": str(afile / "out.html"),
    }


SWEEP: list[tuple[str, int, list[str]]] = [
    ("seed into a directory", 503, ["--db", "{adir}", "seed", "--fixtures", "{fixtures}"]),
    ("seed into a file that is not a database", 503, ["--db", "{notdb}", "seed"]),
    ("verify a file that is not a database", 503, ["--db", "{notdb}", "verify"]),
    ("verify a truncated store", 503, ["--db", "{trunc}", "verify"]),
    ("verify a store under a file", 503, ["--db", "{underfile}", "verify"]),
    ("serve a file that is not a database", 503, ["--db", "{notdb}", "serve", "--port", "9"]),
    (
        "record with --at not a date",
        422,
        ["--db", "{good}", "record", "time-in-service", "SYN-03", "5", *WHO, "--at", "yesterday"],
    ),
    (
        "evidence with --as-of not a date",
        422,
        ["--db", "{good}", "evidence", "SYN-04", "--as-of", "soon", "--out", "{out}"],
    ),
    (
        "records with --as-of not a date",
        422,
        ["--db", "{good}", "records", "SYN-04", "--as-of", "soon", "--out", "{out}"],
    ),
    (
        "fleet-records with --as-of not a date",
        422,
        ["--db", "{good}", "fleet-records", "--as-of", "soon", "--out", "{out}"],
    ),
    ("ask with --as-of not a date", 422, ["ask", "is SYN-01 serviceable?", "--as-of", "soon"]),
    (
        "evidence to a path under a file",
        422,
        ["--db", "{good}", "evidence", "SYN-04", "--out", "{outunder}"],
    ),
    (
        "records to a path under a file",
        422,
        ["--db", "{good}", "records", "SYN-04", "--out", "{outunder}"],
    ),
    (
        "fleet-records to a path under a file",
        422,
        ["--db", "{good}", "fleet-records", "--out", "{outunder}"],
    ),
    (
        "export-static to a path that is a file",
        422,
        ["--db", "{good}", "export-static", "--out", "{afile}"],
    ),
]


@pytest.mark.parametrize(("case", "status", "args"), SWEEP, ids=[c[0] for c in SWEEP])
def test_bad_input_or_state_is_one_refusal_line_never_a_traceback(
    tmp_path: Path, seeded: Path, case: str, status: int, args: list[str]
) -> None:
    paths = _case_paths(tmp_path, seeded) | {"fixtures": str(FIXTURES)}
    before = counts(seeded)
    code, lines, err = uasw(tmp_path, *[a.format(**paths) for a in args])
    assert "Traceback" not in err, err[-400:]
    assert code == 1
    assert len(lines) == 1 and lines[0].startswith(f"refused ({status}): "), lines
    assert counts(seeded) == before


@pytest.mark.parametrize(
    "args",
    [
        ["record", "time-in-service", "SYN-03", "5", *WHO],
        ["ingest", "NEW-01", str(LOG)],
    ],
    ids=["record", "ingest"],
)
def test_a_write_to_a_read_only_store_is_one_refusal_line(
    tmp_path: Path, seeded: Path, args: list[str]
) -> None:
    before = counts(seeded)
    os.chmod(seeded, stat.S_IREAD)
    try:
        code, lines, err = uasw(tmp_path, "--db", str(seeded), *args)
    finally:
        os.chmod(seeded, stat.S_IREAD | stat.S_IWRITE)
    assert "Traceback" not in err, err[-400:]
    assert code == 1
    assert len(lines) == 1 and lines[0].startswith("refused (503): "), lines
    assert counts(seeded) == before
