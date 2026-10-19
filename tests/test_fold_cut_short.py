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
"""

from __future__ import annotations

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
