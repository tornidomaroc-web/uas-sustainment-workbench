"""One instant gives the same due dates, calendar life and board at any UTC offset (issue #149).

An entry's time and a query's `as_of` kept the offset they were given, and the life engine and
the ledger took dates from them as written, so an annual inspection entered at
2025-09-01T02:00+05:00 (2025-08-31 21:00 UTC) fell due a month later than the same instant
written in UTC, and a query at 2026-08-31T23:30-05:00 kept a battery within its calendar life
a day after it ended. Now every time is converted to UTC where it comes in, a stored entry
that already carries an offset (written by 0.6.6 or earlier) is read in UTC without its row
being rewritten, so its journal link still verifies, and the dates are taken in UTC.

The rows of the issue run real `uasw` processes, and the service rows a real `uasw serve`,
each on a store of its own: one aircraft, the committed PX4 log (2026-06-12).
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from test_life_rules import POLICY, compute, record

from uas_workbench.assistant.tools import resolve
from uas_workbench.ledger.codec import entry_to_json
from uas_workbench.ledger.model import Entry
from uas_workbench.life import Component, InspectionDone, Installation, board
from uas_workbench.service import journal

FIXTURES = Path(__file__).parent / "fixtures"
LOG = next((FIXTURES / "px4").glob("*.ulg"))
WHO = ["--by", "A. Tester, maintenance", "--statement", "work done as described"]
UASW = [sys.executable, "-m", "uas_workbench.service.cli", "--db", "fleet.sqlite"]

ANNUAL_OVERDUE = (
    "the annual inspection of aircraft X-1 was due by 2026-08-31 and is 15 days overdue"
)
BATTERY_PAST = (
    "battery pack BAT-1 on aircraft X-1 passed its 24-calendar-month life limit on 2026-08-31"
)
BATTERY_LAST_DAY = (
    "battery pack BAT-1 on aircraft X-1 has 0 days left of its 24-calendar-month life limit, "
    "due by 2026-08-31"
)
FITTED_PAST = "its 24-calendar-month life limit ended on 2026-08-31"


def uasw(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run one `uasw` command in a process of its own."""
    env = {k: v for k, v in os.environ.items() if k != "UASW_WRITE_TOKEN"}
    return subprocess.run(
        [*UASW, *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=180
    )


def ok(cwd: Path, *args: str) -> str:
    r = uasw(cwd, *args)
    assert r.returncode == 0, r.stderr
    return r.stdout


def programme(cwd: Path, as_of: str) -> dict[str, Any]:
    ok(cwd, "evidence", "X-1", "--out", "pack.html", "--json", "--as-of", as_of)
    pack = json.loads((cwd / "pack.json").read_text(encoding="utf-8"))
    out: dict[str, Any] = pack["programme"]
    return out


def calendar_item(prog: dict[str, Any], subject: str) -> dict[str, Any]:
    (found,) = [i for i in prog["items"] if i["subject"] == subject and i["basis"] == "calendar"]
    return found


def annual_store(cwd: Path, at: str) -> None:
    """One aircraft and one annual inspection at `at`, no hours flown by then."""
    ok(cwd, "ingest", "X-1", str(LOG))
    ok(cwd, "record", "inspection", "X-1", "annual inspection", "--at-hours", "0", *WHO, "--at", at)


def battery_store(cwd: Path) -> None:
    """A battery in service since 2024-08-15 (24 calendar months: its last day is
    2026-08-31), fitted 2025-01-01, and an annual on record so that only the battery decides."""
    ok(cwd, "ingest", "X-1", str(LOG))
    ok(cwd, "record", "inspection", "X-1", "annual inspection", "--at-hours", "0", *WHO,
       "--at", "2026-06-01T00:00Z")  # fmt: skip
    register(cwd)
    ok(cwd, "record", "component", "install", "BAT-1", "X-1", *WHO, "--at", "2025-01-01T00:00Z")


def register(cwd: Path) -> None:
    ok(cwd, "record", "component", "register", "BAT-1", "--kind", "battery pack",
       "--since", "2024-08-15", *WHO, "--at", "2024-12-31T00:00Z")  # fmt: skip


# ---- the rows of the issue, through the command line --------------------------------------


# The annual inspection at 2025-08-31 21:00 UTC, written four ways; the pack at 2026-09-15.
# The +05:00 row read due by 2026-09-30, in 15 days, serviceable. The naive row is UTC.
@pytest.mark.parametrize(
    "at",
    [
        "2025-08-31T21:00:00Z",
        "2025-09-01T02:00:00+05:00",
        "2025-08-31T16:00:00-05:00",
        "2025-08-31T21:00:00",
    ],
)
def test_an_annual_entered_at_any_offset_falls_due_as_in_utc(tmp_path: Path, at: str) -> None:
    annual_store(tmp_path, at)
    prog = programme(tmp_path, "2026-09-15T00:00Z")
    annual = calendar_item(prog, "the annual inspection")
    assert (annual["state"], annual["remaining"]) == ("overdue", -15.0)
    assert annual["message"] == ANNUAL_OVERDUE
    assert prog["status"] == "unserviceable"
    assert prog["status_reasons"] == [ANNUAL_OVERDUE]


# The same pack asked at the instant 2026-09-15 00:00 UTC, written three ways. The -05:00 row
# read 14 days overdue, the date of 2026-09-14 19:00 in that offset.
@pytest.mark.parametrize(
    "as_of", ["2026-09-15T05:00:00+05:00", "2026-09-14T19:00:00-05:00", "2026-09-15T00:00:00"]
)
def test_an_annual_asked_at_any_offset_counts_its_days_in_utc(tmp_path: Path, as_of: str) -> None:
    annual_store(tmp_path, "2025-08-31T21:00:00Z")
    prog = programme(tmp_path, as_of)
    annual = calendar_item(prog, "the annual inspection")
    assert (annual["state"], annual["remaining"]) == ("overdue", -15.0)
    assert annual["message"] == ANNUAL_OVERDUE
    assert prog["status"] == "unserviceable"
    assert prog["status_reasons"] == [ANNUAL_OVERDUE]


# The battery past its life at the instant 2026-09-01 04:30 UTC, written three ways. The -05:00
# row read 0 days left, due soon, serviceable.
@pytest.mark.parametrize(
    "as_of", ["2026-09-01T04:30:00Z", "2026-08-31T23:30:00-05:00", "2026-09-01T04:30:00"]
)
def test_a_battery_asked_at_any_offset_is_past_its_calendar_life_as_in_utc(
    tmp_path: Path, as_of: str
) -> None:
    battery_store(tmp_path)
    prog = programme(tmp_path, as_of)
    battery = calendar_item(prog, "battery pack BAT-1")
    assert (battery["state"], battery["remaining"]) == ("overdue", -1.0)
    assert battery["message"] == BATTERY_PAST
    assert prog["status"] == "unserviceable"
    assert prog["status_reasons"] == [BATTERY_PAST]


# The other way: on its last day in UTC, 2026-08-31 23:30, the battery is within its life
# however the instant is written. The +05:00 row read past, unserviceable.
@pytest.mark.parametrize("as_of", ["2026-08-31T23:30:00Z", "2026-09-01T04:30:00+05:00"])
def test_a_battery_on_its_last_utc_day_is_within_its_life_at_any_offset(
    tmp_path: Path, as_of: str
) -> None:
    battery_store(tmp_path)
    prog = programme(tmp_path, as_of)
    battery = calendar_item(prog, "battery pack BAT-1")
    assert (battery["state"], battery["remaining"]) == ("due_soon", 0.0)
    assert battery["message"] == BATTERY_LAST_DAY
    assert prog["status"] == "serviceable"


# ---- the ledger's check at an install (validate.py), both ways -----------------------------


# Past its life in UTC (2026-09-01 04:30), written at -05:00: the install was accepted.
@pytest.mark.parametrize("at", ["2026-09-01T04:30:00Z", "2026-08-31T23:30:00-05:00"])
def test_an_install_after_the_last_utc_day_is_refused_at_any_offset(
    tmp_path: Path, at: str
) -> None:
    ok(tmp_path, "ingest", "X-1", str(LOG))
    register(tmp_path)
    r = uasw(tmp_path, "record", "component", "install", "BAT-1", "X-1", *WHO, "--at", at)
    assert r.returncode == 1
    assert r.stderr.startswith("refused (409): BAT-1 cannot be fitted to X-1 at 2026-09-01 04:30")
    assert FITTED_PAST in r.stderr
    assert len(ok(tmp_path, "record", "history", "BAT-1").splitlines()) == 1  # the register


# On its last day in UTC (2026-08-31 21:00), written at +05:00: the install was refused.
@pytest.mark.parametrize("at", ["2026-08-31T21:00:00Z", "2026-09-01T02:00:00+05:00"])
def test_an_install_on_the_last_utc_day_is_accepted_at_any_offset(tmp_path: Path, at: str) -> None:
    ok(tmp_path, "ingest", "X-1", str(LOG))
    register(tmp_path)
    r = uasw(tmp_path, "record", "component", "install", "BAT-1", "X-1", *WHO, "--at", at)
    assert r.returncode == 0, r.stderr
    history = ok(tmp_path, "record", "history", "BAT-1").splitlines()
    assert history[-1].startswith("#2 2026-08-31 21:00 component.install by A. Tester")


# ---- what the store holds and shows --------------------------------------------------------


def test_an_entry_given_with_an_offset_is_stored_and_shown_in_utc(tmp_path: Path) -> None:
    annual_store(tmp_path, "2025-09-01T02:00:00+05:00")
    (line,) = ok(tmp_path, "record", "history", "X-1").splitlines()
    assert line.startswith("#1 2025-08-31 21:00 inspection.done by A. Tester")
    c = sqlite3.connect(f"file:{(tmp_path / 'fleet.sqlite').as_posix()}?mode=ro", uri=True)
    try:
        column, text = c.execute("SELECT occurred_utc, record FROM entries").fetchone()
    finally:
        c.close()
    assert column == "2025-08-31T21:00:00+00:00"
    assert json.loads(text)["occurred_utc"] == "2025-08-31T21:00:00+00:00"


def write_as_0_6_6(db: Path, entry: Entry) -> None:
    """Append one entry as 0.6.6 wrote it, its offset kept, with its journal link."""
    c = sqlite3.connect(db)
    try:
        with c:
            cur = c.execute(
                "INSERT INTO entries (subject, kind, occurred_utc, recorded_utc, synthetic, "
                "supersedes, record) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    entry.subject,
                    entry.kind,
                    entry.occurred_utc.isoformat(),
                    entry.recorded_utc.isoformat(),
                    int(entry.synthetic),
                    entry.supersedes,
                    "",
                ),
            )
            stored = Entry(**{**entry.__dict__, "id": cur.lastrowid})
            c.execute(
                "UPDATE entries SET record = ? WHERE id = ?",
                (json.dumps(entry_to_json(stored)), stored.id),  # ASCII: as the store writes it
            )
            journal.append_link(c, "entry", stored.id)
    finally:
        c.close()


