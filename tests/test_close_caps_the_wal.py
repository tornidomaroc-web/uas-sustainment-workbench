"""A store's close keeps the `-wal` under a cap beside reads longer than its short wait (#127).

`close` folds the WAL without waiting (`PASSIVE`), then starts it over (`TRUNCATE`) waiting at
most `CLOSE_WAIT_MS` for the write lock and for reads of an older state to end. Beside reads
that each hold their state longer than that wait, one after the other with no gap (a
`GET /journal/verify` on a large store reads for over a second), that `TRUNCATE` never found
the reads ended, so nothing started the WAL over: the `-wal` grew with every command that wrote
beside the service, without bound, until the reads stopped. Once the `-wal` is past
`CLOSE_WAL_CAP` (4 MiB) as the close begins, `close` now tries that short `TRUNCATE` again
after each pause of `CLOSE_GAP_MS`, for up to `CLOSE_TRIES_MS` (3.5 s): each try holds the
write lock for the short wait as SQLite counts it (up to 0.25 s measured on Windows) and the
pause leaves it free, so a write beside it waits about one try and was not refused here, while
the closing command itself waits up to about 3.7 s for the reads of an older state to end.
Under the cap one short try is all it makes. Reads that leave no moment free while the close
tries leave the `-wal` past the cap; the second test pins only that the write is not refused
then, not that the `-wal` is bounded.

The service, the commands and the reader run in processes of their own. A command here sets
the cap low on the module before the real command runs, so a few runs cross it through the same
code the shipped 4 MiB cap runs. Assertions are about statuses, exit codes,
times against the busy timeout and the files' sizes, never an operating system's error text,
a path or a line ending.
"""

from __future__ import annotations

import http.client
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

from test_close_bounds_the_wal import READING, serve, wal_bytes
from test_close_without_waiting import READER, WHO, child_env, seeded, whole

CAP = 48 * 1024  # the cap the commands here run with
# What one `uasw record` run adds to the `-wal` when nothing starts it over: about 16 to 44 KB
# measured, so a `-wal` kept under the cap holds at most the cap and one run more.
ONE_RUN = 64 * 1024
RUNS = 16
HOLD_S = 1.0  # each read without pause holds its state ten times the close's short wait
TIMEOUT_S = 60.0
# A service write sent as a close begins answers within this, far under the 5 s busy timeout
# a close waiting with the lock held made it wait.
WRITE_S = 1.0
# A `uasw` command whose store closes with the cap given as the first argument.
CAPPED = """
import sys
from uas_workbench.service import store
store.CLOSE_WAL_CAP = int(sys.argv[1])
from uas_workbench.service.cli import main
main(sys.argv[2:])
"""


def capped_record(path: Path, work_id: str, cap: int) -> subprocess.Popen[str]:
    """`uasw record` of one work order with the cap set, its stdout a pipe: the line saying
    the entry is recorded arrives before the command closes its store."""
    opened = ["record", "work-order", "open", "SYN-05", "--state", "deferred", "--work-id", work_id]
    return subprocess.Popen(
        [sys.executable, "-u", "-c", CAPPED, str(cap), "--db", str(path), *opened, *WHO],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=child_env(),
    )


def post(host: str, port: int, work_id: str) -> tuple[int, float]:
    """A write through the service, from this machine: its status and how long it took."""
    body = {
        "subject": "SYN-03",
        "kind": "work_order.open",
        "occurred_utc": "2026-09-20T09:00:00Z",
        "entered_by": "A. Tester, maintenance",
        "statement": "work done as described",
        "details": {"work_id": work_id, "state": "deferred"},
    }
    conn = http.client.HTTPConnection(host, port, timeout=TIMEOUT_S)
    began = time.monotonic()
    try:
        conn.request("POST", "/entries", json.dumps(body), {"Content-Type": "application/json"})
        response = conn.getresponse()
        response.read()
        return response.status, time.monotonic() - began
    finally:
        conn.close()


