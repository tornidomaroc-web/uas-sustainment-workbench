"""Concurrent reads through a live service each answer with their own rows (issue #60).

Every request thread read through the store's one SQLite connection with nothing serialising
the reads, and Python's `sqlite3` keeps one statement cache per connection: two threads running
the same SQL stepped the same prepared statement and read each other's rows. A GET could then
answer 200 with the body of another query, or 500. Measured on main: 0 wrong of 2,394 with one
reader; 21 wrong and 41 errors with two; 143 and 205 with four; 114 and 221 with eight.

The check is through a real server (`uvicorn` on loopback, as `uasw serve` runs it), never the
single-threaded `TestClient`: N reader threads loop over several routes for every aircraft but
one, each body is compared with the single-threaded answer for the same URL, and a writer posts
entries about the one aircraft they never read meanwhile, so no answer they compare changes.
Required: zero differing 200s, zero 5xx, zero failed requests, and every write stored. No
assertion names an OS: no error text, no path separator.
"""

from __future__ import annotations

import http.client
import json
import socket
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import uvicorn

from uas_workbench.service.app import create_app
from uas_workbench.service.cli import seed
from uas_workbench.service.store import Store

FIXTURES = Path(__file__).parent / "fixtures"
AS_OF = "2026-10-01T00:00:00Z"  # pinned, so a status computed as of now cannot move mid-test
WRITTEN = "SYN-02"  # the writer's aircraft; readers never ask about it
REQUESTS_PER_READER = 200
TIMEOUT_S = 30.0


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
    """`uasw serve` on a seeded file store, in a thread, on a free loopback port."""
    path = str(tmp_path_factory.mktemp("store") / "fleet.sqlite")
    store = Store(path)
    seed(store, FIXTURES)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(64)
    host, port = sock.getsockname()[:2]
    # A user on this machine: with no write token set, loopback may write.
    config = uvicorn.Config(
        create_app(store, write_token=None), log_config=None, log_level="warning"
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    for _ in range(400):
        if server.started:
            break
        thread.join(0.05)
    assert server.started, "the service did not start"
    try:
        yield host, port
    finally:
        server.should_exit = True
        thread.join(10)
        store.close()


def get(conn: http.client.HTTPConnection, url: str) -> tuple[int, bytes]:
    conn.request("GET", url)
    response = conn.getresponse()
    return response.status, response.read()


def urls_to_read(service: tuple[str, int]) -> list[str]:
    """Several routes for every aircraft the writer never touches."""
    conn = http.client.HTTPConnection(*service, timeout=TIMEOUT_S)
    try:
        status, body = get(conn, "/aircraft")
    finally:
        conn.close()
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
    conn = http.client.HTTPConnection(*service, timeout=TIMEOUT_S)
    try:
        first = {}
        for url in urls:
            status, body = get(conn, url)
            assert status == 200, (url, status, body[:200])
            first[url] = body
        for url in urls:
            assert get(conn, url) == (200, first[url]), f"{url} is not deterministic alone"
    finally:
        conn.close()
    return first


def read(
    service: tuple[str, int], urls: list[str], expected: dict[str, bytes], offset: int, out: Tally
) -> None:
    """One reader: REQUESTS_PER_READER requests round-robin over the URLs on one kept-alive
    connection, reopened after a failed request."""
    conn = http.client.HTTPConnection(*service, timeout=TIMEOUT_S)
    try:
        for n in range(REQUESTS_PER_READER):
            url = urls[(offset + n) % len(urls)]
            try:
                status, body = get(conn, url)
            except (OSError, http.client.HTTPException) as exc:
                out.errors += 1
                out.examples.append(f"{url}: {type(exc).__name__}")
                conn.close()
                conn = http.client.HTTPConnection(*service, timeout=TIMEOUT_S)
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
    finally:
        conn.close()


def write(service: tuple[str, int], stop: threading.Event, statuses: list[int]) -> None:
    """The writer: entries about WRITTEN, one after another, until the readers are done."""
    conn = http.client.HTTPConnection(*service, timeout=TIMEOUT_S)
    try:
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
            except (OSError, http.client.HTTPException):
                statuses.append(0)  # the write did not complete at all
                conn.close()
                conn = http.client.HTTPConnection(*service, timeout=TIMEOUT_S)
                continue
            statuses.append(response.status)
    finally:
        conn.close()


@pytest.mark.parametrize("readers", [2, 4, 8])
def test_concurrent_reads_answer_each_with_their_own_rows(
    service: tuple[str, int], readers: int
) -> None:
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
    # The writes landed: every one of them is in the store, with the rest of the fleet as read.
    conn = http.client.HTTPConnection(*service, timeout=TIMEOUT_S)
    try:
        status, body = get(conn, f"/entries?subject={WRITTEN}")
    finally:
        conn.close()
    assert status == 200
    assert len([e for e in json.loads(body) if "reads were in flight" in e["statement"]]) >= len(
        written
    )
