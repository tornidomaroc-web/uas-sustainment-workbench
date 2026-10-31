"""A store's close keeps the `-wal` bounded, and still makes no writer wait (issue #127).

`close` folded the WAL with `PRAGMA wal_checkpoint(PASSIVE)` alone, so a writer never waited
for a close. But `PASSIVE` never starts the WAL over: SQLite starts it over only once the WAL is
folded whole and no read still uses it, and nothing else did. Beside a service that only reads,
with requests always under way, each `uasw record` run beside it added its frames to the
`-wal` and none were ever dropped: it grew about 36 KB a run, to over a megabyte after 40,
until the service stopped (its own checkpoint counts only its own commits). And a store opened
for reads only that was open when the writer closed was then the last connection, which never
checkpoints, so it left a folded `-wal` of the whole store's writes beside the file, and every
read-only open after it kept that file as found, where the close before #127 left it empty.

`close` now folds without waiting first (`PASSIVE`), then starts the WAL over
(`TRUNCATE`) waiting at most `CLOSE_WAIT_MS` for the write lock and for reads of an older state
to end. A `TRUNCATE` that does not get them within that wait changes nothing, so a `-wal`
holding pages a read still needs is never cut, and the next close or the store's own
checkpoint starts it over.

The service, the commands, the writer and the readers run in processes of their own.
Assertions are about statuses, exit codes and the files' bytes, never an operating system's
error text, a path or a line ending.
"""

from __future__ import annotations

import http.client
import os
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

from test_close_without_waiting import child_env, entry, record, seeded, sidecars, whole

from uas_workbench.service.store import Store

RUNS = 16  # `uasw record` runs beside the service
READERS = 4  # threads reading through the service the whole time
# What the `-wal` may hold while the service runs: a few runs' frames. A run adds about 16 KB
# when nothing starts the WAL over, so 16 runs leave about 260 KB there.
WAL_BOUND = 64 * 1024
START_BUDGET_S = 120.0
TIMEOUT_S = 30.0
HOLD_S = 0.05  # each read of the reader without pause, well within `CLOSE_WAIT_MS`

# A reader without pause in a process of its own: read after read, each holding its state for
# `HOLD_S`, the next begun as the last ends, until a line arrives on its stdin. A close that
# folds without waiting nearly always meets one of its reads begun before the last commit.
READING = """
import sqlite3, sys, threading, time
c = sqlite3.connect(sys.argv[1], isolation_level=None)
hold = float(sys.argv[2])
stop = threading.Event()
threading.Thread(None, lambda: (sys.stdin.readline(), stop.set()), daemon=True).start()
print("reading", flush=True)
while not stop.is_set():
    c.execute("BEGIN")
    c.execute("SELECT COUNT(*) FROM entries").fetchone()
    time.sleep(hold)
    c.execute("COMMIT")
c.close()
"""

# A store open in a process of its own, for writing or for reads only. It writes one entry
# (the writer) or reads every entry (the reader), says so, and closes when a line arrives on
# its stdin.
HOLDER = """
import sys
from datetime import UTC, datetime
from uas_workbench.ledger import Entry
from uas_workbench.service.store import Store
path, role = sys.argv[1], sys.argv[2]
store = Store(path, reads_only=role == "reader")
if role == "writer":
    store.append_entry(Entry(
        id=None, subject="SYN-02", kind="work_order.open",
        occurred_utc=datetime(2026, 9, 21, 9, tzinfo=UTC),
        recorded_utc=datetime(2026, 10, 1, 12, tzinfo=UTC),
        entered_by="A. Tester, maintenance", statement="opened beside a reader",
        details={"work_id": "WO-HELD", "state": "deferred"}, supersedes=None, reason=None,
        synthetic=False,
    ))
else:
    store.entries()
print("ready", flush=True)
sys.stdin.readline()
store.close()
"""


class Holder:
    def __init__(self, path: Path, role: str) -> None:
        self.process = subprocess.Popen(
            [sys.executable, "-c", HOLDER, str(path), role],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            env=child_env(),
        )
        assert self.process.stdout is not None
        assert self.process.stdout.readline().strip() == "ready"

    def close(self) -> int:
        if self.process.poll() is None:
            assert self.process.stdin is not None
            self.process.stdin.write("close\n")
            self.process.stdin.flush()
        code = self.process.wait(timeout=60)
        for stream in (self.process.stdin, self.process.stdout):
            if stream is not None:
                stream.close()
        return code


def wal_bytes(path: Path) -> int:
    wal = path.with_name(path.name + "-wal")
    return wal.stat().st_size if wal.exists() else 0