# A store written by 0.6.6 holds the entry with its offset. It is read in UTC and counted in
# UTC; its row is not rewritten, so the journal verifies with the same head before and after.
def test_an_entry_stored_with_an_offset_is_read_in_utc_and_its_journal_still_verifies(
    tmp_path: Path,
) -> None:
    ok(tmp_path, "ingest", "X-1", str(LOG))
    plus5 = timezone(timedelta(hours=5))
    write_as_0_6_6(
        tmp_path / "fleet.sqlite",
        Entry(
            id=None,
            subject="X-1",
            kind="inspection.done",
            occurred_utc=datetime(2025, 9, 1, 2, 0, tzinfo=plus5),
            recorded_utc=datetime(2025, 9, 1, 2, 5, tzinfo=plus5),
            entered_by="A. Tester, maintenance",
            statement="work done as described",
            details={"name": "annual inspection", "at_hours_s": 0.0, "carried_over_s": 0.0},
            supersedes=None,
            reason=None,
            synthetic=False,
        ),
    )
    before = ok(tmp_path, "verify")
    assert before.startswith("journal intact: 2 links"), before
    prog = programme(tmp_path, "2026-09-15T00:00Z")
    annual = calendar_item(prog, "the annual inspection")
    assert (annual["state"], annual["message"]) == ("overdue", ANNUAL_OVERDUE)
    assert prog["status"] == "unserviceable"
    (line,) = ok(tmp_path, "record", "history", "X-1").splitlines()
    assert line.startswith("#1 2025-08-31 21:00 inspection.done")
    assert ok(tmp_path, "verify") == before
    c = sqlite3.connect(f"file:{(tmp_path / 'fleet.sqlite').as_posix()}?mode=ro", uri=True)
    try:
        (column,) = c.execute("SELECT occurred_utc FROM entries").fetchone()
    finally:
        c.close()
    assert column == "2025-09-01T02:00:00+05:00"  # read in UTC, never rewritten