def run_beside(path: Path, host: str, port: int, work_id: str, cap: int) -> tuple[int, float]:
    """One capped `uasw record` run, with a service write sent as its close begins; returns
    the write's status and time once both are done."""
    command = capped_record(path, work_id, cap)
    assert command.stdout is not None
    assert "recorded" in command.stdout.readline()  # written; its close runs now
    answer: list[tuple[int, float]] = []
    write = threading.Thread(None, lambda: answer.append(post(host, port, f"{work_id}-SVC")))
    write.start()
    out, err = command.communicate(timeout=TIMEOUT_S)
    write.join(TIMEOUT_S)
    assert command.returncode == 0, (out, err)
    return answer[0]


def stop_service(server: subprocess.Popen[bytes]) -> None:
    server.terminate()
    server.wait(TIMEOUT_S)


def test_the_wal_stays_under_its_cap_beside_reads_longer_than_the_close_waits(
    tmp_path: Path,
) -> None:
    """A reader in a third process reads without pause, each read holding its state for
    `HOLD_S`; a `uasw record` run and a service write as its close begins, `RUNS` times. The
    `-wal` after each run holds at most the cap and one run's frames, every service write
    answers 201 well within the busy timeout, and the first close once the reads have stopped
    leaves the `-wal` empty."""
    path = tmp_path / "fleet.sqlite"
    seeded(path).close()
    server, host, port = serve(path)
    sizes: list[int] = []
    writes: list[tuple[int, float]] = []
    reading = subprocess.Popen(
        [sys.executable, "-c", READING, str(path), str(HOLD_S)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        env=child_env(),
    )
    try:
        assert reading.stdout is not None and reading.stdout.readline().strip() == "reading"
        for n in range(RUNS):
            writes.append(run_beside(path, host, port, f"WO-RUN-{n}", CAP))
            sizes.append(wal_bytes(path))
        reading.communicate("stop\n", timeout=TIMEOUT_S)
        command = capped_record(path, "WO-AFTER", CAP)
        out, err = command.communicate(timeout=TIMEOUT_S)
        assert command.returncode == 0, (out, err)
        after = wal_bytes(path)
    finally:
        if reading.poll() is None:
            reading.communicate("stop\n", timeout=TIMEOUT_S)
        stop_service(server)
    assert max(sizes) <= CAP + ONE_RUN, f"-wal bytes after each run: {sizes}"
    assert [status for status, _ in writes] == [201] * RUNS, writes
    assert max(taken for _, taken in writes) < WRITE_S, writes
    # The reads have ended: the close starts the WAL over, whatever the reads left before.
    assert after == 0, f"{after} B left in the -wal after the reads stopped"
    whole(
        path,
        {f"WO-RUN-{n}" for n in range(RUNS)}
        | {f"WO-RUN-{n}-SVC" for n in range(RUNS)}
        | {"WO-AFTER"},
    )


def test_a_close_past_the_cap_beside_a_read_that_never_ends_refuses_no_write(
    tmp_path: Path,
) -> None:
    """The cap is 0, so the close keeps trying to start the WAL over, and a reader in a third
    process holds a state older than the command's write for the whole close. The service
    write sent as the close begins still answers 201 in under `WRITE_S`, where a close that
    waited the busy timeout with the lock held refused it 503 or made it wait as long; the close
    gives up after `CLOSE_TRIES_MS`, cuts nothing a read needs, and exits 0."""
    path = tmp_path / "fleet.sqlite"
    seeded(path).close()
    server, host, port = serve(path)
    holder = subprocess.Popen(
        [sys.executable, "-c", READER, str(path), str(TIMEOUT_S)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        env=child_env(),
    )
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "holding"
        began = time.monotonic()
        status, taken = run_beside(path, host, port, "WO-OUT", 0)
        closed = time.monotonic() - began
    finally:
        if holder.poll() is None:
            assert holder.stdin is not None
            holder.stdin.write("done\n")
            holder.stdin.flush()
        holder.communicate(timeout=TIMEOUT_S)
        stop_service(server)
    assert status == 201, (status, f"{taken:.2f} s")
    assert taken < WRITE_S, f"the write took {taken:.2f} s"
    assert closed < 5.0 + 3.0, f"the command took {closed:.2f} s"
    whole(path, {"WO-OUT", "WO-OUT-SVC"})
