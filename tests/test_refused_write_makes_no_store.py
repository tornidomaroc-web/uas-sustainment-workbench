"""A command that writes and is refused makes no store (issue #83).

`uasw ingest` and `uasw record` make the store on first use. Given a path with nothing at it
and refused (a file that is not a log, a date that is not a date, an aircraft or component
the store does not hold, a kind fleet.toml does not name, a date in the future), each still
made the store there first: an empty 36,864-byte file with no rows, which `uasw verify` then
called intact, the trap issue #73 closed for the commands that only read. Each now checks
before it makes anything, so a refusal leaves no file, side file or folder; a write that is
accepted makes the store as before. Nothing is ever removed: a store that was there, or that
another process makes meanwhile, is never deleted or altered by a refusal.

Every command runs in a process of its own, from a working folder that is not the repository.
Assertions are about exit codes, refusal statuses and files, never an operating system's
error text, a path or a line ending.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
LOG = next((FIXTURES / "px4").glob("*.ulg"))
SEEDED_HEAD = "c7726f26f1d1fddd45c679b1207d1eb0cebdf9c14b682f06c28d2d48d4384991"
WHO = ["--by", "A. Tester, maintenance", "--statement", "work done as described"]
REGISTER = ["record", "component", "register", "BAT-NEW", "--kind", "battery pack"]
REGISTER += ["--since", "2026-01-01", *WHO]


def command(*args: str) -> list[str]:
    return [sys.executable, "-m", "uas_workbench.service.cli", *args]


def child_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in ("UASW_WRITE_TOKEN", "UASW_FIXTURES")}


def uasw(cwd: Path, *args: str) -> tuple[int, list[str], str]:
    r = subprocess.run(
        command(*args), cwd=cwd, env=child_env(), capture_output=True, text=True, timeout=180
    )
    lines = [ln for ln in r.stderr.strip().splitlines() if not ln.startswith("{")]
    return r.returncode, lines, r.stderr


def files(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*")}


def rows(db: Path) -> tuple[int, int, int, str | None]:
    c = sqlite3.connect(str(db))
    try:
        n = [
            int(c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
            for t in ("aircraft", "flights", "entries")
        ]
        last = c.execute("SELECT link_hash FROM journal ORDER BY seq DESC LIMIT 1").fetchone()
    finally:
        c.close()
    return n[0], n[1], n[2], (str(last[0]) if last else None)


def verified(cwd: Path, db: Path) -> str:
    code, _, err = uasw(cwd, "--db", str(db), "verify")
    assert code == 0, err[-400:]
    return err


# ---- a refused write on a path with nothing at it makes nothing ------------------------------

# (name, arguments, status). `{junk}`, `{text}` and `{log}` are files in the working folder.
REFUSED: list[tuple[str, list[str], int]] = [
    ("ingest a file that is not a log", ["ingest", "OWN-1", "{junk}"], 422),
    ("ingest an unsupported file type", ["ingest", "OWN-1", "{text}"], 422),
    ("ingest two files that are not logs", ["ingest", "OWN-1", "{junk}", "{text}"], 422),
    (
        "record with --at not a date",
        ["record", "inspection", "X-1", "annual", *WHO, "--at", "notadate"],
        422,
    ),
    (
        "record dated in the future",
        ["record", "inspection", "X-1", "annual", *WHO, "--at", "2099-01-01T00:00Z"],
        422,
    ),
    (
        "record an inspection of an unknown aircraft",
        ["record", "inspection", "X-1", "annual", *WHO],
        404,
    ),
    (
        "open a work order on an unknown aircraft",
        ["record", "work-order", "open", "X-1", "--state", "in_work", *WHO],
        404,
    ),
    (
        "close a work order on an unknown aircraft",
        ["record", "work-order", "close", "X-1", "WO-1", *WHO],
        404,
    ),
    (
        "set time in service of an unknown aircraft",
        ["record", "time-in-service", "X-1", "12", *WHO],
        404,
    ),
    ("install an unknown component", ["record", "component", "install", "BAT-X", "X-1", *WHO], 404),
    ("remove an unknown component", ["record", "component", "remove", "BAT-X", "X-1", *WHO], 404),
    (
        "register a kind fleet.toml does not name",
        [
            *["record", "component", "register", "BAT-X", "--kind", "no such kind"],
            *["--since", "2026-01-01", *WHO],
        ],
        422,
    ),
    (
        "correct an entry that does not exist",
        [*REGISTER, "--supersedes", "7", "--reason", "a typo"],
        404,
    ),
]


@pytest.mark.parametrize("where", ["beside", "in a folder that does not exist"])
@pytest.mark.parametrize(("name", "args", "status"), REFUSED, ids=[r[0] for r in REFUSED])
def test_a_refused_write_on_a_new_path_makes_no_store(
    tmp_path: Path, name: str, args: list[str], status: int, where: str
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    (work / "junk.bin").write_bytes(b"not a log at all")
    (work / "notes.txt").write_text("not a log either")
    db = work / "fleet-typo.sqlite" if where == "beside" else work / "nowhere" / "fleet.sqlite"
    fill = {"junk": "junk.bin", "text": "notes.txt", "log": str(LOG)}
    before = files(tmp_path)
    code, lines, err = uasw(work, "--db", str(db), *[a.format(**fill) for a in args])
    assert "Traceback" not in err, err[-400:]
    assert code == 1
    assert lines and all(ln.startswith(f"refused ({status}): ") for ln in lines), lines
    # No store, no -wal, -shm or -journal file, no folder.
    assert files(tmp_path) == before


def test_an_ingest_with_one_readable_log_makes_the_store_for_it(tmp_path: Path) -> None:
    """A refused log does not stop the next one, and the readable one makes the store, as
    before: exit 1 for the refusal, the flight stored."""
    (tmp_path / "junk.bin").write_bytes(b"not a log at all")
    db = tmp_path / "new" / "fleet.sqlite"
    code, lines, err = uasw(tmp_path, "--db", str(db), "ingest", "OWN-1", "junk.bin", str(LOG))
    assert "Traceback" not in err, err[-400:]
    assert code == 1
    assert len(lines) == 1 and lines[0].startswith("refused (422): "), lines
    assert rows(db)[:3] == (1, 1, 0)
    verified(tmp_path, db)


def test_an_accepted_write_on_a_new_path_makes_the_store_as_before(tmp_path: Path) -> None:
    db = tmp_path / "new" / "fleet.sqlite"
    code, _, err = uasw(tmp_path, "--db", str(db), *REGISTER)
    assert code == 0, err[-400:]
    assert rows(db)[:3] == (0, 0, 1)
    verified(tmp_path, db)


# ---- a store that was there is left as it was --------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        ["record", "inspection", "SYN-01", "annual", *WHO, "--at", "notadate"],
        ["record", "inspection", "X-1", "annual", *WHO],
        ["ingest", "OWN-1", "junk.bin"],
    ],
    ids=["bad --at", "unknown aircraft", "not a log"],
)
def test_a_refused_write_leaves_an_existing_store_as_it_was(
    tmp_path: Path, args: list[str]
) -> None:
    (tmp_path / "junk.bin").write_bytes(b"not a log at all")
    db = tmp_path / "fleet.sqlite"
    code, _, err = uasw(tmp_path, "--db", str(db), *REGISTER)
    assert code == 0, err[-400:]
    held = rows(db)
    listing = files(tmp_path)
    code, lines, err = uasw(tmp_path, "--db", str(db), *args)
    assert "Traceback" not in err, err[-400:]
    assert code == 1 and lines and lines[0].startswith("refused ("), lines
    assert rows(db) == held
    assert files(tmp_path) == listing


# ---- two processes on one new path --------------------------------------------------------------


def race(cwd: Path, first: list[str], second: list[str]) -> tuple[int, int, str]:
    """Start both commands at once and wait for both; their exit codes and the second's
    stderr."""
    env = child_env()
    a = subprocess.Popen(
        command(*first),
        cwd=cwd,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    b = subprocess.Popen(
        command(*second),
        cwd=cwd,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    _, err_a = a.communicate(timeout=180)
    _, err_b = b.communicate(timeout=180)
    assert "Traceback" not in err_a + err_b, (err_a + err_b)[-600:]
    return a.returncode, b.returncode, err_b


ROUNDS = 5


def test_a_refusal_never_undoes_a_store_another_process_makes(tmp_path: Path) -> None:
    """A write that is accepted and one that is refused, on one new path at once: the store
    holds the accepted entry and verifies, in every round, whichever process starts first."""
    for n in range(ROUNDS):
        db = tmp_path / f"r{n}" / "fleet.sqlite"
        refused = ["--db", str(db), "record", "inspection", "X-1", "annual", *WHO]
        accepted = ["--db", str(db), *REGISTER]
        pair = (accepted, refused) if n % 2 == 0 else (refused, accepted)
        code_a, code_b, _ = race(tmp_path, *pair)
        codes = (code_a, code_b) if n % 2 == 0 else (code_b, code_a)
        assert codes == (0, 1), (n, codes)
        assert rows(db)[:3] == (0, 0, 1), n
        verified(tmp_path, db)


def test_a_refusal_beside_a_seed_leaves_the_seeded_store_whole(tmp_path: Path) -> None:
    for n in range(ROUNDS):
        db = tmp_path / f"s{n}" / "fleet.sqlite"
        seed = ["--db", str(db), "seed", "--fixtures", str(FIXTURES)]
        refused = ["--db", str(db), "record", "inspection", "X-1", "annual", *WHO]
        code_seed, code_refused, _ = race(tmp_path, seed, refused)
        assert (code_seed, code_refused) == (0, 1), n
        held = rows(db)
        assert held[:2] == (9, 34) and held[3] == SEEDED_HEAD, (n, held)


# ---- what --help says about the fixtures default -------------------------------------------------


def test_help_names_where_the_fixtures_default_comes_from(tmp_path: Path) -> None:
    r = subprocess.run(
        command("--help"),
        cwd=tmp_path,
        env=child_env(),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert r.returncode == 0
    text = " ".join(r.stdout.split())
    assert "beside the code" in text, text
    assert "working folder" in text, text
