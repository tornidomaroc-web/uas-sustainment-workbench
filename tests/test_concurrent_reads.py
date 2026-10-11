"""Concurrent reads through a live service each answer with their own rows (issue #60).

Every request thread read through the store's one SQLite connection with nothing serialising
the reads, and Python's `sqlite3` keeps one statement cache per connection: two threads running
the same SQL stepped the same prepared statement and read each other's rows. A GET could then
answer 200 with the body of another query, or 500. Measured on main: 0 wrong of 2,394 with one
reader; 21 wrong and 41 errors with two; 143 and 205 with four; 114 and 221 with eight.

The check is through a real server, `uasw serve` in a process of its own on a loopback port,
never the single-threaded `TestClient`: N reader threads loop over several routes for every
aircraft but one, each on one kept-alive connection of its own, each body is compared with the
single-threaded answer for the same URL, and a writer posts entries about the one aircraft they
never read meanwhile, on one connection of its own, so no answer they compare changes.
Required: zero differing 200s, zero 5xx, zero failed requests, every write stored, and no more
sockets opened than readers + 3 (issue #62: one socket per request ran a machine out of local
ports mid-suite, with nothing wrong in the service). No assertion names an OS: no error text,
no path separator.
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from uas_workbench.service.cli import seed
from uas_workbench.service.store import Store

FIXTURES = Path(__file__).parent / "fixtures"
AS_OF = "2026-10-01T00:00:00Z"  # pinned, so a status computed as of now cannot move mid-test
WRITTEN = "SYN-02"  # the writer's aircraft; readers never ask about it
REQUESTS_PER_READER = 200
# The writer's pace: one entry every so often, as a person or an ingest would, not a loop
# that takes every turn the readers' threads could have had.
WRITE_EVERY_S = 0.02
TIMEOUT_S = 30.0
START_BUDGET_S = 120.0  # wall clock, not a count of polls: a loaded machine starts it slowly
# uvicorn closes a kept-alive connection left idle for 5 s; a connection idle this long may
# have been closed, and one that was not idle was not.
IDLE_CLOSE_S = 4.0


class Connection(http.client.HTTPConnection):
    """A connection to the service that counts the sockets the test opens through it. The
    count is the test's measure of its own footprint on the machine: a run that opens a
    socket per request can run out of local ports where a port closed stays reserved for a
    while, and that fails the test with nothing wrong in the service."""

    opened = 0
    _count = threading.Lock()
    answered_at: float | None = None  # when the last response on this connection was read

    def connect(self) -> None:
        super().connect()
        with Connection._count:
            Connection.opened += 1

    def idle_s(self) -> float:
        return 0.0 if self.answered_at is None else time.monotonic() - self.answered_at


@dataclass
class Tally:
    identical: int = 0
    differing: int = 0  # 200 with a body that is not the single-threaded answer
    failed: int = 0  # any status that is not 200
    errors: int = 0  # the request did not complete at all
    reopened: int = 0  # the server had closed the idle connection; retried once on a new one
    statuses: dict[int, int] = field(default_factory=dict)
    examples: list[str] = field(default_factory=list)

    def line(self, readers: int) -> str:
        return (
            f"{readers} readers: {self.identical} identical, {self.differing} differing 200s, "
            f"{self.failed} non-200 {dict(sorted(self.statuses.items()))}, "
            f"{self.errors} errors, {self.reopened} reopened"
            + ("; e.g. " + "; ".join(self.examples[:3]) if self.examples else "")
        )


@pytest.fixture(scope="module")
def service(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[str, int]]:
    """`uasw serve` on a seeded file store, in a process of its own, on a free loopback port.

    A process, not a thread: the readers here and the service's own threads would otherwise
    share one interpreter lock, and the test would measure that rather than the store."""
    path = str(tmp_path_factory.mktemp("store") / "fleet.sqlite")
    store = Store(path)
    seed(store, FIXTURES)
    store.close()
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        host, port = probe.getsockname()[:2]
    env = {k: v for k, v in os.environ.items() if k != "UASW_WRITE_TOKEN"}
    # A user on this machine: with no write token set, loopback may write.
    server = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uas_workbench.service.cli",
            "--db",
            path,
            "serve",
            "--host",
            host,
            "--port",
            str(port),
        ],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,  # one JSON line per request; a pipe left unread would fill
    )
    try:
        deadline = time.monotonic() + START_BUDGET_S
        while server.poll() is None:
            health = Connection(host, port, timeout=TIMEOUT_S)
            try:
                status, _ = get(health, "/health")
            except OSError:
                status = 0
            finally:
                health.close()
            if status == 200:
                break
            assert time.monotonic() < deadline, "the service did not start"
            time.sleep(0.05)
        assert server.poll() is None, f"the service exited with {server.returncode}"
        yield host, port
    finally:
        server.terminate()
        server.wait(30)


def get(conn: Connection, url: str) -> tuple[int, bytes]:
    """One request on a kept-alive connection; the connection opens its socket on the first
    request and keeps it for the next. Measured here against `uasw serve` on loopback, 200
    GETs took 0.70 s kept alive and 0.85 s on a connection per request, so nothing on the
    keep-alive path waits on the network."""
    conn.request("GET", url)
    response = conn.getresponse()
    body = response.read()
    conn.answered_at = time.monotonic()
    return response.status, body


def urls_to_read(conn: Connection) -> list[str]:
    """Several routes for every aircraft the writer never touches."""
    status, body = get(conn, "/aircraft")
    assert status == 200
    keys = [a["key"] for a in json.loads(body) if a["key"] != WRITTEN]
    assert len(keys) >= 4
    return [
        url
        for key in keys
        for url in (
            f"/aircraft/{key}?as_of={AS_OF}",
            f"/aircraft/{key}/flights",
            f"/aircraft/{key}/due?as_of={AS_OF}",
            f"/aircraft/{key}/reconcile",
            f"/entries?subject={key}",
        )
    ]


def single_threaded(conn: Connection, urls: list[str]) -> dict[str, bytes]:
    """The answer for every URL with one reader and nothing else going on: twice, so the
    comparison below is known to be against a deterministic answer."""
    first = {}
    for url in urls:
        status, body = get(conn, url)
        assert status == 200, (url, status, body[:200])
        first[url] = body
    for url in urls:
        assert get(conn, url) == (200, first[url]), f"{url} is not deterministic alone"
    return first


def get_reopening(conn: Connection, url: str, out: Tally) -> tuple[int, bytes]:
    """`get`, made again once on a new connection when this one had been idle long enough for
    the server to close it and the request failed as a closed connection does; any other
    failure is raised."""
    idle = conn.idle_s()
    try:
        return get(conn, url)
    except ConnectionError:
        if idle < IDLE_CLOSE_S:
            raise
        conn.close()
        out.reopened += 1
        return get(conn, url)


def read(
    service: tuple[str, int], urls: list[str], expected: dict[str, bytes], offset: int, out: Tally
) -> None:
    """One reader: REQUESTS_PER_READER requests round-robin over the URLs, on one connection
    of its own. A server closes a kept-alive connection it finds idle (uvicorn after 5 s),
    which a client learns of only when its next request fails: `RemoteDisconnected` on Linux,
    `ConnectionAbortedError` on Windows. When the connection had been idle that long, that one
    request is made again on a new connection, once, as every client library does. Any other
    failure, a connection dropped while in use included, is an error, and closes the
    connection so the next request opens a socket again and is counted."""
    conn = Connection(*service, timeout=TIMEOUT_S)
    for n in range(REQUESTS_PER_READER):
        url = urls[(offset + n) % len(urls)]
        try:
            status, body = get_reopening(conn, url, out)
        except (OSError, http.client.HTTPException) as exc:
            out.errors += 1
            out.examples.append(f"{url}: {type(exc).__name__}: {exc}")
            conn.close()
            continue
        if status != 200:
            out.failed += 1
            out.statuses[status] = out.statuses.get(status, 0) + 1
            out.examples.append(f"{url}: {status} {body[:120]!r}")
        elif body == expected[url]:
            out.identical += 1
        else:
            out.differing += 1
            out.examples.append(f"{url}: 200 with {body[:120]!r}")
    conn.close()


def write(service: tuple[str, int], stop: threading.Event, statuses: list[int]) -> None:
    """The writer: entries about WRITTEN, one after another on one connection, until the
    readers are done."""
    conn = Connection(*service, timeout=TIMEOUT_S)
    n = 0
    while not stop.is_set():
        n += 1
        entry = {
            "subject": WRITTEN,
            "kind": "work_order.open",
            "occurred_utc": "2026-09-20T09:00:00Z",
            "entered_by": "A. Tester, maintenance",
            "statement": f"written while {n} reads were in flight",
            "details": {"state": "deferred"},
        }
        try:
            conn.request(
                "POST",
                "/entries",
                body=json.dumps(entry),
                headers={"Content-Type": "application/json"},
            )
            response = conn.getresponse()
            response.read()
            statuses.append(response.status)
        except (OSError, http.client.HTTPException):
            statuses.append(0)  # the write did not complete at all
            conn.close()
        stop.wait(WRITE_EVERY_S)
    conn.close()


@pytest.mark.parametrize("readers", [2, 4, 8])
def test_concurrent_reads_answer_each_with_their_own_rows(
    service: tuple[str, int], readers: int
) -> None:
    opened_before = Connection.opened
    own = Connection(*service, timeout=TIMEOUT_S)
    urls = urls_to_read(own)
    expected = single_threaded(own, urls)
    own.close()  # idle while the readers run, so the server would close it; opened again below
    tallies = [Tally() for _ in range(readers)]
    threads = [
        threading.Thread(target=read, args=(service, urls, expected, i * 7, tallies[i]))
        for i in range(readers)
    ]
    stop = threading.Event()
    written: list[int] = []
    writer = threading.Thread(target=write, args=(service, stop, written))
    writer.start()
    for t in threads:
        t.start()
    for t in threads:
        t.join(TIMEOUT_S * REQUESTS_PER_READER)
    stop.set()
    writer.join(TIMEOUT_S)
    total = Tally()
    for tally in tallies:
        total.identical += tally.identical
        total.differing += tally.differing
        total.failed += tally.failed
        total.errors += tally.errors
        total.reopened += tally.reopened
        for status, count in tally.statuses.items():
            total.statuses[status] = total.statuses.get(status, 0) + count
        total.examples.extend(tally.examples)
    line = total.line(readers)
    assert not any(t.is_alive() for t in threads), "a reader did not finish: " + line
    assert total.identical == readers * REQUESTS_PER_READER, line
    assert (total.differing, total.failed, total.errors) == (0, 0, 0), line
    counts = {status: written.count(status) for status in sorted(set(written))}
    assert written and set(written) == {201}, f"the writer's statuses (0: not completed): {counts}"
    print(line + f"; {len(written)} writes")
    # The test's own footprint: one socket per reader, one for the writer, two for the
    # requests made here (before the readers and after them) and one per request made again
    # after the server closed an idle connection, at most one per reader, however many
    # requests they make. One socket per request ran a machine out of local ports mid-suite
    # (issue #62) with nothing wrong in the service.
    opened = Connection.opened - opened_before
    assert total.reopened <= readers, line
    assert opened <= readers + 3 + total.reopened, (
        f"{opened} sockets opened for {readers} readers and a writer; " + line
    )
    # The writes landed: every one of them is in the store, with the rest of the fleet as read.
    status, body = get(own, f"/entries?subject={WRITTEN}")
    own.close()
    assert status == 200
    assert len([e for e in json.loads(body) if "reads were in flight" in e["statement"]]) >= len(
        written
    )


def test_a_connection_the_server_closed_as_idle_is_opened_again_once(
    service: tuple[str, int],
) -> None:
    """Left idle past the server's keep-alive timeout, a connection is closed by the server,
    and its next request fails: `RemoteDisconnected` on Linux, `ConnectionAbortedError` on
    Windows. Either way that request is made again on a new connection, once, and answers."""
    conn = Connection(*service, timeout=TIMEOUT_S)
    out = Tally()
    assert get(conn, "/health")[0] == 200
    time.sleep(IDLE_CLOSE_S + 2.0)
    assert get_reopening(conn, "/health", out)[0] == 200
    assert out.reopened == 1
    assert get_reopening(conn, "/health", out)[0] == 200 and out.reopened == 1
    conn.close()
