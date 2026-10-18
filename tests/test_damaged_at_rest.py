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
was and nothing left beside it, or, on a store a hard stop left with `-wal` and `-shm`, both
side files still there and the `-wal` as it was. Doing what that line says then gives back the
good copy as it was (#108): the `-wal` belongs to the damaged store, and a good copy put beside
it took its pages in. `integrity_check` rather than `quick_check`, the check the
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


def stopped_hard(db: Path) -> None:
    """The flights index rewritten by a process that then ends without closing, as a hard stop
    leaves a store: its pages only in the `-wal`, with the `-shm` beside it."""
    child = (
        "import os, sqlite3, sys; c = sqlite3.connect(sys.argv[1]); "
        "c.execute('PRAGMA wal_autocheckpoint = 0'); "
        "c.execute('REINDEX sqlite_autoindex_flights_1'); c.commit(); os._exit(0)"
    )
    subprocess.run([sys.executable, "-c", child, str(db)], check=True, timeout=60)


def edit_index_key_in_wal(wal: Path) -> None:
    """`edit_index_key`, in the `-wal`'s copy of the page: the frame's checksum and every later
    one's computed again as SQLite does, so SQLite reads the edited page as committed."""
    w = bytearray(wal.read_bytes())
    magic, _, size = struct.unpack(">III", w[:12])
    order = ">" if magic & 1 else "<"

    def checksum(chunk: bytes, s0: int, s1: int) -> tuple[int, int]:
        for i in range(0, len(chunk), 8):
            x0, x1 = struct.unpack(order + "II", chunk[i : i + 8])
            s0 = (s0 + x0 + s1) & 0xFFFFFFFF
            s1 = (s1 + x1 + s0) & 0xFFFFFFFF
        return s0, s1

    s0, s1 = struct.unpack(">II", w[24:32])
    edited, at = False, 32
    while at + 24 + size <= len(w):
        key = w.find(b"SYN-02SYN-02-", at + 24, at + 24 + size)
        if key >= 0 and not edited:
            w[key + 10] = ord("1")
            edited = True
        s0, s1 = checksum(bytes(w[at : at + 8]), s0, s1)
        s0, s1 = checksum(bytes(w[at + 24 : at + 24 + size]), s0, s1)
        w[at + 16 : at + 24] = struct.pack(">II", s0, s1)
        at += 24 + size
    assert edited, "no SYN-02 key in the -wal"
    wal.write_bytes(bytes(w))


SIDE_FILES = [(w, c) for w in ("record", "ingest", "verify") for c in ("file", "wal")]


@pytest.mark.parametrize(("write", "where"), SIDE_FILES, ids=[f"{w}-{c}" for w, c in SIDE_FILES])
def test_a_store_stopped_hard_and_damaged_is_refused_with_its_side_files_as_they_were(
    tmp_path: Path, seeded: bytes, write: str, where: str
) -> None:
    """A store a hard stop left with `-wal` and `-shm`, damaged in the file or only in the
    `-wal`: refused, with the file and the `-wal` byte for byte as they were and both side files
    still there. SQLite folds the WAL into the file and removes the side files when its last
    connection closes; the check's connection was that one on a refusal, so it wrote to the
    damaged file and took away the `-wal` a hard stop left."""
    folder = tmp_path / "store"
    folder.mkdir()
    db = folder / "fleet.sqlite"
    db.write_bytes(seeded)
    stopped_hard(db)
    wal = folder / "fleet.sqlite-wal"
    assert sorted(p.name for p in folder.iterdir()) == [db.name, f"{db.name}-shm", wal.name]
    if where == "file":
        db.write_bytes(overwrite(["aircraft"])(db.read_bytes(), db))
    else:
        edit_index_key_in_wal(wal)
    data, logged = db.read_bytes(), wal.read_bytes()
    args = ["verify"] if write == "verify" else WRITES[write]
    code, lines, err, out = uasw(tmp_path, "--db", str(db), *args)
    assert "Traceback" not in err, err[-600:]
    assert code == 1, (lines, out)
    assert len(lines) == 1 and lines[0].startswith("refused (503): ") and CHECK in lines[0], lines
    assert db.read_bytes() == data
    assert wal.read_bytes() == logged
    assert sorted(p.name for p in folder.iterdir()) == [db.name, f"{db.name}-shm", wal.name]


def recorded_then_stopped_hard(db: Path, entries: int) -> None:
    """`entries` work orders recorded by `uasw record`'s own code in one process that then ends
    without closing the store, as a hard stop leaves one: the entries only in the `-wal`, with
    the `-shm` beside it."""
    child = (
        "import os, sys; from uas_workbench.service import cli; "
        "from uas_workbench.service.store import Store; "
        "Store.close = lambda self: None; "
        "[cli.main(['--db', sys.argv[1], 'record', 'work-order', 'open', 'SYN-01', "
        "'--state', 'deferred', '--work-id', f'WO-H{n}', '--by', 'A. Tester, maintenance', "
        "'--statement', f'hand check {n}']) for n in range(int(sys.argv[2]))]; os._exit(0)"
    )
    subprocess.run([sys.executable, "-c", child, str(db), str(entries)], check=True, timeout=120)


@pytest.fixture(scope="module")
def stopped_with_writes(tmp_path_factory: pytest.TempPathFactory, seeded: bytes) -> list[bytes]:
    """The file, `-wal` and `-shm` of the seeded store with three entries recorded and then
    stopped hard: 97 links, three of them only in the `-wal`."""
    folder = tmp_path_factory.mktemp("stopped")
    db = folder / "fleet.sqlite"
    db.write_bytes(seeded)
    recorded_then_stopped_hard(db, 3)
    names = [db.name, f"{db.name}-wal", f"{db.name}-shm"]
    assert sorted(p.name for p in folder.iterdir()) == sorted(names)
    return [(folder / n).read_bytes() for n in names]


@pytest.fixture(scope="module")
def good_copy(tmp_path_factory: pytest.TempPathFactory, seeded: bytes) -> bytes:
    """A good copy taken apart from that store: the seeded store and one entry of its own,
    closed, nothing beside it; 61 entries, 95 links."""
    folder = tmp_path_factory.mktemp("good")
    db = folder / "fleet.sqlite"
    db.write_bytes(seeded)
    code, _, err, _ = uasw(folder, "--db", str(db), *WRITES["record"])
    assert code == 0, err
    code, _, err, out = uasw(folder, "--db", str(db), "verify")
    assert code == 0 and out.startswith("journal intact: 95 links"), (out, err)
    assert sorted(p.name for p in folder.iterdir()) == ["fleet.sqlite"]
    return db.read_bytes()


def overwrite_a_page_in_wal(wal: Path, db: Path) -> None:
    """The last frame of one page that is not a b-tree's root, overwritten in the `-wal` with
    the bytes 0 to 255 repeated, every frame's checksum computed again as SQLite does, so SQLite
    reads the damaged page as committed while the file alone is sound."""
    c = sqlite3.connect(f"file:{db.as_posix()}?mode=ro&immutable=1", uri=True)
    try:
        roots = {int(r) for (r,) in c.execute("SELECT rootpage FROM sqlite_master")} | {1}
    finally:
        c.close()
    w = bytearray(wal.read_bytes())
    magic, _, size = struct.unpack(">III", w[:12])
    order = ">" if magic & 1 else "<"
    frames = list(range(32, len(w) - 24 - size + 1, 24 + size))
    at = next(f for f in reversed(frames) if struct.unpack(">I", w[f : f + 4])[0] not in roots)
    w[at + 24 : at + 24 + size] = (PATTERN * (size // len(PATTERN) + 1))[:size]

    def checksum(chunk: bytes, s0: int, s1: int) -> tuple[int, int]:
        for i in range(0, len(chunk), 8):
            x0, x1 = struct.unpack(order + "II", chunk[i : i + 8])
            s0 = (s0 + x0 + s1) & 0xFFFFFFFF
            s1 = (s1 + x1 + s0) & 0xFFFFFFFF
        return s0, s1

    s0, s1 = struct.unpack(">II", w[24:32])
    for f in frames:
        s0, s1 = checksum(bytes(w[f : f + 8]), s0, s1)
        s0, s1 = checksum(bytes(w[f + 24 : f + 24 + size]), s0, s1)
        w[f + 16 : f + 24] = struct.pack(">II", s0, s1)
    wal.write_bytes(bytes(w))


def follow_the_advice(line: str, db: Path, good: bytes, aside: Path) -> None:
    """What the refusal line says to do with a good copy: the file moved aside, with its `-wal`
    and `-shm` when the line says so, else with them left where they are; then the good copy
    put where the file was."""
    aside.mkdir()
    together = "move the file, its -wal and its -shm aside together" in line
    for name in [db.name, f"{db.name}-wal", f"{db.name}-shm"] if together else [db.name]:
        shutil.move(db.parent / name, aside / name)
    db.write_bytes(good)


RESTORES = [(w, c) for w in ("record", "verify") for c in ("file", "wal")]


@pytest.mark.parametrize(("write", "where"), RESTORES, ids=[f"{w}-{c}" for w, c in RESTORES])
def test_doing_what_the_refusal_says_gives_back_the_good_copy_as_it_was(
    tmp_path: Path, stopped_with_writes: list[bytes], good_copy: bytes, write: str, where: str
) -> None:
    """A store a hard stop left after three writes, damaged in the file or only in the `-wal`,
    then restored as its refusal line says. The line said to put a good copy in place of the
    file; the `-wal` the refusal kept stayed beside it, SQLite took its pages into the good
    copy, and `verify` called the result intact at 97 links, the good copy's own entry gone
    (#108). The line now says the side files go aside with the file, and says where the damage
    is: in the file, or only in the `-wal` with the file alone passing."""
    folder = tmp_path / "store"
    folder.mkdir()
    db = folder / "fleet.sqlite"
    names = [db.name, f"{db.name}-wal", f"{db.name}-shm"]
    for name, data in zip(names, stopped_with_writes, strict=True):
        (folder / name).write_bytes(data)
    wal = folder / names[1]
    if where == "file":
        db.write_bytes(overwrite(["aircraft"])(db.read_bytes(), db))
    else:
        overwrite_a_page_in_wal(wal, db)
        assert sqlite_says(db, "integrity_check") != "ok"
    data, logged = db.read_bytes(), wal.read_bytes()
    args = ["verify"] if write == "verify" else WRITES[write]
    code, lines, err, out = uasw(tmp_path, "--db", str(db), *args)
    assert "Traceback" not in err, err[-600:]
    assert code == 1, (lines, out)
    assert len(lines) == 1 and lines[0].startswith("refused (503): ") and CHECK in lines[0], lines
    assert db.read_bytes() == data and wal.read_bytes() == logged
    assert sorted(p.name for p in folder.iterdir()) == sorted(names)
    if where == "file":
        assert "the store file fails SQLite's integrity check" in lines[0], lines
    else:
        assert "the store's -wal" in lines[0] and "the file alone passes it" in lines[0], lines

    aside = tmp_path / "aside"
    follow_the_advice(lines[0], db, good_copy, aside)
    code, _, err, out = uasw(tmp_path, "--db", str(db), "verify")
    assert code == 0, err
    assert out.startswith("journal intact: 95 links"), out
    assert db.read_bytes() == good_copy
    assert sorted(p.name for p in folder.iterdir()) == ["fleet.sqlite"]
    # The damaged store, moved aside whole, is as the refusal left it.
    assert (aside / names[0]).read_bytes() == data and (aside / names[1]).read_bytes() == logged


@pytest.mark.parametrize("wal_bytes", [0, 32], ids=["empty", "header only"])
def test_a_wal_holding_no_frames_beside_a_damaged_file(
    tmp_path: Path, seeded: bytes, stopped_with_writes: list[bytes], wal_bytes: int
) -> None:
    """An empty `-wal` is removed with its `-shm` when the check closes; a `-wal` holding
    anything, its 32-byte header alone included, is kept as it was."""
    folder = tmp_path / "store"
    folder.mkdir()
    db = folder / "fleet.sqlite"
    db.write_bytes(seeded)
    db.write_bytes(overwrite(["aircraft"])(seeded, db))
    wal = folder / f"{db.name}-wal"
    wal.write_bytes(stopped_with_writes[1][:wal_bytes])
    (folder / f"{db.name}-shm").write_bytes(stopped_with_writes[2])
    data = db.read_bytes()
    code, lines, err, _ = uasw(tmp_path, "--db", str(db), *WRITES["record"])
    assert "Traceback" not in err, err[-600:]
    assert code == 1 and len(lines) == 1 and CHECK in lines[0], lines
    assert db.read_bytes() == data
    if wal_bytes:
        assert wal.read_bytes() == stopped_with_writes[1][:wal_bytes]
        assert sorted(p.name for p in folder.iterdir()) == [
            db.name,
            f"{db.name}-shm",
            wal.name,
        ]
    else:
        assert sorted(p.name for p in folder.iterdir()) == [db.name]


def test_a_store_file_of_2_gib_is_told_apart_from_its_wal_in_one_line(
    tmp_path: Path, stopped_with_writes: list[bytes]
) -> None:
    """The file alone is checked where it lies, not from a copy in memory: the copy took twice
    the file's size and, at 2 GiB or more, past what SQLite allocates at once, ended `verify` in
    a MemoryError traceback (measured by hand on a real store of 2113 MiB). Here the store a hard
    stop left, damaged only in its `-wal`, is grown to 2 GiB by bytes past the pages its header
    counts, which SQLite does not read."""
    folder = tmp_path / "store"
    folder.mkdir()
    db = folder / "fleet.sqlite"
    names = [db.name, f"{db.name}-wal", f"{db.name}-shm"]
    for name, data in zip(names, stopped_with_writes, strict=True):
        (folder / name).write_bytes(data)
    wal = folder / names[1]
    overwrite_a_page_in_wal(wal, db)
    pages = db.read_bytes()
    with db.open("r+b") as f:
        f.truncate(2**31)
    logged = wal.read_bytes()
    code, lines, err, _ = uasw(tmp_path, "--db", str(db), "verify")
    assert "Traceback" not in err, err[-600:]
    assert code == 1 and len(lines) == 1 and CHECK in lines[0], lines
    assert "the store's -wal" in lines[0] and "the file alone passes it" in lines[0], lines
    assert db.stat().st_size == 2**31 and wal.read_bytes() == logged
    with db.open("rb") as f:
        assert f.read(len(pages)) == pages
    assert sorted(p.name for p in folder.iterdir()) == sorted(names)


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
