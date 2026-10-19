"""A damaged store is checked again after a wait that grows with the check's own time (#113).

A running service that found its store file damaged looked at it again at most once a second,
timed from the end of the last check, so while requests kept coming it spent about
check / (check + 1 s) of one core on SQLite's `integrity_check`: measured on Windows, 22 and
27 % at 86 MiB of 1 KB rows (0.26 s a check) and 84 to 86 % at 83 MiB of 795,000 small indexed
rows (4.3 s a check), under 20 and 100 `/health` requests a second.

Now the next check of a damaged file waits at least nine times as long as the last check took,
and never less than a second, so the checks take at most a tenth of one core whatever the
store's shape. A file restored in place is still checked at the first request after its `stat`
changes; one restored so that its `stat` reads as before (same size, same time of last change,
within one clock tick) is seen within that wait plus one check, where it was within a second.

The service is a real `uasw serve` process, logging to a file, with one thing injected: the
check sleeps a fixed time first, so that its time is known, and the process writes when each
check began and ended to a file of its own. Assertions are about those times and the service's
statuses, never an operating system's error text, a path or a line ending.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path

import pytest
from test_damaged_store import FIXTURES, damage_in_place

FACTOR = 9  # the wait before a damaged file is checked again, in checks' times
FLOOR_S = 1.0  # and never less than this
DELAY_S = 0.3  # what the injected sleep adds to every check
CLI = [sys.executable, "-m", "uas_workbench.service.cli"]

SERVE = """
import json, sys, time
from uas_workbench.service import cli, store

log, delay = sys.argv[1], float(sys.argv[2])
checked = store.Store._damage_in

def slow(self, now):
    began = time.monotonic()
    time.sleep(delay)
    found = checked(self, now)
    with open(log, "a", encoding="utf-8") as f:
        f.write(json.dumps([began, time.monotonic(), found is not None]) + "\\n")
    return found

store.Store._damage_in = slow
cli.main(sys.argv[3:])
"""


@dataclass
class Live:
    base: str
    db: Path
    original: bytes
    checks: Path


def health(base: str) -> int:
    try:
        with urllib.request.urlopen(base + "/health", timeout=60) as r:
            return int(r.status)
    except urllib.error.HTTPError as e:
        with e:
            return int(e.code)


def checks(path: Path) -> list[tuple[float, float, bool]]:
    if not path.exists():
        return []
    found = []
    for line in path.read_text().splitlines():
        began, ended, damaged = json.loads(line)
        found.append((float(began), float(ended), bool(damaged)))
    return found


@pytest.fixture
def live(tmp_path: Path) -> Iterator[Live]:
    env = {k: v for k, v in os.environ.items() if k not in ("UASW_WRITE_TOKEN", "UASW_FIXTURES")}
    db = tmp_path / "fleet.sqlite"
    subprocess.run(
        [*CLI, "--db", str(db), "seed", "--fixtures", str(FIXTURES)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        check=True,
        timeout=180,
    )
    original = db.read_bytes()
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    log = (tmp_path / "serve.log").open("w")
    record = tmp_path / "checks.jsonl"
    served = ["--db", str(db), "serve", "--port", str(port)]
    p = subprocess.Popen(
        [sys.executable, "-c", SERVE, str(record), str(DELAY_S), *served],
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
        yield Live(base, db, original, record)
    finally:
        p.terminate()
        p.wait(30)
        log.close()


def asked_steadily(base: str, seconds: float, every: float = 0.05) -> list[int]:
    """`/health` asked about 20 times a second for `seconds`: the statuses."""
    statuses, end = [], time.monotonic() + seconds
    while time.monotonic() < end:
        statuses.append(health(base))
        time.sleep(every)
    return statuses


def test_a_damaged_file_is_checked_again_only_after_nine_times_the_last_checks_time(
    live: Live,
) -> None:
    assert health(live.base) == 200
    sound = len(checks(live.checks))  # a sound file may be checked as the service starts
    damage_in_place(live.db, live.original, "pages overwritten")
    statuses = asked_steadily(live.base, 12.0)
    assert set(statuses) == {503}, sorted(set(statuses))
    found = checks(live.checks)[sound:]
    assert len(found) >= 2 and all(damaged for _, _, damaged in found), found
    for (began, ended, _), (next_began, _, _) in pairwise(found):
        took = ended - began
        assert next_began - ended >= max(FLOOR_S, FACTOR * took), (took, next_began - ended)
    # The checks' share of the time: at most one part in FACTOR + 1 of each check and its wait.
    whole = found[-1][0] - found[0][0]
    assert sum(e - b for b, e, _ in found[:-1]) <= whole / (FACTOR + 1)


def test_a_file_restored_in_place_as_it_looked_is_served_within_the_wait_and_one_check(
    live: Live,
) -> None:
    """Restored so that one `stat` cannot tell it from the damaged file: only the second look
    finds it, which waits the time above; it is then served, every connection opened afresh."""
    assert health(live.base) == 200
    sound = len(checks(live.checks))
    damage_in_place(live.db, live.original, "pages overwritten")
    damaged = os.stat(live.db)
    assert health(live.base) == 503
    first = checks(live.checks)[sound:]
    assert len(first) == 1 and first[0][2], first
    took = first[0][1] - first[0][0]
    with live.db.open("r+b") as f:  # in place: the same file, the same size
        f.write(live.original)
    os.utime(live.db, ns=(damaged.st_atime_ns, damaged.st_mtime_ns))
    restored = os.stat(live.db)
    assert (restored.st_size, restored.st_mtime_ns) == (damaged.st_size, damaged.st_mtime_ns)
    put_back = time.monotonic()
    deadline = put_back + max(FLOOR_S, FACTOR * took) + took + 10.0
    while health(live.base) != 200:
        assert time.monotonic() < deadline, checks(live.checks)
        time.sleep(0.05)
    served = time.monotonic() - put_back
    last = checks(live.checks)[-1]
    assert last[2] is False
    # Seen at the first request after the wait, timed from the end of the damaged file's check.
    assert last[0] - first[0][1] >= max(FLOOR_S, FACTOR * took)
    assert served <= max(FLOOR_S, FACTOR * took) + took + 10.0
