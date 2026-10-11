"""Concurrent reads through a live service each answer with their own rows (issue #60).

Every request thread read through the store's one SQLite connection with nothing serialising
the reads, and Python's `sqlite3` keeps one statement cache per connection: two threads running
the same SQL stepped the same prepared statement and read each other's rows. A GET could then
answer 200 with the body of another query, or 500. Measured on main: 0 wrong of 2,394 with one
reader; 21 wrong and 41 errors with two; 143 and 205 with four; 114 and 221 with eight.

The check is through a real server, `uasw serve` in a process of its own on a loopback port,
never the single-threaded `TestClient`: N reader threads loop over several routes for every
aircraft but one, each body is compared with the single-threaded answer for the same URL, and
a writer posts entries about the one aircraft they never read meanwhile, so no answer they
compare changes. Required: zero differing 200s, zero 5xx, zero failed requests, and every
write stored. No assertion names an OS: no error text, no path separator.
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


class Connection(http.client.HTTPConnection):
    """A connection to the service that counts the sockets the test opens through it. The
    count is the test's measure of its own footprint on the machine: a run that opens a
    socket per request can run out of local ports where a port closed stays reserved for a
    while, and that fails the test with nothing wrong in the service."""

    opened = 0
    _count = threading.Lock()

    def connect(self) -> None:
        super().connect()
        with Connection._count:
            Connection.opened += 1


@dataclass
class Tally:
    identical: int = 0
    differing: int = 0  # 200 with a body that is not the single-threaded answer
    failed: int = 0  # any status that is not 200
    errors: int = 0  # the request did not complete at all
    statuses: dict[int, int] = field(default_factory=dict)
    examples: list[str] = field(default_factory=list)

    def line(self, readers: int) -> str:
        return (
            f"{readers} readers: {self.identical} identical, {self.differing} differing 200s, "
            f"{self.failed} non-200 {dict(sorted(self.statuses.items()))}, "
            f"{self.errors} errors"
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
            try:
                status, _ = get((host, port), "/health")
            except OSError:
                status = 0
            if status == 200:
                break
            assert time.monotonic() < deadline, "the service did not start"
            time.sleep(0.05)
        assert server.poll() is None, f"the service exited with {server.returncode}"
        yield host, port
    finally:
        server.terminate()
        server.wait(30)


def get(service: tuple[str, int], url: str) -> tuple[int, bytes]:
    """One request on a connection of its own. A kept-alive connection would be the natural
    thing, but on the server's keep-alive path one small write waits for the delayed ACK of
    the one before it (about 40 ms on Linux, 200 ms on Windows), which would make a request
    wait on the network rather than on the store, and the test much slower."""
    conn = Connection(*service, timeout=TIMEOUT_S)
    try:
        conn.request("GET", url, headers={"Connection": "close"})
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


def urls_to_read(service: tuple[str, int]) -> list[str]:
    """Several routes for every aircraft the writer never touches."""
    status, body = get(service, "/aircraft")
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


def single_threaded(service: tuple[str, int], urls: list[str]) -> dict[str, bytes]:
    """The answer for every URL with one reader and nothing else going on: twice, so the
    comparison below is known to be against a deterministic answer."""
    first = {}
    for url in urls:
        status, body = get(service, url)
        assert status == 200, (url, status, body[:200])
        first[url] = body
    for url in urls:
        assert get(service, url) == (200, first[url]), f"{url} is not deterministic alone"
    return first


def read(
    service: tuple[str, int], urls: list[str], expected: dict[str, bytes], offset: int, out: Tally
) -> None:
    """One reader: REQUESTS_PER_READER requests round-robin over the URLs."""
    for n in range(REQUESTS_PER_READER):
        url = urls[(offset + n) % len(urls)]
        try:
            status, body = get(service, url)
        except (OSError, http.client.HTTPException) as exc:
            out.errors += 1
            out.examples.append(f"{url}: {type(exc).__name__}: {exc}")
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


def write(service: tuple[str, int], stop: threading.Event, statuses: list[int]) -> None:
    """The writer: entries about WRITTEN, one after another, until the readers are done."""
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
        conn = Connection(*service, timeout=TIMEOUT_S)
        try:
            conn.request(
                "POST",
                "/entries",
                body=json.dumps(entry),
                headers={"Content-Type": "application/json", "Connection": "close"},
            )
            response = conn.getresponse()
            response.read()
            statuses.append(response.status)
        except (OSError, http.client.HTTPException):
            statuses.append(0)  # the write did not complete at all
        finally:
            conn.close()
        stop.wait(WRITE_EVERY_S)


@pytest.mark.parametrize("readers", [2, 4, 8])
def test_concurrent_reads_answer_each_with_their_own_rows(
    service: tuple[str, int], readers: int
) -> None:
    opened_before = Connection.opened
    urls = urls_to_read(service)
    expected = single_threaded(service, urls)
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
    # The test's own footprint: one socket per reader, one for the writer and one for the
    # requests made here, however many requests they make. One socket per request ran a
    # Windows machine out of local ports mid-suite (issue #62) with nothing wrong in the
    # service; a socket that stays open is also what a browser or a client library does.
    opened = Connection.opened - opened_before
    assert opened <= readers + 2, f"{opened} sockets opened for {readers} readers and a writer"
    # The writes landed: every one of them is in the store, with the rest of the fleet as read.
    status, body = get(service, f"/entries?subject={WRITTEN}")
    assert status == 200
    assert len([e for e in json.loads(body) if "reads were in flight" in e["statement"]]) >= len(
        written
    )
