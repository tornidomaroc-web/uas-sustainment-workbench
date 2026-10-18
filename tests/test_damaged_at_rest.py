"""A store file damaged at rest is refused by every command that writes, and by verify (#104, #105).

`uasw record` and `uasw ingest` opened an existing store without checking the file, so on a
store damaged while no process had it open they wrote and exited 0 whenever the damage missed
the pages their own reads touch: only the `flights` pages for `record`, only the `entries`
pages or only `sqlite_sequence` for `ingest`. The entry joined a file every later `verify`
refused. And `uasw verify` walks the journal chain, not the file: it reported the journal intact
on a file whose `aircraft` page, one of whose automatic indexes or whose `sqlite_sequence` page
was overwritten.

Every command that writes now runs SQLite's `PRAGMA integrity_check` when it opens an existing
store file, before anything else touches it, and `uasw verify` runs it before the walk: a file
that fails is one `refused (503)` line naming the cause, exit 1, the file byte for byte as it
was and nothing left beside it. `integrity_check` rather than `quick_check`, the check the
service runs on a file changed under it: it also finds an index whose keys no longer match
their rows, which `quick_check` passes (the last case below).

Every case damages a copy of one freshly seeded store, with no process holding it, the way the
release session measured: each page of one b-tree overwritten with the bytes 0 to 255 repeated,
or one key of an index edited in place. Every command is a real `uasw` process; `uasw serve`
logs to a file. Assertions are about exit codes, the refusal's status, the store file's bytes
and the folder's contents, never an operating system's error text, a path or a line ending.
"""

from __future__ import annotations

import os
import shutil
import socket
import sqlite3
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
LOG = next((FIXTURES / "px4").glob("*.ulg"))
SEEDED_HEAD = "c7726f26f1d1fddd45c679b1207d1eb0cebdf9c14b682f06c28d2d48d4384991"
WHO = ["--by", "A. Tester, maintenance", "--statement", "work done as described"]
PATTERN = bytes(range(256)) * 16
CHECK = "fails SQLite's integrity check"
SERVE = [sys.executable, "-m", "uas_workbench.service.cli"]


def uasw(cwd: Path, *args: str) -> tuple[int, list[str], str, str]:
    """Run one `uasw` command in a process of its own: the exit code, the lines on stderr
    that are not the JSON log, the whole of stderr and stdout."""
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
    return r.returncode, lines, r.stderr, r.stdout


def btree_pages(data: bytes, root: int, size: int) -> list[int]:
    """The pages of the b-tree rooted at `root`, read from the file's bytes."""
    found, todo = [], [root]
    while todo:
        number = todo.pop()
        found.append(number)
        start = (number - 1) * size
        header = start + (100 if number == 1 else 0)
        if data[header] in (0x02, 0x05):  # an interior page: its children, then the right one
            (cells,) = struct.unpack(">H", data[header + 3 : header + 5])
            todo.append(struct.unpack(">I", data[header + 8 : header + 12])[0])
            for i in range(cells):
                at = header + 12 + 2 * i
                (cell,) = struct.unpack(">H", data[at : at + 2])
                todo.append(struct.unpack(">I", data[start + cell : start + cell + 4])[0])
    return sorted(found)


def sqlite_says(path: Path, pragma: str) -> str:
    c = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        return str(c.execute(f"PRAGMA {pragma}(1)").fetchone()[0])
    except sqlite3.DatabaseError as exc:
        return str(exc)
    finally:
        c.close()


@pytest.fixture(scope="module")
def seeded(tmp_path_factory: pytest.TempPathFactory) -> bytes:
    """The bytes of one freshly seeded store, one file with nothing beside it."""
    folder = tmp_path_factory.mktemp("seeded")
    db = folder / "fleet.sqlite"
    code, _, err, _ = uasw(folder, "--db", str(db), "seed", "--fixtures", str(FIXTURES))
    assert code == 0, err
    assert sorted(p.name for p in folder.iterdir()) == ["fleet.sqlite"]
    return db.read_bytes()


