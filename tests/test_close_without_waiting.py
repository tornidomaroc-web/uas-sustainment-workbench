"""`Store.close()` makes no writer wait and refuses none (issue #127).

`close` folded the WAL into the file with `PRAGMA wal_checkpoint(TRUNCATE)`, which takes the
WAL's write lock and holds it while it waits, up to the busy timeout, for a read of an older
state to end. A write through the service that asked for that lock meanwhile waited its own
busy timeout, the same length, and when its wait ended first it was refused 503 with "another
write held the store for longer than this one waits" when no write held it: the connection
closing was any `uasw` command that writes, the older read any request still building its
answer. Now `close` folds what it can without waiting (`PASSIVE`), which takes no write lock
and invokes no busy handler; the store's last connection folds the rest as it closes, so a
closed store is one file as before, and beside another connection the fold is left to that
connection's own checkpoint. A writer can still be refused while another store's own
checkpoint (`_checkpoint`, every 200 of its commits and after each batch) waits for an older
read with the lock held; that is not a write, so the refusal names what held it: the store's
write lock, by a write or by a checkpoint waiting for a read of an older state to end.

The reader and the command run in processes of their own; the service is served in this
process. Assertions are about statuses, times against the busy timeout, exit codes and the
files' bytes, never an operating system's error text, a path or a line ending.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from uas_workbench.fleet import load_config
from uas_workbench.fleet.synthetic import generate
from uas_workbench.ledger import Entry
from uas_workbench.service import store as store_module
from uas_workbench.service.app import create_app
from uas_workbench.service.store import Store

CONFIG = load_config()
TOKEN = "t"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
WAIT_MS = 1500  # the busy timeout of the store under test, so a wait's end is in the test
HOLD_S = 60.0  # the most a reader holds its state; it is released as soon as the test is done
WHO = ["--by", "A. Tester, maintenance", "--statement", "work done as described"]
# What a writer refused for the lock is told: the lock, not a write, since a checkpoint
# waiting for an older read holds it too.
BUSY = (
    "another connection held the store's write lock for longer than this write waits, a write "
    "or a checkpoint waiting for a read of an older state to end; nothing was written, so send "
    "it again"
)
# A reader in a process of its own, on a state older than every commit made after "holding":
# it keeps that state until a line arrives on its stdin, or `HOLD_S` passes.
READER = """
import sqlite3, sys, threading
c = sqlite3.connect(sys.argv[1])
c.execute("BEGIN")
c.execute("SELECT COUNT(*) FROM entries").fetchone()
print("holding", flush=True)
released = threading.Event()
threading.Thread(target=lambda: (sys.stdin.readline(), released.set()), daemon=True).start()
released.wait(float(sys.argv[2]))
c.rollback()
c.close()
"""
# A `uasw` command that waits one second for the write lock, in a process of its own.
SHORT_WAIT = """
import sys
from uas_workbench.service import store
store.BUSY_TIMEOUT_MS = 1000
from uas_workbench.service.cli import main
main(sys.argv[1:])
"""


def child_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k != "UASW_WRITE_TOKEN"}


class Reader:
    """A read of the store's state as it is now, held in a process of its own."""

    def __init__(self, path: Path) -> None:
        self.process = subprocess.Popen(
            [sys.executable, "-c", READER, str(path), str(HOLD_S)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            env=child_env(),
        )
        assert self.process.stdout is not None
        assert self.process.stdout.readline().strip() == "holding"

    def release(self) -> None:
        if self.process.poll() is None:
            assert self.process.stdin is not None
            self.process.stdin.write("done\n")
            self.process.stdin.flush()
        self.process.wait(timeout=60)
        for stream in (self.process.stdin, self.process.stdout):
            if stream is not None:
                stream.close()


def seeded(path: Path) -> Store:
    store = Store(str(path))
    store.add_fleet(generate(CONFIG))
    return store


def entry(subject: str, work_id: str) -> Entry:
    return Entry(
        id=None,
        subject=subject,
        kind="work_order.open",
        occurred_utc=datetime(2026, 9, 21, 9, tzinfo=UTC),
        recorded_utc=datetime(2026, 10, 1, 12, tzinfo=UTC),
        entered_by="A. Tester, maintenance",
        statement="opened beside a reader",
        details={"work_id": work_id, "state": "deferred"},
        supersedes=None,
        reason=None,
        synthetic=False,
    )


def body(work_id: str) -> dict[str, Any]:
    return {
        "subject": "SYN-03",
        "kind": "work_order.open",
        "occurred_utc": "2026-09-20T09:00:00Z",
        "entered_by": "A. Tester, maintenance",
        "statement": "work done as described",
        "details": {"work_id": work_id, "state": "deferred"},
    }


def record(path: Path, work_id: str, *, wait_short: bool = False) -> subprocess.Popen[str]:
    """`uasw record` of one work order, in a process of its own, its stdout a pipe: the line
    saying the entry is recorded arrives before the command closes its store."""
    uasw = ["-c", SHORT_WAIT] if wait_short else ["-m", "uas_workbench.service.cli"]
    opened = ["record", "work-order", "open", "SYN-05", "--state", "deferred", "--work-id", work_id]
    return subprocess.Popen(
        [sys.executable, "-u", *uasw, "--db", str(path), *opened, *WHO],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=child_env(),
    )


def sidecars(path: Path) -> list[str]:
    return sorted(p.name[len(path.name) :] for p in path.parent.iterdir() if p.name != path.name)


def whole(path: Path, work_ids: set[str]) -> None:
    """Every write answered or exited as recorded is in the store, the journal intact."""
    fresh = Store(str(path))
    try:
        found = {e.details.get("work_id") for e in fresh.entries()}
        assert work_ids <= found, (work_ids - found, found)
        assert fresh.verify_journal().ok
    finally:
        fresh.close()


def test_a_write_through_the_service_is_not_refused_while_another_connections_close_waits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The service waits `WAIT_MS` for the lock; the command closing beside it keeps the 5 s
    default, so on the code before this its close held the lock past the service write's whole
    wait, every time: the service write was refused 503, nothing written, where nothing but a
    checkpoint waiting for a read stood in its way."""
    monkeypatch.setattr(store_module, "BUSY_TIMEOUT_MS", WAIT_MS)
    path = tmp_path / "fleet.sqlite"
    store = seeded(path)
    client = TestClient(create_app(store, write_token=TOKEN))
    reader = Reader(path)
    try:
        command = record(path, "WO-OUT")
        assert command.stdout is not None
        assert "recorded" in command.stdout.readline()  # written; its close runs now
        time.sleep(0.3)
        began = time.monotonic()
        answer = client.post("/entries", json=body("WO-SERVICE"), headers=AUTH)
        taken = time.monotonic() - began
        _, err = command.communicate(timeout=60)
    finally:
        reader.release()
    assert answer.status_code == 201, (answer.status_code, answer.json(), f"{taken:.2f} s")
    # It waited for nothing: well within its own busy timeout.
    assert taken < WAIT_MS / 1000 / 2, f"the write took {taken:.2f} s"
    assert command.returncode == 0, err
    store.close()
    whole(path, {"WO-OUT", "WO-SERVICE"})


def test_close_beside_a_reader_of_an_older_state_waits_for_nothing_and_leaves_the_wal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `-wal` holding pages a reader still needs is neither truncated nor deleted by a close
    that is not the last connection's; the fold is left to the connections that stay open.
    Once the last connection closes, the file alone holds every write and the side files go."""
    monkeypatch.setattr(store_module, "BUSY_TIMEOUT_MS", WAIT_MS)
    path = tmp_path / "fleet.sqlite"
    store = seeded(path)
    reader = Reader(path)
    try:
        store.append_entry(entry("SYN-02", "WO-AFTER"))  # the reader's state is older now
        wal = path.with_name(path.name + "-wal")
        wal_before = wal.read_bytes()
        began = time.monotonic()
        store.close()
        taken = time.monotonic() - began
        assert taken < WAIT_MS / 1000 / 2, f"close took {taken:.2f} s"
        assert sidecars(path) == ["-shm", "-wal"]
        assert wal.read_bytes() == wal_before
    finally:
        reader.release()
    # The reader was the last connection: SQLite folded the rest and removed both side files.
    assert sidecars(path) == []
    plain = sqlite3.connect(str(path))
    try:
        assert plain.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert plain.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        plain.close()
    whole(path, {"WO-AFTER"})
    assert sidecars(path) == []


def test_a_writer_refused_while_another_stores_checkpoint_waits_is_told_what_held_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal left: the store's own checkpoint (every `CHECKPOINT_COMMITS` commits) holds
    the write lock while it waits, the 5 s default here, for a read of an older state; a
    `uasw record` beside it that waits one second is refused with nothing written, and told
    that the lock was held, by a write or by such a checkpoint, not that a write held it."""
    monkeypatch.setattr(store_module, "CHECKPOINT_COMMITS", 5)
    path = tmp_path / "fleet.sqlite"
    store = seeded(path)
    with store.batch():  # nothing written; it checkpoints, so the count starts at 0
        pass
    for n in range(4):
        store.append_entry(entry("SYN-02", f"WO-{n}"))
    reader = Reader(path)
    fifth = threading.Thread(None, lambda: store.append_entry(entry("SYN-02", "WO-4")))
    try:
        fifth.start()  # its checkpoint is waiting for the reader by now
        time.sleep(0.3)
        command = record(path, "WO-OUT", wait_short=True)
        out, err = command.communicate(timeout=60)
    finally:
        fifth.join(60)
        reader.release()
    assert command.returncode == 1, (out, err)
    said = [line for line in err.splitlines() if not line.startswith("{")]
    assert said == [f"refused (503): {BUSY}"]
    store.close()
    fresh = Store(str(path))
    try:
        assert "WO-OUT" not in {e.details.get("work_id") for e in fresh.entries()}
    finally:
        fresh.close()
    whole(path, {f"WO-{n}" for n in range(5)})