def serve(path: Path) -> tuple[subprocess.Popen[bytes], str, int]:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        host, port = probe.getsockname()[:2]
    uasw = [sys.executable, "-m", "uas_workbench.service.cli", "--db", str(path)]
    server = subprocess.Popen(
        [*uasw, "serve", "--host", str(host), "--port", str(port)],
        env=child_env(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + START_BUDGET_S
    while server.poll() is None:
        conn = http.client.HTTPConnection(host, port, timeout=TIMEOUT_S)
        try:
            conn.request("GET", "/health")
            status = conn.getresponse().status
        except OSError:
            status = 0
        finally:
            conn.close()
        if status == 200:
            return server, host, port
        assert time.monotonic() < deadline, "the service did not start"
        time.sleep(0.05)
    raise AssertionError(f"the service exited with {server.returncode}")


def test_the_wal_stays_bounded_beside_a_service_that_reads_while_commands_write(
    tmp_path: Path,
) -> None:
    path = tmp_path / "fleet.sqlite"
    seeded(path).close()
    server, host, port = serve(path)
    stop = threading.Event()
    statuses: dict[int, int] = {}
    errors: list[str] = []
    count = threading.Lock()

    def read(n: int) -> None:
        conn = http.client.HTTPConnection(host, port, timeout=TIMEOUT_S)
        urls = ["/aircraft", "/entries?subject=SYN-05", "/aircraft/SYN-01/due"]
        i = n
        try:
            while not stop.is_set():
                try:
                    conn.request("GET", urls[i % len(urls)])
                    response = conn.getresponse()
                    response.read()
                    with count:
                        statuses[response.status] = statuses.get(response.status, 0) + 1
                except (OSError, http.client.HTTPException) as exc:
                    errors.append(type(exc).__name__)
                    conn.close()
                i += 1
        finally:
            conn.close()

    threads = [threading.Thread(None, read, None, (n,)) for n in range(READERS)]
    sizes: list[int] = []
    reading = subprocess.Popen(
        [sys.executable, "-c", READING, str(path), str(HOLD_S)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        env=child_env(),
    )
    try:
        assert reading.stdout is not None and reading.stdout.readline().strip() == "reading"
        for thread in threads:
            thread.start()
        time.sleep(0.5)
        for n in range(RUNS):
            command = record(path, f"WO-RUN-{n}")
            out, err = command.communicate(timeout=120)
            assert command.returncode == 0, (out, err)
            sizes.append(wal_bytes(path))
    finally:
        reading.communicate("stop\n", timeout=60)
        stop.set()
        for thread in threads:
            thread.join(60)
        server.terminate()
        server.wait(60)
    assert not errors, errors
    assert set(statuses) == {200}, statuses
    # Bounded: the `-wal` after the last runs holds a few runs' frames at most, where nothing
    # starting it over left every run's frames there.
    assert max(sizes[RUNS // 2 :]) <= WAL_BOUND, f"-wal bytes after each run: {sizes}"
    whole(path, {f"WO-RUN-{n}" for n in range(RUNS)})


def test_a_writer_closing_beside_a_reads_only_store_leaves_no_folded_wal_behind(
    tmp_path: Path,
) -> None:
    """The writer closes while a store opened for reads only, which never checkpoints, is
    open; the reader closes last. The `-wal` left beside the file holds nothing."""
    path = tmp_path / "fleet.sqlite"
    seeded(path).close()
    writer = Holder(path, "writer")  # its entry is in the `-wal` now
    try:
        assert wal_bytes(path) > 0
        reader = Holder(path, "reader")  # opened beside a `-wal` holding that entry
    except BaseException:
        writer.close()
        raise
    try:
        assert writer.close() == 0
    finally:
        assert reader.close() == 0
    assert wal_bytes(path) == 0, f"{wal_bytes(path)} B left in the -wal"
    # A store opened for reads only beside it now keeps nothing.
    again = Store(str(path), reads_only=True)
    try:
        assert "WO-HELD" in {e.details.get("work_id") for e in again.entries()}
    finally:
        again.close()
    assert wal_bytes(path) == 0
    whole(path, {"WO-HELD"})


def test_a_close_whose_truncate_cannot_take_the_write_lock_leaves_the_wal_as_it_was(
    tmp_path: Path,
) -> None:
    """Another process holds the write lock, with a write of its own not yet committed, as the
    store closes: the close's `TRUNCATE` cannot take the lock, gives up within its short wait
    and changes nothing in the `-wal`; the other write then commits, and every write is in
    the store."""
    path = tmp_path / "fleet.sqlite"
    store = seeded(path)
    store.append_entry(entry("SYN-02", "WO-MINE"))
    other = sqlite3.connect(str(path), isolation_level=None)
    try:
        other.execute("BEGIN IMMEDIATE")
        # A write, so the lock is held as a writer's.
        other.execute("UPDATE aircraft SET label = label WHERE key = ?", ("SYN-01",))
        wal = path.with_name(path.name + "-wal")
        before = wal.read_bytes()
        began = time.monotonic()
        store.close()
        taken = time.monotonic() - began
        assert wal.read_bytes() == before
        assert taken < 1.0, f"close took {taken:.2f} s"
        other.execute("COMMIT")
    finally:
        other.close()
    assert sidecars(path) == []
    whole(path, {"WO-MINE"})
    plain = sqlite3.connect(str(path))
    try:
        assert plain.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        plain.close()
    assert os.path.getsize(path) > 0
