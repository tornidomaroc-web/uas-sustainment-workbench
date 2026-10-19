"""Beside a fold a hard stop cut short, the refusal never names the wrong file (#111).

A store stopped hard while its `-wal` was being folded into the file (a checkpoint) has a file
part-written and a `-wal` whole: the store with its `-wal` reads and verifies, while the file
alone fails SQLite's check until the next write completes the fold. When such a store was also
damaged in its `-wal`, the refusal said "the store file fails SQLite's integrity check", although
the file was only mid-fold and the damage was in the `-wal`: the line that tells the two apart
checked the file alone, which a fold cut short fails whatever the `-wal` holds.

Now, when the file alone fails beside a `-wal` holding committed pages, the at-rest check looks
at those pages in the file: some of them, not all, in the file exactly as the `-wal` holds them
is a fold begun and not finished, and the line says the damage cannot be told to be in the file
or in the `-wal`. None of them (no fold begun) or every one (a fold finished, its frames left in
the `-wal`) leaves the file alone a store of its own, and the line names the file as before.
What it says to do with the three files is unchanged.

The fold is cut short the way SQLite's checkpoint leaves it, deterministically: the latest
committed copy of each page the `-wal` holds is written into the file in ascending page order,
as the checkpoint writes them, and the rest are not. Measured by hand (issue #111), a real
checkpoint of a 187 MiB `-wal` stopped hard at 300 to 900 ms left the file in that state. The
bulk write is plain sqlite3 into an added table, as in the issue: the at-rest check runs before
the store opens, so who wrote the pages does not change the line. Every command is a real `uasw`
process; assertions are about exit codes, statuses, the files' bytes and the line's words.

A checkpoint can also end, finished, partway through the `-wal`: held back by a reader of an
earlier state, SQLite folds only the pages last written by that state, and a page written again
since keeps its older content in the file. The file alone is then part old and part new as
well, and can fail SQLite's check with nothing damaged, while the store with its `-wal` reads
and verifies. Later commits may write the folded pages again, so that none of them is in the
file as its latest copy, only as an older one, which the look took for no fold begun: damage
only in the `-wal` was then named as the file's. Those shapes are made by real SQLite here, in a
process that ends without closing; beside them the line cannot tell where the damage is, and
the cause it gives is true of a fold held back as of one cut short.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import struct
import subprocess
import sys
from pathlib import Path

import pytest
from test_damaged_at_rest import (
    CHECK,
    FIXTURES,
    PATTERN,
    SEEDED_HEAD,
    follow_the_advice,
    overwrite,
    sqlite_says,
    uasw,
)

TOGETHER = "move the file, its -wal and its -shm aside together"
CANNOT_TELL = "cannot be told"
NAMES = ["fleet.sqlite", "fleet.sqlite-wal", "fleet.sqlite-shm"]


def committed_pages(wal: bytes) -> tuple[dict[int, int], int]:
    """Each page the `-wal` holds in a committed frame, with the offset of its latest copy, and
    the page size; frames whose salts are not the header's are not part of the `-wal`."""
    _, _, size, _, salt1, salt2 = struct.unpack(">IIIIII", wal[:24])
    seen: dict[int, int] = {}
    committed: dict[int, int] = {}
    at = 32
    while at + 24 + size <= len(wal):
        page, commit, s1, s2 = struct.unpack(">IIII", wal[at : at + 16])
        if (s1, s2) != (salt1, salt2):
            break
        seen[page] = at + 24
        if commit:
            committed = dict(seen)
        at += 24 + size
    return committed, size


def checksummed(w: bytearray) -> bytes:
    """Every frame's checksum computed again as SQLite does, so SQLite reads the frames as
    committed whatever was changed in them."""
    magic, _, size = struct.unpack(">III", w[:12])
    order = ">" if magic & 1 else "<"

    def checksum(chunk: bytes, s0: int, s1: int) -> tuple[int, int]:
        for i in range(0, len(chunk), 8):
            x0, x1 = struct.unpack(order + "II", chunk[i : i + 8])
            s0 = (s0 + x0 + s1) & 0xFFFFFFFF
            s1 = (s1 + x1 + s0) & 0xFFFFFFFF
        return s0, s1

    s0, s1 = struct.unpack(">II", w[24:32])
    at = 32
    while at + 24 + size <= len(w):
        s0, s1 = checksum(bytes(w[at : at + 8]), s0, s1)
        s0, s1 = checksum(bytes(w[at + 24 : at + 24 + size]), s0, s1)
        w[at + 16 : at + 24] = struct.pack(">II", s0, s1)
        at += 24 + size
    return bytes(w)