def overwrite(names: list[str]) -> Callable[[bytes, Path], bytes]:
    """Every page of the b-trees named, overwritten as the release session did."""

    def damage(data: bytes, db: Path) -> bytes:
        c = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        try:
            size = int(c.execute("PRAGMA page_size").fetchone()[0])
            roots = [
                int(
                    c.execute("SELECT rootpage FROM sqlite_master WHERE name = ?", (n,)).fetchone()[
                        0
                    ]
                )
                for n in names
            ]
        finally:
            c.close()
        out = bytearray(data)
        for root in roots:
            for page in btree_pages(data, root, size):
                assert page != 1
                out[(page - 1) * size : page * size] = (PATTERN * (size // len(PATTERN) + 1))[:size]
        return bytes(out)

    return damage


def edit_index_key(data: bytes, db: Path) -> bytes:
    """One key of the flights index edited in place: SYN-02's first log_ref, one digit changed,
    the page's structure untouched, so the index no longer finds its row."""
    c = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        size = int(c.execute("PRAGMA page_size").fetchone()[0])
        name = "sqlite_autoindex_flights_1"
        root = int(
            c.execute("SELECT rootpage FROM sqlite_master WHERE name = ?", (name,)).fetchone()[0]
        )
    finally:
        c.close()
    out = bytearray(data)
    for page in btree_pages(data, root, size):
        at = data.find(b"SYN-02SYN-02-", (page - 1) * size, page * size)
        if at >= 0:
            assert out[at + 10] == ord("0")
            out[at + 10] = ord("1")  # SYN-02-... becomes SYN-12-..., the same length
            return bytes(out)
    raise AssertionError("no SYN-02 key in the flights index")


DAMAGE: dict[str, Callable[[bytes, Path], bytes]] = {
    "flights pages": overwrite(["flights"]),
    "entries pages": overwrite(["entries"]),
    "sqlite_sequence page": overwrite(["sqlite_sequence"]),
    "aircraft page": overwrite(["aircraft"]),
    "journal pages": overwrite(["journal"]),
    "aircraft index": overwrite(["sqlite_autoindex_aircraft_1"]),
    "flights index": overwrite(["sqlite_autoindex_flights_1"]),
    "journal index": overwrite(["sqlite_autoindex_journal_1"]),
    "flights index key edited": edit_index_key,
}
# The cases the release session measured a write going through on, and the one only
# integrity_check finds.
FEW = ["flights pages", "sqlite_sequence page", "flights index key edited"]


def damaged(tmp_path: Path, seeded: bytes, case: str) -> tuple[Path, bytes]:
    """A copy of the seeded store damaged at rest, alone in its folder, and its bytes."""
    folder = tmp_path / "store"
    folder.mkdir()
    db = folder / "fleet.sqlite"
    db.write_bytes(seeded)
    data = DAMAGE[case](seeded, db)
    db.write_bytes(data)
    assert sqlite_says(db, "integrity_check") != "ok"
    if case == "flights index key edited":
        # Why this case is here: SQLite's quicker check passes it.
        assert sqlite_says(db, "quick_check") == "ok"
    return db, data


def assert_refused_untouched(
    db: Path, data: bytes, code: int, lines: list[str], err: str, out: str = ""
) -> None:
    assert "Traceback" not in err, err[-600:]
    assert code == 1, (lines, out)
    assert len(lines) == 1 and lines[0].startswith("refused (503): "), lines
    assert CHECK in lines[0], lines
    assert db.read_bytes() == data
    assert sorted(p.name for p in db.parent.iterdir()) == ["fleet.sqlite"]


WRITES: dict[str, list[str]] = {
    "record": ["record", "work-order", "open", "SYN-04", "--state", "in_work", *WHO],
    "ingest": ["ingest", "SYN-04", str(LOG)],
    "record retract": ["record", "retract", "1", "--by", "A. Tester", "--reason", "wrong aircraft"],
    "seed": ["seed", "--fixtures", str(FIXTURES)],
}
MATRIX = [(w, c) for w in ("record", "ingest") for c in DAMAGE] + [
    (w, c) for w in ("record retract", "seed") for c in FEW
]


@pytest.mark.parametrize(("write", "case"), MATRIX, ids=[f"{w}-{c}" for w, c in MATRIX])
def test_a_write_to_a_store_damaged_at_rest_is_refused_with_the_file_untouched(
    tmp_path: Path, seeded: bytes, write: str, case: str
) -> None:
    db, data = damaged(tmp_path, seeded, case)
    code, lines, err, out = uasw(tmp_path, "--db", str(db), *WRITES[write])
    assert_refused_untouched(db, data, code, lines, err, out)


@pytest.mark.parametrize("case", FEW)
def test_serve_refuses_a_store_damaged_at_rest_before_it_listens(
    tmp_path: Path, seeded: bytes, case: str
) -> None:
    db, data = damaged(tmp_path, seeded, case)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    log = tmp_path / "serve.log"
    env = {k: v for k, v in os.environ.items() if k != "UASW_WRITE_TOKEN"}
    with log.open("wb") as stderr:
        proc = subprocess.Popen(
            [*SERVE, "--db", str(db), "serve", "--port", str(port), "--fixtures", str(FIXTURES)],
            cwd=tmp_path,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=stderr,
        )
        try:
            listening = False
            deadline = time.monotonic() + 120
            while proc.poll() is None and time.monotonic() < deadline:
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2):
                        listening = True
                except (urllib.error.URLError, OSError):
                    pass
                if listening:
                    break
                time.sleep(0.1)
        finally:
            if proc.poll() is None:
                proc.terminate()
            proc.wait(timeout=60)
    assert not listening, "the service started on a damaged store"
    err = log.read_text(encoding="utf-8", errors="replace")
    lines = [ln for ln in err.strip().splitlines() if not ln.startswith("{")]
    assert_refused_untouched(db, data, proc.returncode, lines, err)


VERIFIES = [(h, c) for h in (False, True) for c in DAMAGE]


@pytest.mark.parametrize(
    ("head", "case"), VERIFIES, ids=[f"{'head' if h else 'plain'}-{c}" for h, c in VERIFIES]
)
def test_verify_reports_a_store_damaged_at_rest_apart_from_the_chain(
    tmp_path: Path, seeded: bytes, head: bool, case: str
) -> None:
    db, data = damaged(tmp_path, seeded, case)
    args = ["verify", "--head", SEEDED_HEAD] if head else ["verify"]
    code, lines, err, out = uasw(tmp_path, "--db", str(db), *args)
    assert "intact" not in out + err and "unchanged" not in out + err, (out, err)
    assert_refused_untouched(db, data, code, lines, err, out)


def test_a_sound_store_still_verifies_and_takes_writes(tmp_path: Path, seeded: bytes) -> None:
    folder = tmp_path / "store"
    folder.mkdir()
    db = folder / "fleet.sqlite"
    db.write_bytes(seeded)
    code, _, err, out = uasw(tmp_path, "--db", str(db), "verify", "--head", SEEDED_HEAD)
    assert code == 0, err
    assert "unchanged since head" in out
    copy = tmp_path / "copy.sqlite"
    shutil.copy(db, copy)
    code, _, err, _ = uasw(tmp_path, "--db", str(db), *WRITES["record"])
    assert code == 0, err
    code, _, err, _ = uasw(tmp_path, "--db", str(db), *WRITES["ingest"])
    assert code == 0, err
    code, _, err, out = uasw(tmp_path, "--db", str(db), "verify")
    assert code == 0, err
    assert out.startswith("journal intact: 96 links")
