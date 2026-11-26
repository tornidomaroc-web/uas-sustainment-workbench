"""A calendar item is counted at the bound that is never late, whatever offset a time carries
(issue #174).

Since #149 every time was converted to UTC where it came in and every date was its UTC date.
That reads late the other way: an annual inspection entered at 2025-08-31T22:00-05:00 (UTC
2025-09-01 03:00) read "due by 2026-09-30, in 15 days", serviceable, on 2026-09-15, where its
date as entered gives due by 2026-08-31, 15 days overdue; a battery whose last day is
2026-08-31, asked at 2026-09-01T02:00+05:00 (UTC 2026-08-31 21:00), read 0 days left; and it
could be fitted at that time. And the offset was dropped from every new row, so the date as
entered was lost.

Now a calendar interval is counted from the earlier of its start's date as entered and its
UTC date, and to the later of the computation date's (or an install's) date as given and its
UTC date; the time is stored as given, offset included, and shown in UTC. Where the bound
reads earlier than the UTC count, the item's sentence says so.

Every row of the issue runs real `uasw` processes, and the service rows a real `uasw serve`,
each on a store of its own: one aircraft, the committed PX4 log (2026-06-12).
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import sqlite3
import subprocess
import time
import urllib.parse
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any

import pytest
from test_life_rules import compute, record
from test_utc_offsets import (
    FIXTURES,
    LOG,
    UASW,
    WHO,
    calendar_item,
    call,
    ok,
    programme,
    stop,
    uasw,
    write_as_0_6_6,
)

from uas_workbench.assistant.tools import resolve
from uas_workbench.fleet import load_config
from uas_workbench.fleet.synthetic import generate
from uas_workbench.ledger.model import Entry
from uas_workbench.life import Component, InspectionDone, Installation
from uas_workbench.life.cue import item_cue
from uas_workbench.service import app as app_module
from uas_workbench.service import static_export
from uas_workbench.service.static_export import export_static
from uas_workbench.service.store import Store

NOT_FITTED_AGAIN = (
    "a life-limited part that has reached its life limit is replaced, not fitted again"
)


def annual_overdue(due: str, entered: str, utc: str) -> str:
    """The sentence of an annual inspection counted from its date as entered."""
    return (
        f"the annual inspection of aircraft X-1 was due by {due} and is 15 days overdue, "
        f"counted from its date as entered, {entered}, the earlier of that and its UTC date, "
        f"{utc}"
    )


def battery_past(ended: str, given: str, utc: str) -> str:
    """The sentence of a battery counted to the computation date as given."""
    return (
        f"battery pack BAT-1 on aircraft X-1 passed its 24-calendar-month life limit on "
        f"{ended}, counted to the computation date as given, {given}, the later of that and "
        f"its UTC date, {utc}"
    )


def install_refused(stamp: str, ended: str, given: str, utc: str) -> str:
    """The refusal of a part fitted on a date as given after its calendar life ended."""
    return (
        f"BAT-1 cannot be fitted to X-1 at {stamp}: its 24-calendar-month life limit ended on "
        f"{ended}, and this install's date as entered is {given}, the later of that and its "
        f"UTC date, {utc}; {NOT_FITTED_AGAIN}"
    )


def annual_store(cwd: Path, at: str) -> None:
    """One aircraft and one annual inspection at `at`, no hours flown by then."""
    ok(cwd, "ingest", "X-1", str(LOG))
    ok(cwd, "record", "inspection", "X-1", "annual inspection", "--at-hours", "0", *WHO, "--at", at)


def battery_store(cwd: Path, since: str, annual: str, fitted: str) -> None:
    """A battery in service since `since` (24 calendar months), fitted at `fitted`, and an
    annual inspection at `annual`, so that only the battery is past its limit."""
    ok(cwd, "ingest", "X-1", str(LOG))
    ok(cwd, "record", "inspection", "X-1", "annual inspection", "--at-hours", "0", *WHO,
       "--at", annual)  # fmt: skip
    registered(cwd, since)
    ok(cwd, "record", "component", "install", "BAT-1", "X-1", *WHO, "--at", fitted)


def registered(cwd: Path, since: str, at: str | None = None) -> None:
    """BAT-1 registered, in service since `since`, the register entry a day after it."""
    when = at or (date.fromisoformat(since) + timedelta(days=1)).isoformat() + "T00:00Z"
    ok(cwd, "record", "component", "register", "BAT-1", "--kind", "battery pack",
       "--since", since, *WHO, "--at", when)  # fmt: skip


def column(db: Path) -> list[tuple[str, str]]:
    """occurred_utc as each row holds it, in its column and in its record JSON."""
    c = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        rows = c.execute("SELECT occurred_utc, record FROM entries ORDER BY id").fetchall()
    finally:
        c.close()
    return [(str(col), json.loads(text)["occurred_utc"]) for col, text in rows]


def table_hashes(db: Path) -> dict[str, str]:
    """SHA-256 of every row of every table, in rowid order, read only."""
    c = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        names = [
            str(r[0])
            for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")
        ]
        out = {}
        for name in names:
            rows = c.execute(f'SELECT * FROM "{name}" ORDER BY rowid').fetchall()
            out[name] = hashlib.sha256(repr(rows).encode()).hexdigest()
        return out
    finally:
        c.close()


@contextmanager
def serving(cwd: Path) -> Iterator[str]:
    """`uasw serve` on the store in `cwd`, answering `/health`."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = {k: v for k, v in os.environ.items() if k not in ("UASW_WRITE_TOKEN", "UASW_FIXTURES")}
    log = (cwd / "serve.log").open("w")
    p = subprocess.Popen(
        [*UASW, "serve", "--port", str(port), "--fixtures", str(FIXTURES)],
        cwd=cwd,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=log,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 120
        while True:
            assert p.poll() is None, (cwd / "serve.log").read_text()[-600:]
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


def due_at(base: str, as_of: str, cues: bool = False) -> dict[str, Any]:
    query = urllib.parse.urlencode({"as_of": as_of, **({"cues": "true"} if cues else {})})
    status, due = call(base, "GET", f"/aircraft/X-1/due?{query}")
    assert status == 200, due
    out: dict[str, Any] = due
    return out


INSPECTION = {
    "subject": "X-1",
    "kind": "inspection.done",
    "entered_by": "A. Tester, maintenance",
    "statement": "work done as described",
    "details": {"name": "annual inspection", "at_hours_s": 0.0},
}


# ---- the start of a calendar interval: west of UTC late on a month's last day -------------


# Each read due a month later on main: by 2026-09-30, 2027-01-31, 2025-03-31 and 2026-09-30,
# in 15 or 16 days, serviceable.
@pytest.mark.parametrize(
    ("at", "as_of", "due", "entered", "utc"),
    [
        ("2025-08-31T22:00:00-05:00", "2026-09-15T00:00Z", "2026-08-31",
         "2025-08-31", "2025-09-01"),
        ("2025-12-31T22:00:00-05:00", "2027-01-15T00:00Z", "2026-12-31",
         "2025-12-31", "2026-01-01"),
        ("2024-02-29T22:00:00-05:00", "2025-03-15T00:00Z", "2025-02-28",
         "2024-02-29", "2024-03-01"),
        ("2025-08-31T14:00:00-11:00", "2026-09-15T00:00Z", "2026-08-31",
         "2025-08-31", "2025-09-01"),
    ],
)  # fmt: skip
def test_a_west_entry_on_a_months_last_day_counts_from_its_date_as_entered(
    tmp_path: Path, at: str, as_of: str, due: str, entered: str, utc: str
) -> None:
    annual_store(tmp_path, at)
    prog = programme(tmp_path, as_of)
    annual = calendar_item(prog, "the annual inspection")
    expected = annual_overdue(due, entered, utc)
    assert (annual["state"], annual["remaining"]) == ("overdue", -15.0)
    assert annual["message"] == expected
    assert prog["status"] == "unserviceable"
    assert prog["status_reasons"] == [expected]


# The time is kept as given and shown in UTC: the row holds -05:00, `record history` the UTC
# time, as on main. On main the row held 2025-09-01T03:00:00+00:00 and nothing kept -05:00.
def test_a_new_entry_keeps_its_time_as_given_and_is_shown_in_utc(tmp_path: Path) -> None:
    annual_store(tmp_path, "2025-08-31T22:00:00-05:00")
    assert column(tmp_path / "fleet.sqlite") == [
        ("2025-08-31T22:00:00-05:00", "2025-08-31T22:00:00-05:00")
    ]
    (line,) = ok(tmp_path, "record", "history", "X-1").splitlines()
    assert line.startswith("#1 2025-09-01 03:00 inspection.done by A. Tester")


# The same entry through `POST /entries` on a real `uasw serve`: answered and listed in UTC,
# stored as given, read overdue in the due list, the board and the evidence pack alike.
def test_the_service_keeps_a_west_entry_as_given_and_counts_it_from_its_date_as_entered(
    tmp_path: Path,
) -> None:
    ok(tmp_path, "ingest", "X-1", str(LOG))
    expected = annual_overdue("2026-08-31", "2025-08-31", "2025-09-01")
    with serving(tmp_path) as base:
        status, entry = call(
            base, "POST", "/entries", {**INSPECTION, "occurred_utc": "2025-08-31T22:00:00-05:00"}
        )
        assert status == 201, entry
        assert entry["occurred_utc"] == "2025-09-01T03:00:00Z"
        status, entries = call(base, "GET", "/entries")
        assert [e["occurred_utc"] for e in entries] == ["2025-09-01T03:00:00Z"]
        due = due_at(base, "2026-09-15T00:00:00Z", cues=True)
        (annual,) = [i for i in due["items"] if i["subject"] == "the annual inspection"]
        assert (annual["state"], annual["message"]) == ("overdue", expected)
        assert annual["cue"] == "annual insp 15 days overdue"
        assert due["status"] == "unserviceable"
        status, row = call(base, "GET", "/aircraft/X-1?as_of=2026-09-15T00%3A00%3A00Z")
        assert (status, row["status"], row["status_reasons"]) == (200, "unserviceable", [expected])
        status, pack = call(base, "GET", "/aircraft/X-1/evidence?as_of=2026-09-15T00%3A00%3A00Z")
        assert status == 200, pack
        assert pack["programme"]["status_reasons"] == [expected]
    assert column(tmp_path / "fleet.sqlite") == [
        ("2025-08-31T22:00:00-05:00", "2025-08-31T22:00:00-05:00")
    ]


# ---- guards: green on main and after ---------------------------------------------------------


GUARD = "the annual inspection of aircraft X-1 was due by {due} and is 15 days overdue"


# East of UTC early on a month's first day (#149): the earlier date is the UTC date, so these
# read as main reads them, with no clause. Z, +00:00, no offset and a mid-month west entry
# (2025-08-15 as entered, 2025-08-16 in UTC: both fall due by 2026-08-31) read as before.
@pytest.mark.parametrize(
    ("at", "as_of", "due"),
    [
        ("2025-09-01T02:00:00+05:00", "2026-09-15T00:00Z", "2026-08-31"),
        ("2026-01-01T02:00:00+05:00", "2027-01-15T00:00Z", "2026-12-31"),
        ("2024-03-01T02:00:00+05:00", "2025-03-15T00:00Z", "2025-02-28"),
        ("2025-09-01T09:00:00+14:00", "2026-09-15T00:00Z", "2026-08-31"),
        ("2025-08-31T22:00:00Z", "2026-09-15T00:00Z", "2026-08-31"),
        ("2025-08-31T22:00:00+00:00", "2026-09-15T00:00Z", "2026-08-31"),
        ("2025-08-31T22:00:00", "2026-09-15T00:00Z", "2026-08-31"),
        ("2025-08-15T22:00:00-05:00", "2026-09-15T00:00Z", "2026-08-31"),
    ],
)
def test_an_entry_whose_earlier_date_is_its_utc_date_reads_as_before(
    tmp_path: Path, at: str, as_of: str, due: str
) -> None:
    annual_store(tmp_path, at)
    prog = programme(tmp_path, as_of)
    annual = calendar_item(prog, "the annual inspection")
    expected = GUARD.format(due=due)
    assert (annual["state"], annual["remaining"], annual["message"]) == ("overdue", -15.0, expected)
    assert prog["status"] == "unserviceable"


# ---- the end of a calendar interval: east of UTC early on a month's first day -------------


# Each read 0 days left, serviceable, on main.
@pytest.mark.parametrize(
    ("since", "annual", "fitted", "as_of", "ended", "given", "utc"),
    [
        ("2024-08-15", "2026-06-01T00:00Z", "2025-01-01T00:00Z", "2026-09-01T02:00:00+05:00",
         "2026-08-31", "2026-09-01", "2026-08-31"),
        ("2022-02-15", "2023-12-01T12:00Z", "2023-01-01T00:00Z", "2024-03-01T02:00:00+05:00",
         "2024-02-29", "2024-03-01", "2024-02-29"),
        ("2023-12-15", "2025-10-01T12:00Z", "2024-01-01T00:00Z", "2026-01-01T02:00:00+05:00",
         "2025-12-31", "2026-01-01", "2025-12-31"),
        ("2024-08-15", "2026-06-01T00:00Z", "2025-01-01T00:00Z", "2026-09-01T09:00:00+14:00",
         "2026-08-31", "2026-09-01", "2026-08-31"),
    ],
)  # fmt: skip
def test_a_battery_asked_east_of_utc_on_the_next_day_is_past_its_calendar_life(
    tmp_path: Path,
    since: str,
    annual: str,
    fitted: str,
    as_of: str,
    ended: str,
    given: str,
    utc: str,
) -> None:
    battery_store(tmp_path, since, annual, fitted)
    prog = programme(tmp_path, as_of)
    battery = calendar_item(prog, "battery pack BAT-1")
    expected = battery_past(ended, given, utc)
    assert (battery["state"], battery["remaining"]) == ("overdue", -1.0)
    assert battery["message"] == expected
    assert prog["status"] == "unserviceable"
    assert prog["status_reasons"][0] == expected


# The same through a real `uasw serve`: the due list, its cue and the board; as_of echoed in UTC.
def test_the_service_counts_a_battery_to_the_later_date_of_as_of(tmp_path: Path) -> None:
    battery_store(tmp_path, "2024-08-15", "2026-06-01T00:00Z", "2025-01-01T00:00Z")
    expected = battery_past("2026-08-31", "2026-09-01", "2026-08-31")
    with serving(tmp_path) as base:
        due = due_at(base, "2026-09-01T02:00:00+05:00", cues=True)
        assert due["as_of"] == "2026-08-31T21:00:00Z"
        (life,) = [i for i in due["items"] if i["subject"] == "battery pack BAT-1"
                   and i["basis"] == "calendar"]  # fmt: skip
        assert (life["state"], life["remaining"], life["message"]) == ("overdue", -1.0, expected)
        assert life["cue"] == "BAT-1 past calendar limit 2026-08-31"
        assert due["status"] == "unserviceable"
        query = urllib.parse.urlencode({"as_of": "2026-09-01T02:00:00+05:00"})
        status, row = call(base, "GET", f"/aircraft/X-1?{query}")
        assert (status, row["status"], row["status_reasons"]) == (200, "unserviceable", [expected])


# ---- the ledger's check at an install ------------------------------------------------------


# Each was accepted on main.
@pytest.mark.parametrize(
    ("since", "at", "stamp", "ended", "given", "utc"),
    [
        ("2024-08-15", "2026-09-01T02:00:00+05:00", "2026-08-31 21:00 UTC", "2026-08-31",
         "2026-09-01", "2026-08-31"),
        ("2022-02-15", "2024-03-01T02:00:00+05:00", "2024-02-29 21:00 UTC", "2024-02-29",
         "2024-03-01", "2024-02-29"),
        ("2023-12-15", "2026-01-01T02:00:00+05:00", "2025-12-31 21:00 UTC", "2025-12-31",
         "2026-01-01", "2025-12-31"),
        ("2024-08-15", "2026-09-01T09:00:00+14:00", "2026-08-31 19:00 UTC", "2026-08-31",
         "2026-09-01", "2026-08-31"),
    ],
)  # fmt: skip
def test_an_install_on_a_date_as_entered_after_the_life_ended_is_refused(
    tmp_path: Path, since: str, at: str, stamp: str, ended: str, given: str, utc: str
) -> None:
    ok(tmp_path, "ingest", "X-1", str(LOG))
    registered(tmp_path, since)
    r = uasw(tmp_path, "record", "component", "install", "BAT-1", "X-1", *WHO, "--at", at)
    assert r.returncode == 1
    assert r.stderr == f"refused (409): {install_refused(stamp, ended, given, utc)}\n"
    assert len(ok(tmp_path, "record", "history", "BAT-1").splitlines()) == 1  # the register


def test_the_service_refuses_an_install_on_a_date_as_entered_after_the_life_ended(
    tmp_path: Path,
) -> None:
    ok(tmp_path, "ingest", "X-1", str(LOG))
    registered(tmp_path, "2024-08-15")
    body = {
        "subject": "BAT-1",
        "kind": "component.install",
        "occurred_utc": "2026-09-01T02:00:00+05:00",
        "entered_by": "A. Tester, maintenance",
        "statement": "work done as described",
        "details": {"aircraft_key": "X-1"},
    }
    with serving(tmp_path) as base:
        status, answer = call(base, "POST", "/entries", body)
    assert status == 409
    assert answer["detail"] == install_refused(
        "2026-08-31 21:00 UTC", "2026-08-31", "2026-09-01", "2026-08-31"
    )
    assert len(column(tmp_path / "fleet.sqlite")) == 1


# Guards: a west install whose UTC date is after the life is refused as on main (its later
# date is its UTC date, and the sentence is main's); one within the life by both dates is
# accepted, and its installation is shown in UTC.
def test_an_install_refused_by_its_utc_date_reads_as_before(tmp_path: Path) -> None:
    ok(tmp_path, "ingest", "X-1", str(LOG))
    registered(tmp_path, "2024-08-15")
    r = uasw(tmp_path, "record", "component", "install", "BAT-1", "X-1", *WHO,
             "--at", "2026-08-31T14:00:00-11:00")  # fmt: skip
    assert r.returncode == 1
    assert r.stderr == (
        "refused (409): BAT-1 cannot be fitted to X-1 at 2026-09-01 01:00 UTC: its "
        f"24-calendar-month life limit ended on 2026-08-31; {NOT_FITTED_AGAIN}\n"
    )


def test_an_install_within_the_life_keeps_its_time_and_is_shown_in_utc(tmp_path: Path) -> None:
    ok(tmp_path, "ingest", "X-1", str(LOG))
    registered(tmp_path, "2024-08-15")
    ok(tmp_path, "record", "component", "install", "BAT-1", "X-1", *WHO,
       "--at", "2026-08-31T09:00:00+05:00")  # fmt: skip
    assert column(tmp_path / "fleet.sqlite")[-1][0] == "2026-08-31T09:00:00+05:00"
    history = ok(tmp_path, "record", "history", "BAT-1").splitlines()
    assert history[-1].startswith("#2 2026-08-31 04:00 component.install by A. Tester")
    with serving(tmp_path) as base:
        status, part = call(base, "GET", "/components/BAT-1")
    assert status == 200, part
    assert [i["from_utc"] for i in part["installations"]] == ["2026-08-31T04:00:00Z"]


# A register entry's own time does not move a calendar life: the life counts from the date
# given as --since, which carries no offset.
def test_a_register_entry_given_west_late_on_a_months_last_day_moves_no_calendar_life(
    tmp_path: Path,
) -> None:
    ok(tmp_path, "ingest", "X-1", str(LOG))
    ok(tmp_path, "record", "inspection", "X-1", "annual inspection", "--at-hours", "0", *WHO,
       "--at", "2026-06-01T00:00Z")  # fmt: skip
    registered(tmp_path, "2024-08-15", at="2024-08-31T22:00:00-05:00")
    ok(tmp_path, "record", "component", "install", "BAT-1", "X-1", *WHO,
       "--at", "2025-01-01T00:00Z")  # fmt: skip
    prog = programme(tmp_path, "2026-08-31T12:00Z")
    battery = calendar_item(prog, "battery pack BAT-1")
    assert (battery["state"], battery["remaining"]) == ("due_soon", 0.0)


# ---- old stores: read, never rewritten -------------------------------------------------------


def _annual_entry(at: datetime) -> Entry:
    return Entry(
        id=None,
        subject="X-1",
        kind="inspection.done",
        occurred_utc=at,
        recorded_utc=datetime(2025, 9, 1, 3, 5, tzinfo=UTC),
        entered_by="A. Tester, maintenance",
        statement="work done as described",
        details={"name": "annual inspection", "at_hours_s": 0.0, "carried_over_s": 0.0},
        supersedes=None,
        reason=None,
        synthetic=False,
    )


MINUS5 = timezone(timedelta(hours=-5))
PLUS5 = timezone(timedelta(hours=5))


# A store written by 0.6.6 holds the time as given: read at the bound, as 0.6.6 read it
# (main read it due by 2026-09-30, in 15 days, serviceable); no row and no journal link moves.
def test_a_0_6_6_store_holding_a_west_entry_reads_it_from_its_date_as_entered(
    tmp_path: Path,
) -> None:
    ok(tmp_path, "ingest", "X-1", str(LOG))
    db = tmp_path / "fleet.sqlite"
    write_as_0_6_6(db, _annual_entry(datetime(2025, 8, 31, 22, 0, tzinfo=MINUS5)))
    before, tables = ok(tmp_path, "verify"), table_hashes(db)
    assert before.startswith("journal intact: 2 links"), before
    prog = programme(tmp_path, "2026-09-15T00:00Z")
    annual = calendar_item(prog, "the annual inspection")
    expected = annual_overdue("2026-08-31", "2025-08-31", "2025-09-01")
    assert (annual["state"], annual["message"]) == ("overdue", expected)
    assert prog["status"] == "unserviceable"
    (line,) = ok(tmp_path, "record", "history", "X-1").splitlines()
    assert line.startswith("#1 2025-09-01 03:00 inspection.done")
    assert ok(tmp_path, "verify") == before
    assert table_hashes(db) == tables
    assert column(db) == [("2025-08-31T22:00:00-05:00", "2025-08-31T22:00:00-05:00")]


# A row main wrote since #149 holds the UTC time alone: nothing tells it from an entry given
# in UTC, so it reads as main reads it. A correction giving the time with its offset as
# entered (the same instant) is accepted, through the command line and the service, and the
# item then counts from the date as entered.
@pytest.mark.parametrize("way", ["cli", "http"])
def test_a_row_main_wrote_in_utc_is_repaired_by_a_correction_giving_its_offset(
    tmp_path: Path, way: str
) -> None:
    ok(tmp_path, "ingest", "X-1", str(LOG))
    db = tmp_path / "fleet.sqlite"
    write_as_0_6_6(db, _annual_entry(datetime(2025, 9, 1, 3, 0, tzinfo=UTC)))  # as main wrote it
    annual = calendar_item(programme(tmp_path, "2026-09-15T00:00Z"), "the annual inspection")
    assert (annual["state"], annual["message"]) == (
        "due_soon",
        "the annual inspection of aircraft X-1 is due by 2026-09-30, in 15 days",
    )
    at = "2025-08-31T22:00:00-05:00"
    reason = "the inspection was done on 2025-08-31 at 22:00 local time, UTC-05:00"
    if way == "cli":
        ok(tmp_path, "record", "inspection", "X-1", "annual inspection", "--at-hours", "0", *WHO,
           "--at", at, "--supersedes", "1", "--reason", reason)  # fmt: skip
    else:
        with serving(tmp_path) as base:
            status, entry = call(
                base,
                "POST",
                "/entries",
                {**INSPECTION, "occurred_utc": at, "supersedes": 1, "reason": reason},
            )
        assert status == 201, entry
        assert entry["occurred_utc"] == "2025-09-01T03:00:00Z"
    prog = programme(tmp_path, "2026-09-15T00:00Z")
    annual = calendar_item(prog, "the annual inspection")
    expected = annual_overdue("2026-08-31", "2025-08-31", "2025-09-01")
    assert (annual["state"], annual["message"]) == ("overdue", expected)
    assert prog["status"] == "unserviceable"
    assert column(db)[0] == ("2025-09-01T03:00:00+00:00", "2025-09-01T03:00:00+00:00")
    assert column(db)[1] == (at, at)


# ---- the engine, the cue, the assistant and the export, on their own ------------------------


def test_the_engine_counts_a_start_from_the_earlier_date_and_an_end_to_the_later() -> None:
    done = datetime(2025, 8, 31, 22, 0, tzinfo=MINUS5)  # 2025-09-01 03:00 UTC
    rec = record("A", inspections=(InspectionDone("annual inspection", done, 0.0),))
    due = compute("A", rec, as_of=datetime(2026, 9, 15, tzinfo=UTC))
    (annual,) = [i for i in due.items if i.subject == "the annual inspection"]
    assert (annual.remaining, annual.state) == (-15.0, "overdue")
    fitted = datetime(2025, 1, 1, tzinfo=UTC)
    bat = Component("BAT-1", "battery pack", date(2024, 8, 15), 0.0, 0,
                    (Installation("A", fitted, None),), True)  # fmt: skip
    as_of = datetime(2026, 9, 1, 2, 0, tzinfo=PLUS5)  # 2026-08-31 21:00 UTC
    due = compute("A", record("A"), components=(bat,), as_of=as_of)
    (life,) = [i for i in due.items if i.component_id == "BAT-1" and i.basis == "calendar"]
    assert (life.remaining, life.state) == (-1.0, "overdue")
    assert due.as_of == datetime(2026, 8, 31, 21, 0, tzinfo=UTC)
    assert item_cue(life, due.day or due.as_of) == "BAT-1 past calendar limit 2026-08-31"


def test_the_assistant_sends_as_of_with_the_offset_it_was_given() -> None:
    _, query = resolve("aircraft_due", {"key": "X-1"}, datetime(2026, 9, 1, 2, 0, tzinfo=PLUS5))
    assert query["as_of"] == "2026-09-01T02:00:00+05:00"
    _, query = resolve("aircraft_due", {"key": "X-1"}, datetime(2026, 9, 1, 2, 0, tzinfo=UTC))
    assert query["as_of"] == "2026-09-01T02:00:00Z"


class _Frozen(datetime):
    @classmethod
    def now(cls, tz: tzinfo | None = None) -> datetime:
        return datetime(2026, 9, 15, tzinfo=tz or UTC)


# The static export reads the store at its own now (frozen here): the west entry is listed in
# UTC and counted from its date as entered, in its data and its page.
def test_the_static_export_counts_a_west_entry_from_its_date_as_entered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    annual_store(tmp_path, "2025-08-31T22:00:00-05:00")
    monkeypatch.setattr(static_export, "datetime", _Frozen)
    monkeypatch.setattr(app_module, "datetime", _Frozen)
    store = Store(str(tmp_path / "fleet.sqlite"))
    try:
        store.add_fleet(generate(load_config()))  # the export's sample pack is SYN-04's
        out = export_static(store, tmp_path / "site", load_config())
    finally:
        store.close()
    fleet = json.loads((out / "fleet.json").read_text(encoding="utf-8"))
    (x1,) = [a for a in fleet["aircraft"] if a["key"] == "X-1"]
    expected = annual_overdue("2026-08-31", "2025-08-31", "2025-09-01")
    assert (x1["status"], x1["status_reasons"]) == ("unserviceable", [expected])
    (annual,) = [i for i in x1["due"]["items"] if i["subject"] == "the annual inspection"]
    assert annual["message"] == expected
    assert [e["occurred_utc"] for e in x1["entries"]] == ["2025-09-01T03:00:00Z"]