@pytest.fixture(scope="module")
def bulk_stopped(tmp_path_factory: pytest.TempPathFactory) -> list[bytes]:
    """The seeded store, then one transaction of 24 rows of 64 KiB into an added table by a
    process that ends without closing: the file as seeded, every new page only in the `-wal`."""
    folder = tmp_path_factory.mktemp("bulk")
    db = folder / NAMES[0]
    code, _, err, _ = uasw(folder, "--db", str(db), "seed", "--fixtures", str(FIXTURES))
    assert code == 0, err
    child = (
        "import os, sqlite3, sys; c = sqlite3.connect(sys.argv[1]); "
        "c.execute('PRAGMA wal_autocheckpoint = 0'); c.execute('CREATE TABLE bulk (b BLOB)'); "
        "c.executemany('INSERT INTO bulk VALUES (?)', [(bytes([n]) * 65536,) for n in range(24)]); "
        "c.commit(); os._exit(0)"
    )
    subprocess.run([sys.executable, "-c", child, str(db)], check=True, timeout=120)
    assert sorted(p.name for p in folder.iterdir()) == sorted(NAMES)
    return [(folder / n).read_bytes() for n in NAMES]


def lay_out(folder: Path, files: list[bytes]) -> tuple[Path, Path]:
    folder.mkdir()
    for name, data in zip(NAMES, files, strict=True):
        (folder / name).write_bytes(data)
    return folder / NAMES[0], folder / NAMES[1]


def fold(db: Path, wal: Path, share: float) -> list[int]:
    """The checkpoint cut short: the first `share` of the pages the `-wal` holds, in ascending
    order, written into the file from their latest committed copy. Returns the pages not
    written yet."""
    logged = wal.read_bytes()
    pages, size = committed_pages(logged)
    order = sorted(pages)
    done = order[: int(len(order) * share)]
    data = bytearray(db.read_bytes())
    for page in done:
        start = (page - 1) * size
        if len(data) < start:
            data.extend(bytes(start - len(data)))
        data[start : start + size] = logged[pages[page] : pages[page] + size]
    db.write_bytes(bytes(data))
    return order[len(done) :]


def roots_after_the_fold(db: Path, wal: Path, scratch: Path) -> set[int]:
    """The b-trees' root pages, read from a copy of the store whose fold completed."""
    scratch.mkdir()
    shutil.copy(db, scratch / db.name)
    shutil.copy(wal, scratch / wal.name)
    c = sqlite3.connect(scratch / db.name)
    try:
        return {int(r) for (r,) in c.execute("SELECT rootpage FROM sqlite_master")} | {1}
    finally:
        c.close()


