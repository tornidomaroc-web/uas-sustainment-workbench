"""The commands that only read check the file whole, and keep a -wal a hard stop left (#107).

`uasw verify` and every command that writes run SQLite's `PRAGMA integrity_check` on an existing
store file before they use it (#104, #105); the commands that only read did not. On a store
whose flights index had one key edited in place, which SQLite's quicker check passes, `records`,
`evidence`, `fleet-records` and `export-static` wrote their reports and `record history` printed
its history, each exit 0. And a running `uasw serve`, which checks a file changed under it with
that quicker check, answered `GET /journal/verify` with `ok: true` on such a file and accepted a
write into it (201).

A command that only read also closed its store as every command does: with a checkpoint that
folds the `-wal` into the file and removes both side files when it is the last connection. On a
store a hard stop left, `record history` folded the `-wal` into a file the at-rest check refuses,
and did so even when it refused the command itself; the `-wal` as the hard stop left it was gone,
and with it the line that tells damage only in the `-wal` from damage in the file (#108).

Now every command that only reads runs the same check before it reads, and leaves a `-wal` that
holds anything as it found it, whether it answers or refuses; the running service checks a file
changed under it with `integrity_check`, and `GET /journal/verify` runs it before its walk.

Every command is a real `uasw` process, and `uasw serve` logs to a file. Assertions are about
exit codes, statuses, the store's bytes and the folder's contents, never an operating system's
error text, a path or a line ending.
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest
from test_damaged_at_rest import (
    CHECK,
    FIXTURES,
    SERVE,
    edit_index_key,
    overwrite,
    overwrite_a_page_in_wal,
    recorded_then_stopped_hard,
    uasw,
)

from uas_workbench.service.store import BUSY_TIMEOUT_MS

# Each command that only reads, with what it writes where it is told to (None: stdout only).
READS: dict[str, tuple[list[str], str | None]] = {
    "records": (["records", "SYN-04", "--out", "{out}/records.html"], "records.html"),
    "evidence": (["evidence", "SYN-04", "--out", "{out}/evidence.html"], "evidence.html"),
    "fleet-records": (["fleet-records", "--out", "{out}/fleet.html"], "fleet.html"),
    "export-static": (["export-static", "--out", "{out}/site"], "site"),
    "record history": (["record", "history", "SYN-01"], None),
}


def read_command(name: str, out: Path) -> list[str]:
    return [a.replace("{out}", str(out)) for a in READS[name][0]]


@pytest.fixture(scope="module")
def seeded(tmp_path_factory: pytest.TempPathFactory) -> bytes:
    """The bytes of one freshly seeded store, one file with nothing beside it."""
    folder = tmp_path_factory.mktemp("seeded")
    db = folder / "fleet.sqlite"
    code, _, err, _ = uasw(folder, "--db", str(db), "seed", "--fixtures", str(FIXTURES))
    assert code == 0, err
    return db.read_bytes()


@pytest.fixture(scope="module")
def stopped(tmp_path_factory: pytest.TempPathFactory, seeded: bytes) -> list[bytes]:
    """The file, `-wal` and `-shm` of the seeded store with three work orders recorded on
    SYN-01 and then stopped hard: 97 links, three of them only in the `-wal`."""
    folder = tmp_path_factory.mktemp("stopped")
    db = folder / "fleet.sqlite"
    db.write_bytes(seeded)
    recorded_then_stopped_hard(db, 3)
    names = [db.name, f"{db.name}-wal", f"{db.name}-shm"]
    assert sorted(p.name for p in folder.iterdir()) == sorted(names)
    return [(folder / n).read_bytes() for n in names]


@pytest.mark.parametrize("command", list(READS))
def test_a_command_that_only_reads_refuses_a_store_whose_index_key_was_edited(
    tmp_path: Path, seeded: bytes, command: str
) -> None:
    """One key of the flights index edited in place, which `quick_check` passes: refused in
    one line naming SQLite's integrity check, exit 1, nothing written anywhere, the file byte
    for byte as it was and nothing left beside it."""
    folder, out = tmp_path / "store", tmp_path / "out"
    folder.mkdir()
    out.mkdir()
    db = folder / "fleet.sqlite"
    db.write_bytes(seeded)
    data = edit_index_key(seeded, db)
    db.write_bytes(data)
    code, lines, err, stdout = uasw(tmp_path, "--db", str(db), *read_command(command, out))
    assert "Traceback" not in err, err[-600:]
    assert code == 1, (lines, stdout[-300:])
    assert len(lines) == 1 and lines[0].startswith("refused (503): ") and CHECK in lines[0], lines
    assert "SYN-01" not in stdout and "written to" not in stdout, stdout[-300:]
    assert list(out.iterdir()) == []
    assert db.read_bytes() == data
    assert sorted(p.name for p in folder.iterdir()) == [db.name]


STOPPED = [(c, w) for c in [*READS, "verify"] for w in ("sound", "file", "wal")]


@pytest.mark.parametrize(("command", "where"), STOPPED, ids=[f"{c}-{w}" for c, w in STOPPED])
def test_a_command_that_only_reads_leaves_the_wal_a_hard_stop_left_as_it_was(
    tmp_path: Path, stopped: list[bytes], command: str, where: str
) -> None:
    """A store a hard stop left after three writes, sound, damaged in the file, or damaged
    only in its `-wal`. Sound, the command answers from the file and the `-wal` together (the
    three writes included); damaged, it refuses in one line. Either way the file and the
    `-wal` are byte for byte as they were and both side files are still there: `record history`
    folded the `-wal` into the file as it closed, even when it refused the command."""
    folder, out = tmp_path / "store", tmp_path / "out"
    folder.mkdir()
    out.mkdir()
    db = folder / "fleet.sqlite"
    names = [db.name, f"{db.name}-wal", f"{db.name}-shm"]
    for name, data in zip(names, stopped, strict=True):
        (folder / name).write_bytes(data)
    wal = folder / names[1]
    if where == "file":
        db.write_bytes(overwrite(["aircraft"])(db.read_bytes(), db))
    elif where == "wal":
        overwrite_a_page_in_wal(wal, db)
    data, logged = db.read_bytes(), wal.read_bytes()
    args = ["verify"] if command == "verify" else read_command(command, out)
    code, lines, err, stdout = uasw(tmp_path, "--db", str(db), *args)
    assert "Traceback" not in err, err[-600:]
    if where == "sound":
        assert code == 0, (lines, err[-600:])
        if command == "verify":
            assert stdout.startswith("journal intact: 97 links"), stdout
        elif command == "record history":
            assert stdout.count("hand check") == 3, stdout
        else:
            assert READS[command][1] in [p.name for p in out.iterdir()]
    else:
        assert code == 1, (lines, stdout[-300:])
        assert len(lines) == 1 and lines[0].startswith("refused (503): "), lines
        assert CHECK in lines[0], lines
        assert list(out.iterdir()) == []
    assert db.read_bytes() == data
    assert wal.read_bytes() == logged
    assert sorted(p.name for p in folder.iterdir()) == sorted(names)


def test_a_command_that_only_reads_still_leaves_a_sound_closed_store_one_file(
    tmp_path: Path, seeded: bytes
) -> None:
    """With nothing beside the file, nothing is left beside it, and the file is as it was."""
    folder, out = tmp_path / "store", tmp_path / "out"
    folder.mkdir()
    out.mkdir()
    db = folder / "fleet.sqlite"
    db.write_bytes(seeded)
    for command in [*READS, "verify"]:
        args = ["verify"] if command == "verify" else read_command(command, out)
        code, lines, err, _ = uasw(tmp_path, "--db", str(db), *args)
        assert code == 0 and "Traceback" not in err, (command, lines)
        assert sorted(p.name for p in folder.iterdir()) == [db.name], command
        assert db.read_bytes() == seeded, command


def call(base: str, method: str, path: str, body: bytes | None = None) -> tuple[int, object]:
    req = urllib.request.Request(
        base + path,
        data=body,
        method=method,
        headers={"Content-Type": "application/json"} if body else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            status, text = r.status, r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        with e:
            status, text = e.code, e.read().decode("utf-8")
    try:
        return status, json.loads(text)
    except ValueError:
        return status, text


def entry_body() -> bytes:
    return json.dumps(
        {
            "subject": "SYN-03",
            "kind": "time_in_service.set",
            "occurred_utc": "2026-09-01T09:00:00Z",
            "entered_by": "A. Tester, maintenance",
            "statement": "hours read off the aircraft",
            "details": {"before_s": 3600.0},
        }
    ).encode()


def entry_count(db: Path) -> int:
    c = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        return int(c.execute("SELECT COUNT(*) FROM entries").fetchone()[0])
    finally:
        c.close()


@pytest.fixture
def served(tmp_path: Path, seeded: bytes) -> Iterator[tuple[str, Path]]:
    """`uasw serve` on a copy of the seeded store, answering `/health`."""
    db = tmp_path / "fleet.sqlite"
    db.write_bytes(seeded)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = {k: v for k, v in os.environ.items() if k not in ("UASW_WRITE_TOKEN", "UASW_FIXTURES")}
    log = (tmp_path / "serve.log").open("w")
    p = subprocess.Popen(
        [*SERVE, "--db", str(db), "serve", "--port", str(port), "--fixtures", str(FIXTURES)],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=log,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 120
        while True:
            assert p.poll() is None, (tmp_path / "serve.log").read_text()[-600:]
            try:
                urllib.request.urlopen(base + "/health", timeout=2).close()
                break
            except OSError:
                assert time.monotonic() < deadline, "the service never answered"
                time.sleep(0.2)
        yield base, db
    finally:
        p.terminate()
        p.wait(30)
        log.close()


def edit_index_key_in_place(db: Path, keep_stamp: bool) -> None:
    """`edit_index_key` on the file the service holds open, the one byte written in place; with
    `keep_stamp`, the file's time of last change put back, so one `stat` sees nothing change, as
    for an edit landing within the clock tick of the change last seen."""
    before = db.stat()
    data = db.read_bytes()
    edited = edit_index_key(data, db)
    at = next(i for i in range(len(data)) if data[i] != edited[i])
    with db.open("r+b") as f:
        f.seek(at)
        f.write(edited[at : at + 1])
    if keep_stamp:
        os.utime(db, ns=(before.st_atime_ns, before.st_mtime_ns))
        assert db.stat().st_mtime_ns == before.st_mtime_ns


def test_the_service_refuses_reads_and_writes_on_a_file_whose_index_key_was_edited_under_it(
    served: tuple[str, Path],
) -> None:
    """The edit changes the file under the running service, which checks it: `quick_check`
    passed it, so the service answered `GET /journal/verify` ok and wrote the entry into it."""
    base, db = served
    held = entry_count(db)
    assert call(base, "GET", "/journal/verify")[0] == 200
    edit_index_key_in_place(db, keep_stamp=False)
    status, body = call(base, "GET", "/journal/verify")
    assert status == 503, body
    assert isinstance(body, dict) and CHECK in str(body["detail"]), body
    assert call(base, "GET", "/health")[0] == 503
    assert call(base, "POST", "/entries", entry_body())[0] == 503
    assert entry_count(db) == held


def test_journal_verify_checks_the_file_whole_even_when_the_file_looks_unchanged(
    served: tuple[str, Path],
) -> None:
    """An edit one `stat` cannot see: `GET /journal/verify` runs the integrity check itself,
    as `uasw verify` does, and once it has refused the file the service writes nothing to it."""
    base, db = served
    held = entry_count(db)
    assert call(base, "GET", "/journal/verify")[0] == 200
    edit_index_key_in_place(db, keep_stamp=True)
    status, body = call(base, "GET", "/journal/verify")
    assert status == 503, body
    assert isinstance(body, dict) and CHECK in str(body["detail"]), body
    assert call(base, "POST", "/entries", entry_body())[0] == 503
    assert entry_count(db) == held


def test_a_command_that_only_reads_does_not_wait_for_a_write_lock_held_beside_it(
    tmp_path: Path, seeded: bytes
) -> None:
    """Another connection holds the store's write lock (`BEGIN IMMEDIATE`) for the whole run:
    each command that only reads answers without waiting for it. Its check and its reads take
    no write lock; the checkpoint every command ran as it closed waited the busy timeout, 5 s,
    for that lock, and then gave up."""
    folder, out = tmp_path / "store", tmp_path / "out"
    folder.mkdir()
    out.mkdir()
    db = folder / "fleet.sqlite"
    db.write_bytes(seeded)
    holder = sqlite3.connect(db, isolation_level=None)
    try:
        holder.execute("BEGIN IMMEDIATE")
        for command in ["verify", "record history"]:
            args = ["verify"] if command == "verify" else read_command(command, out)
            started = time.monotonic()
            code, lines, err, _ = uasw(tmp_path, "--db", str(db), *args)
            took = time.monotonic() - started
            assert code == 0 and "Traceback" not in err, (command, lines, err[-600:])
            assert took < BUSY_TIMEOUT_MS / 1000, f"{command} took {took:.1f} s"
        holder.execute("ROLLBACK")
    finally:
        holder.close()
