"""A store whose side files cannot be written is refused in one line, nothing written (#112).

A store file marked read-only gets an empty `-wal` and a `-shm` from any read, and SQLite cannot
remove them as its last connection closes. With those side files read-only too, SQLite cannot
write the `-shm`, its index of the `-wal`, and keeps that index in the reading process's memory
instead: then every read on any connection is a new state of the store (`PRAGMA data_version`
moves on each one), so the store's snapshot, which stands only when the state's label is the
same before and after its first read, never stood, and every command ended in a
`RuntimeError: the store never stood still long enough to read one state of it` traceback after
1000 tries: the six commands that only read, and `record` and `serve` on such a store too.

Now every command that opens an existing store looks at its `-shm` first, before SQLite opens
anything, and refuses one it cannot write in one `refused (503)` line naming the side files that
cannot be written, exit 1, the three files byte for byte as they were and nothing made beside
them. A read-only `-wal` alone is not refused: SQLite reads a store beside one.

Every command is a real `uasw` process. Assertions are about exit codes, statuses, the files'
bytes and the folder's contents, never an operating system's error text, a path or a line
ending. On POSIX a read-only file is mode 0400 and the tests do not run as root, as on CI.
"""

from __future__ import annotations

import os
import sqlite3
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest
from test_damaged_at_rest import FIXTURES, WHO, uasw

from uas_workbench.service.store import Store

# Each command that opens an existing store, with what it writes where it is told to.
COMMANDS: dict[str, list[str]] = {
    "verify": ["verify"],
    "record history": ["record", "history", "SYN-04"],
    "records": ["records", "SYN-04", "--out", "{out}/records.html"],
    "evidence": ["evidence", "SYN-04", "--out", "{out}/evidence.html"],
    "fleet-records": ["fleet-records", "--out", "{out}/fleet.html"],
    "export-static": ["export-static", "--out", "{out}/site"],
    "record": ["record", "work-order", "open", "SYN-04", "--state", "in_work", *WHO],
}
READ_ONLY = stat.S_IREAD
WRITABLE = stat.S_IREAD | stat.S_IWRITE


@pytest.fixture(scope="module")
def seeded(tmp_path_factory: pytest.TempPathFactory) -> bytes:
    folder = tmp_path_factory.mktemp("seeded")
    db = folder / "fleet.sqlite"
    code, _, err, _ = uasw(folder, "--db", str(db), "seed", "--fixtures", str(FIXTURES))
    assert code == 0, err
    return db.read_bytes()


@pytest.fixture
def read_only_store(tmp_path: Path, seeded: bytes) -> Iterator[Path]:
    """The seeded store marked read-only, read once by `uasw verify` (which leaves the `-wal`
    and `-shm` SQLite cannot remove beside it), then both side files marked read-only too."""
    folder = tmp_path / "store"
    folder.mkdir()
    db = folder / "fleet.sqlite"
    db.write_bytes(seeded)
    os.chmod(db, READ_ONLY)
    try:
        code, _, err, out = uasw(tmp_path, "--db", str(db), "verify")
        assert code == 0 and out.startswith("journal intact: 94 links"), (out, err)
        names = sorted(p.name for p in folder.iterdir())
        assert names == [db.name, f"{db.name}-shm", f"{db.name}-wal"], names
        for side in ("-wal", "-shm"):
            os.chmod(f"{db}{side}", READ_ONLY)
        yield db
    finally:
        for p in folder.iterdir():
            os.chmod(p, WRITABLE)


def snapshot(folder: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted(folder.iterdir())}


@pytest.mark.parametrize("command", list(COMMANDS))
def test_a_store_whose_side_files_are_read_only_is_refused_in_one_line(
    tmp_path: Path, read_only_store: Path, command: str
) -> None:
    db = read_only_store
    out = tmp_path / "out"
    out.mkdir()
    before = snapshot(db.parent)
    args = [a.replace("{out}", str(out)) for a in COMMANDS[command]]
    code, lines, err, stdout = uasw(tmp_path, "--db", str(db), *args)
    assert "Traceback" not in err, err[-600:]
    assert code == 1, (lines, stdout[-300:])
    assert len(lines) == 1 and lines[0].startswith("refused (503): "), lines
    assert f"{db.name}-shm cannot be written" in lines[0], lines
    assert f"nor can {db.name}-wal" in lines[0], lines
    assert "nothing was read from it or written to it" in lines[0], lines
    assert "intact" not in stdout and "SYN-04" not in stdout, stdout[-300:]
    assert list(out.iterdir()) == []
    assert snapshot(db.parent) == before


def test_serve_refuses_a_store_whose_side_files_are_read_only_before_it_listens(
    tmp_path: Path, read_only_store: Path
) -> None:
    db = read_only_store
    before = snapshot(db.parent)
    code, lines, err, _ = uasw(tmp_path, "--db", str(db), "serve", "--port", "1")
    assert "Traceback" not in err, err[-600:]
    assert code == 1 and len(lines) == 1, lines
    assert lines[0].startswith("refused (503): ") and "-shm cannot be written" in lines[0], lines
    assert snapshot(db.parent) == before


def test_a_read_only_wal_beside_a_writable_shm_is_read_as_before(
    tmp_path: Path, read_only_store: Path
) -> None:
    """Only the `-shm` stops a read: SQLite reads a store beside a `-wal` it cannot write."""
    db = read_only_store
    os.chmod(f"{db}-shm", WRITABLE)
    code, _, err, out = uasw(tmp_path, "--db", str(db), "verify")
    assert code == 0, err[-600:]
    assert out.startswith("journal intact: 94 links"), out


def test_the_store_says_which_side_file_stops_a_read_where_it_raised_a_runtime_error(
    read_only_store: Path,
) -> None:
    """Opened directly, past the command line's look: the read is refused as SQLite's own
    refusals are (`sqlite3.OperationalError`, one line in a command, 503 in the service),
    naming the `-shm`, never a `RuntimeError`."""
    store = Store(str(read_only_store), reads_only=True)
    try:
        with pytest.raises(sqlite3.OperationalError, match=r"-shm cannot be written"):
            store.entry_count()
    finally:
        store.close()