def damage_in_wal(wal: Path, page: int) -> None:
    """The latest committed copy of `page` in the `-wal` overwritten with the bytes 0 to 255
    repeated, every frame's checksum computed again."""
    w = bytearray(wal.read_bytes())
    pages, size = committed_pages(bytes(w))
    at = pages[page]
    w[at : at + size] = (PATTERN * (size // len(PATTERN) + 1))[:size]
    wal.write_bytes(checksummed(w))


def file_alone(db: Path) -> str:
    c = sqlite3.connect(f"file:{db.as_posix()}?mode=ro&immutable=1", uri=True)
    try:
        return str(c.execute("PRAGMA integrity_check(1)").fetchone()[0])
    except sqlite3.DatabaseError as exc:
        return str(exc)
    finally:
        c.close()


def test_a_store_mid_fold_with_nothing_damaged_still_verifies(
    tmp_path: Path, bulk_stopped: list[bytes]
) -> None:
    """The state the hard stop leaves: the file alone fails, the store with its `-wal` passes
    and is read with the file and the `-wal` as they were."""
    db, wal = lay_out(tmp_path / "store", bulk_stopped)
    fold(db, wal, 0.5)
    assert file_alone(db) != "ok"
    data, logged = db.read_bytes(), wal.read_bytes()
    code, _, err, out = uasw(tmp_path, "--db", str(db), "verify", "--head", SEEDED_HEAD)
    assert code == 0, err[-600:]
    assert out.startswith(f"unchanged since head {SEEDED_HEAD} (link 94 of 94)"), out
    assert db.read_bytes() == data and wal.read_bytes() == logged


@pytest.mark.parametrize("share", [0.25, 0.5, 0.75])
def test_damage_in_the_wal_beside_a_fold_cut_short_is_never_named_as_the_files(
    tmp_path: Path, bulk_stopped: list[bytes], share: float
) -> None:
    db, wal = lay_out(tmp_path / "store", bulk_stopped)
    roots = roots_after_the_fold(db, wal, tmp_path / "folded")
    waiting = fold(db, wal, share)
    damage_in_wal(wal, next(p for p in waiting if p not in roots))
    assert sqlite_says(db, "integrity_check") != "ok"
    assert file_alone(db) != "ok"
    data, logged = db.read_bytes(), wal.read_bytes()
    code, lines, err, out = uasw(tmp_path, "--db", str(db), "verify")
    assert "Traceback" not in err, err[-600:]
    assert code == 1, (lines, out)
    assert len(lines) == 1 and lines[0].startswith("refused (503): ") and CHECK in lines[0], lines
    assert "the store file fails" not in lines[0], lines
    assert "the file alone passes" not in lines[0], lines
    assert CANNOT_TELL in lines[0] and TOGETHER in lines[0], lines
    assert db.read_bytes() == data and wal.read_bytes() == logged
    assert sorted(p.name for p in db.parent.iterdir()) == sorted(NAMES)


def test_damage_in_the_file_beside_a_fold_cut_short_is_never_named_as_the_wals(
    tmp_path: Path, bulk_stopped: list[bytes]
) -> None:
    """The other way round: the `aircraft` pages, which the `-wal` does not hold, overwritten in
    the file mid-fold. The line cannot tell which either, and says so; it never says the damage
    is only in the `-wal`."""
    db, wal = lay_out(tmp_path / "store", bulk_stopped)
    db.write_bytes(overwrite(["aircraft"])(db.read_bytes(), db))
    fold(db, wal, 0.5)
    code, lines, err, out = uasw(tmp_path, "--db", str(db), "verify")
    assert "Traceback" not in err, err[-600:]
    assert code == 1 and len(lines) == 1 and CHECK in lines[0], (lines, out)
    assert "the store's -wal" not in lines[0] and "the file alone passes" not in lines[0], lines
    assert TOGETHER in lines[0], lines


@pytest.mark.parametrize("share", [0.0, 1.0], ids=["no fold begun", "fold finished"])
def test_damage_in_the_file_with_no_fold_cut_short_is_named_as_the_files(
    tmp_path: Path, bulk_stopped: list[bytes], share: float
) -> None:
    """No page of the `-wal` in the file yet, or every one: the file alone is a store of its own,
    and damage it fails on is the file's, named as before."""
    db, wal = lay_out(tmp_path / "store", bulk_stopped)
    db.write_bytes(overwrite(["aircraft"])(db.read_bytes(), db))
    fold(db, wal, share)
    code, lines, err, out = uasw(tmp_path, "--db", str(db), "verify")
    assert "Traceback" not in err, err[-600:]
    assert code == 1 and len(lines) == 1, (lines, out)
    assert "the store file fails SQLite's integrity check" in lines[0], lines
    assert CANNOT_TELL not in lines[0] and TOGETHER in lines[0], lines


def test_doing_what_the_line_says_beside_a_fold_cut_short_gives_back_the_good_copy(
    tmp_path: Path, bulk_stopped: list[bytes], seeded_copy: bytes
) -> None:
    db, wal = lay_out(tmp_path / "store", bulk_stopped)
    roots = roots_after_the_fold(db, wal, tmp_path / "folded")
    waiting = fold(db, wal, 0.5)
    damage_in_wal(wal, next(p for p in waiting if p not in roots))
    code, lines, _, _ = uasw(tmp_path, "--db", str(db), "verify")
    assert code == 1 and len(lines) == 1 and TOGETHER in lines[0], lines
    follow_the_advice(lines[0], db, seeded_copy, tmp_path / "aside")
    code, _, err, out = uasw(tmp_path, "--db", str(db), "verify", "--head", SEEDED_HEAD)
    assert code == 0, err[-600:]
    assert "unchanged since head" in out, out
    assert db.read_bytes() == seeded_copy
    assert sorted(p.name for p in db.parent.iterdir()) == [db.name]


@pytest.fixture(scope="module")
def seeded_copy(tmp_path_factory: pytest.TempPathFactory) -> bytes:
    folder = tmp_path_factory.mktemp("good")
    db = folder / NAMES[0]
    code, _, err, _ = uasw(folder, "--db", str(db), "seed", "--fixtures", str(FIXTURES))
    assert code == 0, err
    return db.read_bytes()


# One process writes into the store, a commit at a time, and ends without closing. A reader
# holds the state after the first commits while more are made, so a PASSIVE checkpoint folds
# the pages last written by that state and stops there, finished.
#   rows: rows of 64 KiB into an added table; "rewritten" then commits new copies of rows the
#     checkpoint folded; "started over" first fills the `-wal` and folds it whole with a RESTART
#     checkpoint, so the next commit writes it again from its first frame under new salts,
#     leaving the old frames past the new ones. The file alone happens to pass.
#   a table: a table made, then a row put in it: page 1, naming the table, is folded and the
#     table's root, written again by the row, is not, so the file alone fails with nothing
#     damaged; "page 1 again" then makes a second table, so page 1 is in the file only as an
#     older copy and no page the `-wal` holds is in the file as its latest.
HELD_BACK = """\
import json, os, sqlite3, sys
shape = sys.argv[2]
w = sqlite3.connect(sys.argv[1], isolation_level=None)
w.execute("PRAGMA wal_autocheckpoint = 0")
def rows(first, count, fill):
    w.execute("BEGIN")
    for k in range(first, first + count):
        w.execute("INSERT OR REPLACE INTO bulk VALUES (?, ?)", (k, bytes([fill + k]) * 65536))
    w.execute("COMMIT")
r = sqlite3.connect(sys.argv[1], isolation_level=None)
if shape.startswith("a table"):
    w.execute("CREATE TABLE more (k INTEGER PRIMARY KEY, v TEXT)")
    r.execute("BEGIN")
    r.execute("SELECT count(*) FROM more").fetchone()
    w.execute("INSERT INTO more VALUES (1, 'one')")
else:
    w.execute("CREATE TABLE bulk (k INTEGER PRIMARY KEY, b BLOB)")
    if shape == "started over":
        rows(100, 60, 0)
        assert w.execute("PRAGMA wal_checkpoint(RESTART)").fetchone()[0] == 0
    rows(0, 12, 0)
    r.execute("BEGIN")
    r.execute("SELECT count(*) FROM bulk").fetchone()
    rows(12, 12, 0)
folded = w.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
r.execute("ROLLBACK")
if shape in ("rewritten", "started over"):
    rows(0, 6, 50)
if shape == "a table, page 1 again":
    w.execute("CREATE TABLE again (k INTEGER PRIMARY KEY)")
print(json.dumps(list(folded)), flush=True)
os._exit(0)
"""
ROWS = ["held back", "rewritten", "started over"]
TABLE = ["a table", "a table, page 1 again"]
HELD = "held back"  # the cause the line gives names a fold held back as well as one cut short
WRONG_CAUSE = "a hard stop cut short the fold"


def held_back(folder: Path, seeded: bytes, shape: str) -> tuple[Path, Path]:
    """The seeded store, then the writes of `shape` with a checkpoint finished partway, stopped
    hard: the file holds some of the `-wal`'s pages as it holds them and not others, and the
    store with its `-wal` verifies unchanged, with the files as they were."""
    folder.mkdir()
    db, wal = folder / NAMES[0], folder / NAMES[1]
    db.write_bytes(seeded)
    run = subprocess.run(
        [sys.executable, "-c", HELD_BACK, str(db), shape],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    busy, logged, folded = json.loads(run.stdout)
    assert busy == 0 and 0 < folded < logged, (busy, logged, folded)
    assert sorted(p.name for p in folder.iterdir()) == sorted(NAMES)
    frames = wal.read_bytes()
    copies, size = committed_copies(frames)
    data = db.read_bytes()

    def held(page: int) -> list[bool]:
        """For each committed copy of `page`, oldest first, whether the file holds it."""
        mine = data[(page - 1) * size : page * size]
        return [mine == frames[at : at + size] for at in copies[page]]

    latest = [p for p in copies if held(p)[-1]]
    if shape == "a table, page 1 again":
        assert latest == [] and any(held(1)), latest
    else:
        assert 0 < len(latest) < len(copies), (len(latest), len(copies))
    if shape == "started over":
        at = 32  # past the frames under the header's salts, the old ones are still there
        while at + 24 + size <= len(frames) and frames[at + 8 : at + 16] == frames[16:24]:
            at += 24 + size
        assert at + 24 + size <= len(frames), "nothing of the -wal before it started over"
    assert (file_alone(db) == "ok") == (shape in ROWS), file_alone(db)
    code, _, err, out = uasw(folder.parent, "--db", str(db), "verify", "--head", SEEDED_HEAD)
    assert code == 0, err[-600:]
    assert out.startswith(f"unchanged since head {SEEDED_HEAD} (link 94 of 94)"), out
    assert db.read_bytes() == data and wal.read_bytes() == frames
    return db, wal


def committed_copies(wal: bytes) -> tuple[dict[int, list[int]], int]:
    """Each page the `-wal` holds in committed frames, with the offsets of all its copies,
    oldest first, and the page size."""
    _, _, size, _, salt1, salt2 = struct.unpack(">IIIIII", wal[:24])
    pending: list[tuple[int, int]] = []
    copies: dict[int, list[int]] = {}
    at = 32
    while at + 24 + size <= len(wal):
        page, commit, s1, s2 = struct.unpack(">IIII", wal[at : at + 16])
        if (s1, s2) != (salt1, salt2):
            break
        pending.append((page, at + 24))
        if commit:
            for held, offset in pending:
                copies.setdefault(held, []).append(offset)
            pending = []
        at += 24 + size
    return copies, size


def fold_rest(db: Path, wal: Path, share: float) -> list[int]:
    """A checkpoint begun again and cut short: of the pages the file does not hold as the
    `-wal`'s latest committed copy, the first `share` in ascending order written from that copy,
    as a checkpoint writes them. Returns the pages not written yet."""
    logged = wal.read_bytes()
    pages, size = committed_pages(logged)
    data = bytearray(db.read_bytes())
    behind = [
        p
        for p in sorted(pages)
        if bytes(data[(p - 1) * size : p * size]) != logged[pages[p] : pages[p] + size]
    ]
    done = behind[: int(len(behind) * share)]
    for page in done:
        start = (page - 1) * size
        if len(data) < start:
            data.extend(bytes(start - len(data)))
        data[start : start + size] = logged[pages[page] : pages[page] + size]
    db.write_bytes(bytes(data))
    return behind[len(done) :]


@pytest.mark.parametrize("shape", TABLE)
def test_a_checkpoint_held_back_can_leave_the_file_alone_failing_with_nothing_damaged(
    tmp_path: Path, seeded_copy: bytes, shape: str
) -> None:
    """Why the file alone says nothing about where damage is beside a checkpoint held back:
    it fails SQLite's check here with nothing damaged, while the store verifies unchanged (the
    premises `held_back` asserts)."""
    db, _ = held_back(tmp_path / "store", seeded_copy, shape)
    assert file_alone(db) != "ok"


def waiting_page(db: Path, wal: Path, shape: str, scratch: Path) -> int:
    """A page the store reads from the `-wal`, its latest copy not in the file: the table's
    root, or a page of the rows that is no b-tree's root."""
    copies, size = committed_copies(wal.read_bytes())
    data, frames = db.read_bytes(), wal.read_bytes()
    waiting = [
        p
        for p in sorted(copies)
        if p != 1
        and data[(p - 1) * size : p * size] != frames[copies[p][-1] : copies[p][-1] + size]
    ]
    if shape in TABLE:
        return waiting[0]
    roots = roots_after_the_fold(db, wal, scratch)
    return next(p for p in waiting if p not in roots)


@pytest.mark.parametrize("shape", ROWS + TABLE)
def test_damage_only_in_the_wal_beside_a_checkpoint_held_back_is_never_named_as_the_files(
    tmp_path: Path, seeded_copy: bytes, shape: str
) -> None:
    """The store's own pages in the `-wal` damaged, the file as the checkpoint left it. Where
    the file alone fails with nothing damaged (a table), the line says it cannot tell, giving a
    cause true of a fold held back; with no page in the file as its latest copy ("page 1
    again"), it named the file. Where the file alone passes (rows), it names the `-wal`."""
    db, wal = held_back(tmp_path / "store", seeded_copy, shape)
    damage_in_wal(wal, waiting_page(db, wal, shape, tmp_path / "folded"))
    assert sqlite_says(db, "integrity_check") != "ok"
    data, logged = db.read_bytes(), wal.read_bytes()
    code, lines, err, out = uasw(tmp_path, "--db", str(db), "verify")
    assert "Traceback" not in err, err[-600:]
    assert code == 1 and len(lines) == 1 and lines[0].startswith("refused (503): "), (lines, out)
    assert CHECK in lines[0] and TOGETHER in lines[0], lines
    assert "the store file fails" not in lines[0], lines
    if shape in ROWS:
        assert "the file alone passes" in lines[0] and CANNOT_TELL not in lines[0], lines
    else:
        assert "the file alone passes" not in lines[0], lines
        assert CANNOT_TELL in lines[0] and HELD in lines[0], lines
        assert WRONG_CAUSE not in lines[0], lines
    assert db.read_bytes() == data and wal.read_bytes() == logged
    assert sorted(p.name for p in db.parent.iterdir()) == sorted(NAMES)


@pytest.mark.parametrize("shape", ROWS)
def test_damage_in_the_file_beside_a_checkpoint_held_back_cannot_be_told(
    tmp_path: Path, seeded_copy: bytes, shape: str
) -> None:
    """The `aircraft` pages, which the `-wal` does not hold, overwritten in the file. The file
    alone passed before, but nothing the check can read says so; the line cannot tell either,
    gives the cause true of a fold held back, and never says the damage is only in the
    `-wal`."""
    db, wal = held_back(tmp_path / "store", seeded_copy, shape)
    db.write_bytes(overwrite(["aircraft"])(db.read_bytes(), db))
    assert file_alone(db) != "ok"
    data, logged = db.read_bytes(), wal.read_bytes()
    code, lines, err, out = uasw(tmp_path, "--db", str(db), "verify")
    assert "Traceback" not in err, err[-600:]
    assert code == 1 and len(lines) == 1 and lines[0].startswith("refused (503): "), (lines, out)
    assert CHECK in lines[0] and "the store's -wal" not in lines[0], lines
    assert "the file alone passes" not in lines[0], lines
    assert CANNOT_TELL in lines[0] and HELD in lines[0] and TOGETHER in lines[0], lines
    assert WRONG_CAUSE not in lines[0], lines
    assert db.read_bytes() == data and wal.read_bytes() == logged
    assert sorted(p.name for p in db.parent.iterdir()) == sorted(NAMES)


@pytest.mark.parametrize("shape", ROWS)
def test_a_fold_cut_short_after_a_checkpoint_held_back_still_cannot_be_told(
    tmp_path: Path, seeded_copy: bytes, shape: str
) -> None:
    """The other way: the next checkpoint begun on top of that state and cut short, with the
    damage only in the `-wal`. The file alone is no longer the store at any commit, and the line
    never names the file."""
    db, wal = held_back(tmp_path / "store", seeded_copy, shape)
    roots = roots_after_the_fold(db, wal, tmp_path / "folded")
    waiting = fold_rest(db, wal, 0.5)
    damage_in_wal(wal, next(p for p in waiting if p not in roots))
    assert file_alone(db) != "ok"
    data, logged = db.read_bytes(), wal.read_bytes()
    code, lines, err, out = uasw(tmp_path, "--db", str(db), "verify")
    assert "Traceback" not in err, err[-600:]
    assert code == 1 and len(lines) == 1 and CHECK in lines[0], (lines, out)
    assert "the store file fails" not in lines[0], lines
    assert "the file alone passes" not in lines[0], lines
    assert CANNOT_TELL in lines[0] and TOGETHER in lines[0], lines
    assert db.read_bytes() == data and wal.read_bytes() == logged
    assert sorted(p.name for p in db.parent.iterdir()) == sorted(NAMES)