# ---- the service, a real `uasw serve` ------------------------------------------------------


def call(base: str, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
    req = urllib.request.Request(
        base + path,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"Content-Type": "application/json"} if body is not None else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        with e:
            return e.code, json.loads(e.read().decode("utf-8"))


def stop(p: subprocess.Popen[bytes]) -> None:
    """Stop the service and every process under it (a venv's python.exe is a launcher)."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True)
    else:
        p.terminate()
    p.wait(30)


@pytest.fixture
def served(tmp_path: Path) -> Iterator[str]:
    """`uasw serve` on a store of its own holding X-1 and its log, answering `/health`."""
    ok(tmp_path, "ingest", "X-1", str(LOG))
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = {k: v for k, v in os.environ.items() if k not in ("UASW_WRITE_TOKEN", "UASW_FIXTURES")}
    log = (tmp_path / "serve.log").open("w")
    p = subprocess.Popen(
        [*UASW, "serve", "--port", str(port), "--fixtures", str(FIXTURES)],
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
        yield base
    finally:
        stop(p)
        log.close()


def test_the_service_stores_an_entry_in_utc_and_counts_any_as_of_in_utc(served: str) -> None:
    status, entry = call(
        served,
        "POST",
        "/entries",
        {
            "subject": "X-1",
            "kind": "inspection.done",
            "occurred_utc": "2025-09-01T02:00:00+05:00",
            "entered_by": "A. Tester, maintenance",
            "statement": "work done as described",
            "details": {"name": "annual inspection", "at_hours_s": 0.0},
        },
    )
    assert status == 201, entry
    assert entry["occurred_utc"] == "2025-08-31T21:00:00Z"
    status, entries = call(served, "GET", "/entries")
    assert [e["occurred_utc"] for e in entries] == ["2025-08-31T21:00:00Z"]
    for as_of in ("2026-09-15T00:00:00Z", "2026-09-14T19:00:00-05:00", "2026-09-15T00:00:00"):
        query = urllib.parse.urlencode({"as_of": as_of})
        status, due = call(served, "GET", f"/aircraft/X-1/due?{query}")
        assert status == 200, due
        assert due["as_of"] == "2026-09-15T00:00:00Z", as_of
        (annual,) = [i for i in due["items"] if i["subject"] == "the annual inspection"]
        assert (annual["state"], annual["message"]) == ("overdue", ANNUAL_OVERDUE), as_of
        assert due["status"] == "unserviceable", as_of
        status, row = call(served, "GET", f"/aircraft/X-1?{query}")
        assert (status, row["status"]) == (200, "unserviceable"), as_of


# ---- the engine and the assistant's tools, on their own ------------------------------------


PLUS5 = timezone(timedelta(hours=5))
MINUS5 = timezone(timedelta(hours=-5))


def test_the_engine_takes_every_date_in_utc_whatever_offset_it_is_given() -> None:
    done_utc = datetime(2025, 8, 31, 21, 0, tzinfo=UTC)
    as_of_utc = datetime(2026, 9, 15, 0, 0, tzinfo=UTC)
    for done in (done_utc, done_utc.astimezone(PLUS5), done_utc.astimezone(MINUS5)):
        for as_of in (as_of_utc, as_of_utc.astimezone(PLUS5), as_of_utc.astimezone(MINUS5)):
            rec = record("A", inspections=(InspectionDone("annual inspection", done, 0.0),))
            due = compute("A", rec, as_of=as_of)
            (annual,) = [i for i in due.items if i.subject == "the annual inspection"]
            assert (annual.remaining, annual.state) == (-15.0, "overdue"), (done, as_of)
            assert board(due).status == "unserviceable"
            assert due.as_of == as_of_utc and due.as_of.utcoffset() == timedelta(0)


def test_the_engine_counts_a_calendar_life_in_utc_whatever_offset_as_of_carries() -> None:
    fitted = datetime(2025, 1, 1, tzinfo=UTC)
    bat = Component("BAT-1", "battery pack", date(2024, 8, 15), 0.0, 0,
                    (Installation("A", fitted, None),), True)  # fmt: skip
    past = datetime(2026, 9, 1, 4, 30, tzinfo=UTC)
    last = datetime(2026, 8, 31, 23, 30, tzinfo=UTC)
    for at, remaining in ((past, -1.0), (last, 0.0)):
        for as_of in (at, at.astimezone(PLUS5), at.astimezone(MINUS5)):
            due = compute("A", record("A"), components=(bat,), as_of=as_of)
            (life,) = [i for i in due.items if i.component_id == "BAT-1" and i.basis == "calendar"]
            assert life.remaining == remaining, as_of


def test_the_assistant_sends_as_of_in_utc() -> None:
    _, query = resolve("aircraft_due", {"key": "X-1"}, datetime(2026, 9, 14, 19, 0, tzinfo=MINUS5))
    assert query["as_of"] == "2026-09-15T00:00:00Z"
    assert POLICY.inspections["annual inspection"].limits() == (("calendar", 12),)
