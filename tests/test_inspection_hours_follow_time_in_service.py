"""An inspection's hours follow the time in service, whatever order the records arrive in
(issue #151).

An inspection stores the time in service it was done at, stated or derived, and the next
interval is today's time in service minus that figure. Two ways let the two part company.

A. The hours before the first log are one total, and a later entry of them replaced it for
every date, but was checked only against the entries that fold after it. An inspection whose
hours were taken from the old total was never checked again: 200 h entered, a 100-hour
inspection derived at 210 h, then a new entry of 0 h ("another airframe's") was accepted, and
the item read "due in 205.0 h" where 95 h had been flown since. Now any entry that changes the
hours before the first log is refused while an inspection would then state more hours than
the aircraft had at its date, as the correction of the old entry already was; and an
inspection stating more than a later entry of those hours allows is refused too. A store
written before this holds such records: the item reads not known, saying the records
disagree, and never a number.

B. A log with no UTC start counts at any date, so an inspection that derived its hours counted
it before itself when it was uploaded first and after itself when it came later: the same
records gave "15.0 h left" or "5.0 h overdue, within tolerance". Now derived hours count dated
logs only; an undated log's hours fall inside the interval after the inspection, the most they
can add, and a note names the log the records cannot place. A store written before this can
hold an inspection whose derived hours counted the undated log: the tool's own figure is then
more than the dated logs give, and the item reads not known, saying so.

Every row runs real `uasw` processes, each store of its own, with DataFlash logs built here:
a GPS fix gives the UTC start, STAT gives the flight time.
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from test_life_rules import T0, compute, flight, item, record
from test_utc_offsets import call, stop, write_as_0_6_6

from builders.dataflash import DataFlashBuilder
from uas_workbench.assistant.agent import service_caller
from uas_workbench.assistant.tools import resolve
from uas_workbench.flight.ardupilot import GPS_EPOCH, GPS_MINUS_UTC
from uas_workbench.ledger.model import Entry
from uas_workbench.life import InspectionDone, board
from uas_workbench.life.cue import item_cue, problems, status_cue

FIXTURES = Path(__file__).parent / "fixtures"
WHO = ["--by", "A. Tester, maintenance", "--statement", "work done as described"]
UASW = [sys.executable, "-m", "uas_workbench.service.cli", "--db", "fleet.sqlite"]
US = 1_000_000
H = 3600.0
HUNDRED = "the 100-hour inspection"

REFUSED_LATER_HOURS = (
    "refused (409): this would make entry 3 impossible (inspection.done on X-1, "
    "2026-03-01 00:00 UTC): at_hours_s 210.0 h is more than the time in service of X-1 at "
    "2026-03-01 00:00 UTC, 10.0 h; retract or correct entry 3 first"
)
REFUSED_EARLIER_INSPECTION = (
    "refused (409): at_hours_s 210.0 h is more than the time in service of X-1 at "
    "2026-03-01 00:00 UTC, 10.0 h, counting the 0.0 h before the first log that entry 3 "
    "records; state fewer hours or correct entry 3 first"
)
DISAGREE = (
    "the records disagree: the 100-hour inspection of 2026-03-01 is recorded at 210.0 h, more "
    "than the 10.0 h time in service the records give by then, counting 0.0 h before the first "
    "log; correct the inspection or the hours before the first log"
)
DERIVED = (
    "the records disagree: the 100-hour inspection of 2026-03-01 is recorded at 30.0 h, derived, "
    "more than the 10.0 h the dated logs give by then: the difference can only be logs with no "
    "UTC start, which the records cannot place before it; correct the inspection, stating its "
    "hours"
)
CANNOT_MEASURE = "the 100-hour inspection of aircraft {key} cannot be measured on 2026-07-01: "
DUE_IN_5 = "the 100-hour inspection of aircraft X-1 is due in 5.0 h"
OVERDUE_5 = (
    "the 100-hour inspection of aircraft {key} is 5.0 h overdue, within the 10 h tolerance "
    "that allows flight only to reach a place where the inspection can be done"
)
UNPLACED = (
    "flight no-gps-fix of aircraft {key} has no UTC start, so the records cannot place it "
    "before or after the 100-hour inspection of 2026-03-01; its 20.0 h count in the time in "
    "service at any date"
)


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


def refused(cwd: Path, *args: str) -> str:
    """The `refused (...)` line of a command that must be refused, nothing written."""
    r = uasw(cwd, *args)
    assert r.returncode == 1, r.stdout + r.stderr
    (line,) = [s for s in r.stderr.splitlines() if s.startswith("refused (")]
    return line


def log(path: Path, start: datetime | None, hours: float) -> Path:
    """A DataFlash log flying `hours` from `start`; with no start, no GPS fix at all. STAT
    carries only the columns the reader takes."""
    b = (
        DataFlashBuilder()
        .define("MSG", "QZ", "TimeUS,Message")
        .define("STAT", "QBBB", "TimeUS,isFlying,Crash,Hit")
        .define("GPS", "QBIHf", "TimeUS,Status,GMS,GWk,Spd")
    )
    b.msg("MSG", US, "ArduPlane V4.5.7 (1234abcd)")
    if start is None:
        b.msg("GPS", US, 1, 0, 0, 0.0)
    else:
        week, into = divmod(start - GPS_EPOCH + GPS_MINUS_UTC, timedelta(weeks=1))
        b.msg("GPS", US, 3, into // timedelta(milliseconds=1), week, 0.0)
    b.msg("STAT", US, 1, 0, 0)
    b.msg("STAT", US + round(hours * H * US), 0, 0, 0)
    return b.write(path)


def ingest(cwd: Path, key: str, name: str, start: datetime | None, hours: float) -> None:
    ok(cwd, "ingest", key, str(log(cwd / f"{name}.bin", start, hours)))


def inspection(cwd: Path, key: str, name: str, at: str, *more: str) -> None:
    ok(cwd, "record", "inspection", key, name, *WHO, "--at", at, *more)


def case_a(cwd: Path, key: str = "X-1") -> None:
    """Issue #151 case A up to the later hours entry: 200 h before the first log, 10 h flown
    on 2026-02-01, the 100-hour inspection derived at 210 h on 2026-03-01 (entry 3), 95 h
    flown on 2026-04-01. An annual inspection, derived at 0 h before any entry of hours,
    keeps the board known."""
    ingest(cwd, key, f"{key}-feb", datetime(2026, 2, 1, tzinfo=UTC), 10.0)
    inspection(cwd, key, "annual inspection", "2025-12-15T00:00Z")
    ok(cwd, "record", "time-in-service", key, "200", *WHO, "--at", "2026-01-01T00:00Z")
    inspection(cwd, key, "100-hour inspection", "2026-03-01T00:00Z")
    ingest(cwd, key, f"{key}-apr", datetime(2026, 4, 1, tzinfo=UTC), 95.0)


def case_b(cwd: Path, order: str, key: str = "X-1") -> None:
    """Issue #151 case B: 10 h flown on 2026-02-01, a log of 20 h with no UTC start uploaded
    `order` the 100-hour inspection (derived, 2026-03-01), 85 h flown on 2026-04-01. An
    annual inspection on 2026-01-15, derived at 0 h, keeps the board known."""
    ingest(cwd, key, f"{key}-feb", datetime(2026, 2, 1, tzinfo=UTC), 10.0)
    inspection(cwd, key, "annual inspection", "2026-01-15T00:00Z")
    if order == "before":
        ok(cwd, "ingest", key, str(log(cwd / "no-gps-fix.bin", None, 20.0)))
    inspection(cwd, key, "100-hour inspection", "2026-03-01T00:00Z")
    if order == "after":
        ok(cwd, "ingest", key, str(log(cwd / "no-gps-fix.bin", None, 20.0)))
    ingest(cwd, key, f"{key}-apr", datetime(2026, 4, 1, tzinfo=UTC), 85.0)


def programme(cwd: Path, key: str, as_of: str) -> dict[str, Any]:
    ok(cwd, "evidence", key, "--out", "pack.html", "--json", "--as-of", as_of)
    pack = json.loads((cwd / "pack.json").read_text(encoding="utf-8"))
    out: dict[str, Any] = pack["programme"]
    return out


def hundred(items: list[dict[str, Any]]) -> dict[str, Any]:
    (one,) = [i for i in items if i["subject"] == HUNDRED and i["basis"] == "hours"]
    return one


def figures(item: dict[str, Any]) -> tuple[Any, ...]:
    return (item["used"], item["remaining"], item["state"], item["message"])


def rows(db: Path) -> list[tuple[Any, ...]]:
    c = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        return [
            *c.execute("SELECT * FROM entries ORDER BY id"),
            *c.execute("SELECT * FROM journal ORDER BY 1"),
        ]
    finally:
        c.close()


def derived_hours(db: Path, name: str) -> list[float]:
    c = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        found = c.execute("SELECT record FROM entries WHERE kind = 'inspection.done'").fetchall()
    finally:
        c.close()
    details = [json.loads(r)["details"] for (r,) in found]
    return [d["at_hours_s"] for d in details if d["name"] == name and d.get("derived")]


def as_0_6_6_later_hours(db: Path, key: str) -> None:
    """The later hours entry of case A, 0 h on 2026-06-01, as 0.6.6 accepted and wrote it."""
    write_as_0_6_6(
        db,
        Entry(
            id=None,
            subject=key,
            kind="time_in_service.set",
            occurred_utc=datetime(2026, 6, 1, tzinfo=UTC),
            recorded_utc=datetime(2026, 6, 1, 0, 5, tzinfo=UTC),
            entered_by="A. Tester, maintenance",
            statement="the 200 h were another airframe's",
            details={"before_s": 0.0},
            supersedes=None,
            reason=None,
            synthetic=False,
        ),
    )


# ---- case A: a later entry of the hours before the first log ------------------------------


# The new entry of 0 h was accepted and the item read due in 205.0 h. It is refused as the
# correction of the old entry is, naming the inspection; nothing is written, and the item
# reads what the records hold: 305 h now, 210 h at the inspection, 5.0 h left, due soon.
def test_a_later_hours_entry_below_an_inspection_is_refused_naming_it(tmp_path: Path) -> None:
    case_a(tmp_path)
    before = rows(tmp_path / "fleet.sqlite")
    line = refused(tmp_path, "record", "time-in-service", "X-1", "0", *WHO,
                   "--at", "2026-06-01T00:00Z")  # fmt: skip
    assert line == REFUSED_LATER_HOURS
    assert rows(tmp_path / "fleet.sqlite") == before
    prog = programme(tmp_path, "X-1", "2026-07-01T00:00Z")
    assert figures(hundred(prog["items"])) == (95.0, 5.0, "due_soon", DUE_IN_5)
    assert prog["status"] == "serviceable"


# The way through: correct the inspection first, then the hours. The figures then follow the
# new total: 105 h now, 10 h at the inspection, 5.0 h left. (Passes on 0.6.6 too: the refusal
# above must not block it.)
def test_the_inspection_corrected_first_lets_the_hours_change(tmp_path: Path) -> None:
    case_a(tmp_path)
    inspection(tmp_path, "X-1", "100-hour inspection", "2026-03-01T00:00Z", "--at-hours", "10",
               "--supersedes", "3", "--reason", "the 200 h were another airframe's")  # fmt: skip
    ok(tmp_path, "record", "time-in-service", "X-1", "0", *WHO, "--at", "2026-06-01T00:00Z")
    prog = programme(tmp_path, "X-1", "2026-07-01T00:00Z")
    assert figures(hundred(prog["items"])) == (95.0, 5.0, "due_soon", DUE_IN_5)


# The same disagreement entered the other way round: the later entry of 0 h first, then an
# inspection dated before it, stating or deriving 210 h from the 200 h it replaced.
@pytest.mark.parametrize("hours", [["--at-hours", "210"], []], ids=["stated", "derived"])
def test_an_inspection_above_a_later_hours_entry_is_refused(
    tmp_path: Path, hours: list[str]
) -> None:
    ingest(tmp_path, "X-1", "X-1-feb", datetime(2026, 2, 1, tzinfo=UTC), 10.0)
    inspection(tmp_path, "X-1", "annual inspection", "2025-12-15T00:00Z")
    ok(tmp_path, "record", "time-in-service", "X-1", "200", *WHO, "--at", "2026-01-01T00:00Z")
    ok(tmp_path, "record", "time-in-service", "X-1", "0", *WHO, "--at", "2026-06-01T00:00Z")
    before = rows(tmp_path / "fleet.sqlite")
    line = refused(tmp_path, "record", "inspection", "X-1", "100-hour inspection", *WHO,
                   "--at", "2026-03-01T00:00Z", *hours)  # fmt: skip
    assert line == REFUSED_EARLIER_INSPECTION
    assert rows(tmp_path / "fleet.sqlite") == before


# A store written by 0.6.6 holds the later entry. It loads, its journal verifies with the same
# head, no row is rewritten, and the item reads not known, saying the records disagree, never
# "due in 205.0 h"; the board is not known with that sentence.
def test_a_store_holding_the_disagreement_reads_not_known(tmp_path: Path) -> None:
    case_a(tmp_path)
    db = tmp_path / "fleet.sqlite"
    as_0_6_6_later_hours(db, "X-1")
    head = ok(tmp_path, "verify")
    assert head.startswith("journal intact: 6 links"), head  # 2 logs, 3 entries, 0.6.6's row
    before = rows(db)
    prog = programme(tmp_path, "X-1", "2026-07-01T00:00Z")
    item = hundred(prog["items"])
    message = CANNOT_MEASURE.format(key="X-1") + DISAGREE
    assert figures(item) == ({"unknown": DISAGREE}, {"unknown": DISAGREE}, "unknown", message)
    assert prog["status"] == {"unknown": message}
    assert ok(tmp_path, "verify") == head
    assert rows(db) == before


# ---- case B: a log with no UTC start -------------------------------------------------------


# The same records in either upload order: the inspection's hours count the dated 10 h only,
# the undated 20 h fall inside the interval, and a note names the log the records cannot
# place. 0.6.6 gave 30 h and "15.0 h left" when the undated log came first.
@pytest.mark.parametrize("order", ["before", "after"])
def test_an_undated_log_gives_one_answer_whatever_the_upload_order(
    tmp_path: Path, order: str
) -> None:
    case_b(tmp_path, order)
    assert derived_hours(tmp_path / "fleet.sqlite", "100-hour inspection") == [10.0 * H]
    prog = programme(tmp_path, "X-1", "2026-05-01T00:00Z")
    item = hundred(prog["items"])
    assert figures(item) == (105.0, -5.0, "overdue_within_tolerance", OVERDUE_5.format(key="X-1"))
    assert prog["status"] == "serviceable with deferred defects"
    assert UNPLACED.format(key="X-1") in prog["notes"]


# A store written by 0.6.6 with the undated log uploaded first holds the inspection derived
# at 30 h, as 0.6.6 wrote it. Its row is kept, so the figure cannot be put right by reading;
# "due in 15.0 h" would still be shown. The tool's own figure is more than the dated logs
# give, which only the undated log explains: the item reads not known, and says so.
def test_a_store_whose_derived_hours_counted_an_undated_log_reads_not_known(
    tmp_path: Path,
) -> None:
    ingest(tmp_path, "X-1", "X-1-feb", datetime(2026, 2, 1, tzinfo=UTC), 10.0)
    inspection(tmp_path, "X-1", "annual inspection", "2026-01-15T00:00Z")
    ok(tmp_path, "ingest", "X-1", str(log(tmp_path / "no-gps-fix.bin", None, 20.0)))
    db = tmp_path / "fleet.sqlite"
    write_as_0_6_6(
        db,
        Entry(
            id=None,
            subject="X-1",
            kind="inspection.done",
            occurred_utc=datetime(2026, 3, 1, tzinfo=UTC),
            recorded_utc=datetime(2026, 3, 1, 0, 5, tzinfo=UTC),
            entered_by="A. Tester, maintenance",
            statement="work done as described",
            details={
                "name": "100-hour inspection",
                "at_hours_s": 30.0 * H,
                "carried_over_s": 0.0,
                "derived": True,
            },
            supersedes=None,
            reason=None,
            synthetic=False,
        ),
    )
    ingest(tmp_path, "X-1", "X-1-apr", datetime(2026, 4, 1, tzinfo=UTC), 85.0)
    head = ok(tmp_path, "verify")
    before = rows(db)
    prog = programme(tmp_path, "X-1", "2026-05-01T00:00Z")
    message = "the 100-hour inspection of aircraft X-1 cannot be measured on 2026-05-01: " + DERIVED
    assert figures(hundred(prog["items"])) == (
        {"unknown": DERIVED},
        {"unknown": DERIVED},
        "unknown",
        message,
    )
    assert prog["status"] == {"unknown": message}
    assert UNPLACED.format(key="X-1") in prog["notes"]
    assert ok(tmp_path, "verify") == head
    assert rows(db) == before


# ---- one answer on every surface -----------------------------------------------------------


@pytest.fixture
def served(tmp_path: Path) -> Iterator[tuple[Path, str]]:
    """`uasw serve` on a store holding case B (X-1, undated log first) and a case A store
    as 0.6.6 left it (X-2)."""
    case_b(tmp_path, "before", "X-1")
    case_a(tmp_path, "X-2")
    as_0_6_6_later_hours(tmp_path / "fleet.sqlite", "X-2")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = {k: v for k, v in os.environ.items() if k not in ("UASW_WRITE_TOKEN", "UASW_FIXTURES")}
    out = (tmp_path / "serve.log").open("w")
    p = subprocess.Popen(
        [*UASW, "serve", "--port", str(port), "--fixtures", str(FIXTURES)],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=out,
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
        yield tmp_path, base
    finally:
        stop(p)
        out.close()


# The command line's pack, the HTTP due list, the fleet due list and the assistant's
# aircraft_due tool give the same figures and the same board for both aircraft.
def test_the_command_line_the_service_and_the_assistant_agree(served: tuple[Path, str]) -> None:
    cwd, base = served
    tool = service_caller(base)
    for key, as_of in (("X-1", "2026-05-01T00:00:00Z"), ("X-2", "2026-07-01T00:00:00Z")):
        prog = programme(cwd, key, as_of)
        cli = figures(hundred(prog["items"]))
        query = urllib.parse.urlencode({"as_of": as_of})
        status, due = call(base, "GET", f"/aircraft/{key}/due?{query}")
        assert status == 200, due
        status, fleet = call(base, "GET", f"/fleet/due?{query}")
        assert status == 200, fleet
        path, q = resolve("aircraft_due", {"key": key}, datetime.fromisoformat(as_of))
        told = tool(path, q)
        assert figures(hundred(due["items"])) == cli, key
        assert figures(hundred([i for i in fleet if i["aircraft_key"] == key])) == cli, key
        assert figures(hundred(told["items"])) == cli, key
        assert due["status"] == told["status"] == prog["status"], key
    assert cli[2] == "unknown" and cli[3] == CANNOT_MEASURE.format(key="X-2") + DISAGREE
    # The short form says not known and why, with no number of its own.
    query = urllib.parse.urlencode({"as_of": "2026-07-01T00:00:00Z", "cues": "true"})
    status, cued = call(base, "GET", f"/aircraft/X-2/due?{query}")
    assert status == 200, cued
    assert hundred(cued["items"])["cue"] == "100 h insp: records disagree, not known"
    assert cued["status_cue"] == "not known: records disagree"


# The engine on its own: a completion recorded at more hours than there were reads not known,
# and the cue grammar's own checker accepts its cue.
def test_the_cue_of_a_disagreement_passes_the_cue_checker() -> None:
    later = T0 + timedelta(days=1)
    rec = record(
        "A",
        inspections=(
            InspectionDone("100-hour inspection", later, 210.0 * H),
            InspectionDone("annual inspection", T0 - timedelta(days=1), 0.0),
        ),
    )
    due = compute("A", rec, flights={"A": [flight("A", 1, T0, 1800.0)]}, as_of=later)
    hours = item(due, HUNDRED, "hours")
    assert hours.state == "unknown" and hours.message.endswith(
        "the records disagree: the 100-hour inspection of 2026-03-02 is recorded at 210.0 h, more "
        "than the 0.5 h time in service the records give by then, counting 0.0 h before the "
        "first log; correct the inspection or the hours before the first log"
    )
    cue = item_cue(hours, due.as_of)
    assert cue == "100 h insp: records disagree, not known"
    assert problems(cue, hours, due.as_of) == []
    assert status_cue(board(due).status) == "not known: records disagree"
