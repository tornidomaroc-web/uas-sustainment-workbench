"""A log whose flight time is not known counts the most the records allow (issue #152).

A log whose reader cannot say how long the aircraft flew (an ArduPilot log with no STAT
messages, a ULog with no vehicle_land_detected topic) counted one cycle and no hours. Every
hour-based figure it touched read less used than the records allow: a part could look
younger than it may be, and an item that may be due could read not due. Nothing said so.

Its flight lies inside the log, so it is at most the log's span. Each such log now counts its
whole span toward the time in service and toward every part fitted when it started, and the
sentence of each hour item it moves says how many such logs it counts, how much, and the
least the known flight time gives. An inspection's interval counts a log after the
inspection, or with no UTC start, at its whole span; a log before it only as far as the
inspection's hours exceed what the dated logs give at its date, since stated hours already
hold that log's real flight. An inspection's derived hours stay the least the dated logs
give, as before, so no stored figure moves. A log with no UTC start still counts toward no
part, as before (the records cannot place it in a fitting).

The cases, read on 2026-07-01 unless named, each aircraft with an annual inspection on
2026-01-01 and a propeller set registered on 2026-01-01 with 200 h before (fleet.toml's only
hours-limited kind is the 300 h propeller set, so 200 h before leaves a 100 h life, as in the
cases of the fix PR):
  C1  X-1: P-1 fitted 2026-01-02; 50 h known (02-01), a log of unknown flight time spanning
      2 h (03-10), 45 h known (04-01); 100-hour inspection derived on 03-01. Main: "has 5.0 h
      left". Now 3.0 h left, at least 295.0 h used, at most 297.0 h.
  C2  X-2: as C1 with 49 h in April. Main: "has 1.0 h left", serviceable. Now 1.0 h past at
      most, unserviceable; refitting P-1 is refused; fitting another part is the route.
  C3  X-3: as C1 with the 2 h log carrying no UTC start. The time in service counts it at any
      date and the 100-hour interval holds it; P-3 does not count it, as before, with a note.
  C4  X-4: the 2 h log on 03-10, P-4 fitted 04-01, 50 h known on 04-02: P-4 reads as before.
  I1  issue #152's own case: a PX4 log of 3600 s and unknown flight time on 2026-02-01, a
      propeller set at 299.5 h and a battery pack at 299 cycles, read on 2026-03-01.

Operator-facing cases run real `uasw` processes; the many small shapes of fitting windows and
ledger checks run through the library in one process (the same functions `uasw` and the
service call: life.engine.due_list and ledger.append).
"""

from __future__ import annotations

import dataclasses
import json
import os
import socket
import subprocess
import time
import urllib.parse
import urllib.request
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from test_inspection_hours_follow_time_in_service import (
    FIXTURES,
    UASW,
    WHO,
    figures,
    hundred,
    ingest,
    inspection,
    ok,
    programme,
    refused,
    rows,
)
from test_life_rules import POLICY, flight, item
from test_utc_offsets import call, stop, write_as_0_6_6

from builders.dataflash import DataFlashBuilder
from builders.ulog import ULogBuilder
from uas_workbench.assistant.agent import service_caller
from uas_workbench.assistant.tools import resolve
from uas_workbench.fleet import load_config
from uas_workbench.fleet.model import Aircraft
from uas_workbench.flight import FlightRecord, Unknown
from uas_workbench.flight.ardupilot import GPS_EPOCH, GPS_MINUS_UTC
from uas_workbench.ledger import Entry, LedgerError, append
from uas_workbench.life import (
    Component,
    InspectionDone,
    Installation,
    MaintenanceRecord,
    board,
    due_list,
)
from uas_workbench.life.cue import item_cue, problems
from uas_workbench.service.store import Store

US = 1_000_000
H = 3600.0
JULY = "2026-07-01T00:00Z"
NOT_STAT = "no STAT messages (ArduPlane logs them; other vehicle types do not)"


def bounded(n: int, x: float, least: float, most: float) -> str:
    """The clause an hour item carries when logs of unknown flight time move it."""
    logs = "1 log" if n == 1 else f"{n} logs"
    return (
        f", counting {logs} whose flight time is not known at the most the records allow "
        f"({x:.1f} h): at least {least:.1f} h used, at most {most:.1f} h"
    )


def span_note(ref: str, key: str, span_h: float) -> str:
    return (
        f"flight {ref} of aircraft {key} has no flight time on record, so the whole "
        f"{span_h:.1f} h span of its log counts as flown, the most the records allow"
    )


# C1, main word for word, and now.
P1_C1_MAIN = "propeller set P-1 on aircraft X-1 has 5.0 h left of its 300 h life limit"
P1_C1 = "propeller set P-1 on aircraft X-1 has 3.0 h left of its 300 h life limit" + bounded(
    1, 2.0, 295.0, 297.0
)
HUNDRED_C1 = "the 100-hour inspection of aircraft X-1 is due in 53.0 h" + bounded(
    1, 2.0, 45.0, 47.0
)
# C2.
P1_C2_MAIN = "propeller set P-1 on aircraft X-2 has 1.0 h left of its 300 h life limit"
P1_C2 = "propeller set P-1 on aircraft X-2 is 1.0 h past its 300 h life limit" + bounded(
    1, 2.0, 299.0, 301.0
)
HUNDRED_C2 = "the 100-hour inspection of aircraft X-2 is due in 49.0 h" + bounded(
    1, 2.0, 49.0, 51.0
)
REFIT = (
    "refused (409): P-1 cannot be fitted to X-2 at 2026-07-01 02:00 UTC: it may have flown "
    "301.0 h, past its 300 h life limit, counting 1 log whose flight time is not known at the "
    "most the records allow (2.0 h): at least 299.0 h flown; a life-limited part that has "
    "reached its life limit is replaced, not fitted again"
)
# C3.
P3_C3 = "propeller set P-3 on aircraft X-3 has 5.0 h left of its 300 h life limit"
HUNDRED_C3 = "the 100-hour inspection of aircraft X-3 is due in 53.0 h" + bounded(
    1, 2.0, 45.0, 47.0
)
NOT_AGAINST = (
    "flight X-3-undated of aircraft X-3 has no UTC start and is not counted against any component"
)
UNPLACED_C3 = (
    "flight X-3-undated of aircraft X-3 has no UTC start, so the records cannot place it before "
    "or after the 100-hour inspection of 2026-03-01; its 2.0 h count in the time in service at "
    "any date"
)
# C4.
P4_C4 = "propeller set P-4 on aircraft X-4 has 50.0 h left of its 300 h life limit"
HUNDRED_C4 = "the 100-hour inspection of aircraft X-4 is due in 48.0 h" + bounded(
    1, 2.0, 50.0, 52.0
)
# I1, issue #152's own case.
PR1_MAIN = "propeller set PR-1 on aircraft X-6 has 0.5 h left of its 300 h life limit"
PR1 = "propeller set PR-1 on aircraft X-6 is 0.5 h past its 300 h life limit" + bounded(
    1, 1.0, 299.5, 300.5
)
BT1 = "battery pack BT-1 on aircraft X-6 has reached its 300-cycle life limit"


def unknown_log(path: Path, start: datetime | None, span_h: float) -> Path:
    """A DataFlash log of `span_h` with no STAT message, so its flight time is not known: a
    GPS message at its start and one at its end give the span; with no start, no GPS fix."""
    b = (
        DataFlashBuilder()
        .define("MSG", "QZ", "TimeUS,Message")
        .define("GPS", "QBIHf", "TimeUS,Status,GMS,GWk,Spd")
    )
    b.msg("MSG", US, "ArduPlane V4.5.7 (1234abcd)")
    if start is None:
        b.msg("GPS", US, 1, 0, 0, 0.0)
    else:
        week, into = divmod(start - GPS_EPOCH + GPS_MINUS_UTC, timedelta(weeks=1))
        b.msg("GPS", US, 3, into // timedelta(milliseconds=1), week, 0.0)
    b.msg("GPS", US + round(span_h * H * US), 1, 0, 0, 0.0)
    return b.write(path)


def unknown_ulog(path: Path, start: datetime, span_s: float) -> Path:
    """A ULog of `span_s` with GPS UTC time and no vehicle_land_detected topic."""
    b = ULogBuilder(start_us=US).format(
        "vehicle_gps_position", ["uint64_t timestamp", "uint64_t time_utc_usec"]
    )
    b.info("sys_uuid", "synthetic-vehicle-0006")
    gps = b.subscribe("vehicle_gps_position")
    utc = round(start.timestamp() * US)
    b.data(gps, {"timestamp": US, "time_utc_usec": utc})
    b.data(gps, {"timestamp": US + round(span_s * US), "time_utc_usec": utc + round(span_s * US)})
    return b.write(path)


def part(cwd: Path, pid: str, *, since: str = "2026-01-01", hours: str = "200",
         kind: str = "propeller set", more: tuple[str, ...] = ()) -> None:  # fmt: skip
    ok(cwd, "record", "component", "register", pid, "--kind", kind, "--since", since,
       "--hours-before", hours, *more, *WHO, "--at", f"{since}T00:00Z")  # fmt: skip


def fit(cwd: Path, pid: str, key: str, at: str) -> None:
    ok(cwd, "record", "component", "install", pid, key, *WHO, "--at", at)


def airframe(cwd: Path, key: str, pid: str, april_h: float, march: datetime | None) -> None:
    """C1 (april 45 h), C2 (49 h) and C3 (`march` None): the logs, the annual, the part and
    the 100-hour inspection derived on 2026-03-01."""
    ingest(cwd, key, f"{key}-feb", datetime(2026, 2, 1, tzinfo=UTC), 50.0)
    name = f"{key}-mar" if march is not None else f"{key}-undated"
    ok(cwd, "ingest", key, str(unknown_log(cwd / f"{name}.bin", march, 2.0)))
    ingest(cwd, key, f"{key}-apr", datetime(2026, 4, 1, tzinfo=UTC), april_h)
    inspection(cwd, key, "annual inspection", "2026-01-01T00:00Z")
    part(cwd, pid)
    fit(cwd, pid, key, "2026-01-02T00:00Z")
    inspection(cwd, key, "100-hour inspection", "2026-03-01T00:00Z")


MARCH = datetime(2026, 3, 10, tzinfo=UTC)


def c2(cwd: Path) -> None:
    airframe(cwd, "X-2", "P-1", 49.0, MARCH)


def c4(cwd: Path) -> None:
    ok(cwd, "ingest", "X-4", str(unknown_log(cwd / "X-4-mar.bin", MARCH, 2.0)))
    ingest(cwd, "X-4", "X-4-apr", datetime(2026, 4, 2, tzinfo=UTC), 50.0)
    inspection(cwd, "X-4", "annual inspection", "2026-01-01T00:00Z")
    part(cwd, "P-4")
    fit(cwd, "P-4", "X-4", "2026-04-01T00:00Z")


def i1(cwd: Path) -> None:
    path = unknown_ulog(cwd / "X-6-feb.ulg", datetime(2026, 2, 1, 10, tzinfo=UTC), 3600.0)
    ok(cwd, "ingest", "X-6", str(path))
    inspection(cwd, "X-6", "annual inspection", "2026-01-01T00:00Z")
    part(cwd, "PR-1", hours="299.5")
    part(cwd, "BT-1", hours="0", kind="battery pack", more=("--cycles-before", "299"))
    fit(cwd, "PR-1", "X-6", "2026-01-01T12:00Z")
    fit(cwd, "BT-1", "X-6", "2026-01-01T12:00Z")


@pytest.fixture(scope="module")
def fleet(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One store holding C1 (X-1), C2 (X-2), C3 (X-3), C4 (X-4) and I1 (X-6), only read."""
    cwd = tmp_path_factory.mktemp("fleet")
    airframe(cwd, "X-1", "P-1", 45.0, MARCH)
    airframe(cwd, "X-2", "P-2", 49.0, MARCH)
    airframe(cwd, "X-3", "P-3", 45.0, None)
    c4(cwd)
    i1(cwd)
    return cwd


def part_item(items: list[dict[str, Any]], pid: str, basis: str = "hours") -> dict[str, Any]:
    (one,) = [i for i in items if i["component_id"] == pid and i["basis"] == basis]
    return one


def tis_h(cwd: Path, key: str, as_of: str = JULY) -> float:
    ok(cwd, "evidence", key, "--out", "pack.html", "--json", "--as-of", as_of)
    pack = json.loads((cwd / "pack.json").read_text(encoding="utf-8"))
    return round(float(pack["usage"]["time_in_service_s"]) / H, 3)


# ---- issue #148's red test, word for word ------------------------------------------------


# A stored record with an unknown flight time and a known span shows on an hour-based item,
# either counted or named, and never silently as 0 h.
def test_148_a_record_of_unknown_flight_time_shows_on_its_hour_items(fleet: Path) -> None:
    prog = programme(fleet, "X-1", JULY)
    hours = [i for i in prog["items"] if i["basis"] == "hours"]
    assert hours and all("whose flight time is not known" in i["message"] for i in hours)
    assert span_note("X-1-mar", "X-1", 2.0) in prog["notes"]
    assert tis_h(fleet, "X-1") == 97.0  # 95 h known and the 2 h span; main read 95.0


# ---- the cases, through real processes ---------------------------------------------------


# C1: main read "has 5.0 h left" (295 h used) and the 100-hour inspection due in 55.0 h.
def test_c1_a_part_reads_the_most_it_can_have_flown(fleet: Path) -> None:
    prog = programme(fleet, "X-1", JULY)
    assert figures(part_item(prog["items"], "P-1")) == (297.0, 3.0, "due_soon", P1_C1)
    assert figures(hundred(prog["items"])) == (47.0, 53.0, "ok", HUNDRED_C1)
    assert prog["status"] == "serviceable"
    assert prog["status_reasons"] == []


# C2: main read "has 1.0 h left" and serviceable. The part may be 1.0 h past its limit: the
# item reads past with the least the known flight gives, and the aircraft is unserviceable.
def test_c2_a_part_that_may_be_past_its_limit_grounds_the_aircraft(fleet: Path) -> None:
    prog = programme(fleet, "X-2", JULY)
    p1 = part_item(prog["items"], "P-2")
    expected = P1_C2.replace("P-1", "P-2")
    assert figures(p1) == (301.0, -1.0, "overdue", expected)
    assert figures(hundred(prog["items"])) == (51.0, 49.0, "ok", HUNDRED_C2)
    assert prog["status"] == "unserviceable"
    assert prog["status_reasons"] == [expected]


# The ledger agrees with the board: the part cannot be fitted again, in a sentence that says
# the figure counts a log at its span. The route is the one the sentence gives: fit another
# part. Main accepted the refit, as the 299.0 h it counted were within the limit.
def test_c2_refitting_the_part_is_refused_and_another_part_is_the_route(tmp_path: Path) -> None:
    c2(tmp_path)
    ok(tmp_path, "record", "component", "remove", "P-1", "X-2", *WHO,
       "--at", "2026-07-01T01:00Z")  # fmt: skip
    before = rows(tmp_path / "fleet.sqlite")
    line = refused(tmp_path, "record", "component", "install", "P-1", "X-2", *WHO,
                   "--at", "2026-07-01T02:00Z")  # fmt: skip
    assert line == REFIT
    assert rows(tmp_path / "fleet.sqlite") == before
    part(tmp_path, "P-9", since="2026-07-01", hours="0")
    fit(tmp_path, "P-9", "X-2", "2026-07-01T02:00Z")
    prog = programme(tmp_path, "X-2", "2026-07-02T00:00Z")
    assert figures(part_item(prog["items"], "P-9")) == (
        0.0,
        300.0,
        "ok",
        "propeller set P-9 on aircraft X-2 has 300.0 h left of its 300 h life limit",
    )
    assert prog["status"] == "serviceable"


# C3: the log has no UTC start. The time in service counts its span at any date and the
# 100-hour interval holds it, the most it can add (#151); P-3 does not count it, as before:
# the records cannot place the log in the part's fitting, and a note says so.
def test_c3_a_log_with_no_utc_start_counts_for_the_airframe_only(fleet: Path) -> None:
    prog = programme(fleet, "X-3", JULY)
    assert figures(part_item(prog["items"], "P-3")) == (295.0, 5.0, "due_soon", P3_C3)
    assert figures(hundred(prog["items"])) == (47.0, 53.0, "ok", HUNDRED_C3)
    assert prog["status"] == "serviceable"
    for note in (NOT_AGAINST, UNPLACED_C3, span_note("X-3-undated", "X-3", 2.0)):
        assert note in prog["notes"]
    assert tis_h(fleet, "X-3") == 97.0


# C4: the log was flown before P-4 was fitted: P-4 reads as on main, with no clause; the
# airframe counts the span.
def test_c4_a_log_before_the_part_was_fitted_leaves_the_part_alone(fleet: Path) -> None:
    prog = programme(fleet, "X-4", JULY)
    assert figures(part_item(prog["items"], "P-4")) == (250.0, 50.0, "ok", P4_C4)
    assert figures(hundred(prog["items"])) == (52.0, 48.0, "ok", HUNDRED_C4)
    assert tis_h(fleet, "X-4") == 52.0


# I1: issue #152's own case, a PX4 log. Main read "has 0.5 h left", due soon.
def test_i1_the_case_of_issue_152(fleet: Path) -> None:
    prog = programme(fleet, "X-6", "2026-03-01T00:00Z")
    assert figures(part_item(prog["items"], "PR-1")) == (300.5, -0.5, "overdue", PR1)
    bt = part_item(prog["items"], "BT-1", "cycles")
    assert (bt["used"], bt["remaining"], bt["state"], bt["message"]) == (300.0, 0.0, "overdue", BT1)
    assert prog["status"] == "unserviceable"
    assert sorted(prog["status_reasons"]) == sorted([PR1, BT1])
    assert span_note("X-6-feb", "X-6", 1.0) in prog["notes"]


# A derived inspection keeps the least the dated logs give: 50 h, as on main, whatever the
# log of unknown flight time, so no stored figure depends on the bound.
def test_derived_hours_count_the_dated_known_flight_only(fleet: Path) -> None:
    from test_inspection_hours_follow_time_in_service import derived_hours

    assert derived_hours(fleet / "fleet.sqlite", "100-hour inspection") == [50.0 * H] * 3


# A stated inspection the bound allows: 50 h known by 03-01 and a log of unknown flight time
# spanning 2 h on 02-20, so the aircraft can have had up to 52 h. Main refused 51 h (422,
# "more than the time in service ... 50.0 h"). Accepted now; the interval counts the earlier
# log only as far as the 51 h exceed the 50 h the dated logs give: 1.0 h.
def test_a_stated_inspection_within_the_bound_is_accepted_and_read_from(tmp_path: Path) -> None:
    key = "X-7"
    ingest(tmp_path, key, f"{key}-feb", datetime(2026, 2, 1, tzinfo=UTC), 50.0)
    feb20 = datetime(2026, 2, 20, tzinfo=UTC)
    ok(tmp_path, "ingest", key, str(unknown_log(tmp_path / f"{key}-feb20.bin", feb20, 2.0)))
    ingest(tmp_path, key, f"{key}-apr", datetime(2026, 4, 1, tzinfo=UTC), 45.0)
    inspection(tmp_path, key, "annual inspection", "2026-01-01T00:00Z")
    inspection(tmp_path, key, "100-hour inspection", "2026-03-01T00:00Z", "--at-hours", "51")
    prog = programme(tmp_path, key, JULY)
    assert figures(hundred(prog["items"])) == (
        45.0,
        55.0,
        "ok",
        "the 100-hour inspection of aircraft X-7 is due in 55.0 h" + bounded(1, 1.0, 44.0, 45.0),
    )


# A store written before this fix holding C2 and the refit main accepted: no row is
# rewritten, the journal verifies with the same head, and it reads what a new store reads,
# never later than main ("has 1.0 h left", serviceable). The route through is a removal, then
# another part; a removal is never judged against the part's life.
def test_a_store_holding_the_refit_main_accepted_reads_past_and_has_a_route(
    tmp_path: Path,
) -> None:
    c2(tmp_path)
    ok(tmp_path, "record", "component", "remove", "P-1", "X-2", *WHO,
       "--at", "2026-07-01T01:00Z")  # fmt: skip
    db = tmp_path / "fleet.sqlite"
    write_as_0_6_6(
        db,
        Entry(
            id=None,
            subject="P-1",
            kind="component.install",
            occurred_utc=datetime(2026, 7, 1, 2, tzinfo=UTC),
            recorded_utc=datetime(2026, 7, 1, 2, 5, tzinfo=UTC),
            entered_by="A. Tester, maintenance",
            statement="work done as described",
            details={"aircraft_key": "X-2"},
            supersedes=None,
            reason=None,
            synthetic=False,
        ),
    )
    head = ok(tmp_path, "verify")
    before = rows(db)
    prog = programme(tmp_path, "X-2", "2026-07-02T00:00Z")
    assert figures(part_item(prog["items"], "P-1")) == (301.0, -1.0, "overdue", P1_C2)
    assert prog["status"] == "unserviceable"
    assert ok(tmp_path, "verify") == head
    assert rows(db) == before
    ok(tmp_path, "record", "component", "remove", "P-1", "X-2", *WHO,
       "--at", "2026-07-01T03:00Z")  # fmt: skip
    part(tmp_path, "P-9", since="2026-07-01", hours="0")
    fit(tmp_path, "P-9", "X-2", "2026-07-01T04:00Z")
    assert programme(tmp_path, "X-2", "2026-07-02T00:00Z")["status"] == "serviceable"


# The evidence pack's usage table adds up: the time in service is the hours before the first
# log, the logged flight, the flight no log covers and, only when such logs exist, the spans
# of the logs whose flight time is not known. Main had no such column: 97.0 h read as 0 + 95
# + 0. A pack with no such log keeps its fields and table as they were.
SPANS = "Spans of logs whose flight time is not known"


def test_the_pack_says_what_its_time_in_service_counts(fleet: Path, tmp_path: Path) -> None:
    ok(fleet, "evidence", "X-1", "--out", "pack.html", "--json", "--as-of", JULY)
    usage = json.loads((fleet / "pack.json").read_text(encoding="utf-8"))["usage"]
    parts = ("time_in_service_s", "before_s", "logged_s", "unlogged_s", "unknown_spans_s")
    assert tuple(usage[k] for k in parts) == (97.0 * H, 0.0, 95.0 * H, 0.0, 2.0 * H)
    html = (fleet / "pack.html").read_text(encoding="utf-8")
    assert f"<th>Flight no log covers</th><th>{SPANS}</th></tr>" in html
    assert "<td>2.0 h (7200 s)</td></tr>" in html
    ingest(tmp_path, "X-8", "X-8-feb", datetime(2026, 2, 1, tzinfo=UTC), 5.0)
    inspection(tmp_path, "X-8", "annual inspection", "2026-01-01T00:00Z")
    ok(tmp_path, "evidence", "X-8", "--out", "pack.html", "--json", "--as-of", JULY)
    plain = json.loads((tmp_path / "pack.json").read_text(encoding="utf-8"))["usage"]
    assert "unknown_spans_s" not in plain
    assert plain["time_in_service_s"] == plain["logged_s"] == 5.0 * H
    assert SPANS not in (tmp_path / "pack.html").read_text(encoding="utf-8")


# ---- one answer on every surface ---------------------------------------------------------


@pytest.fixture(scope="module")
def served(fleet: Path) -> Iterator[str]:
    """`uasw serve` on the module's store, read only."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = {k: v for k, v in os.environ.items() if k not in ("UASW_WRITE_TOKEN", "UASW_FIXTURES")}
    out = (fleet / "serve.log").open("w")
    p = subprocess.Popen(
        [*UASW, "serve", "--port", str(port), "--fixtures", str(FIXTURES)],
        cwd=fleet,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=out,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 120
        while True:
            assert p.poll() is None, (fleet / "serve.log").read_text()[-600:]
            try:
                urllib.request.urlopen(base + "/health", timeout=2).close()
                break
            except OSError:
                assert time.monotonic() < deadline, "the service never answered"
                time.sleep(0.2)
        yield base
    finally:
        stop(p)
        out.close()


# The command line's pack, the HTTP due list, the fleet due list, the aircraft row and the
# assistant's aircraft_due tool give the same items and the same board; the cue of the item
# the bound moves passes the cue checker.
@pytest.mark.parametrize("key", ["X-1", "X-2", "X-3", "X-4"])
def test_every_surface_reads_the_same(fleet: Path, served: str, key: str) -> None:
    as_of = "2026-07-01T00:00:00Z"
    prog = programme(fleet, key, as_of)
    query = urllib.parse.urlencode({"as_of": as_of})
    status, due = call(served, "GET", f"/aircraft/{key}/due?{query}")
    assert status == 200, due
    status, fleet_due = call(served, "GET", f"/fleet/due?{query}")
    assert status == 200, fleet_due
    status, row = call(served, "GET", f"/aircraft/{key}?{query}")
    assert status == 200, row
    path, q = resolve("aircraft_due", {"key": key}, datetime.fromisoformat(as_of))
    told = service_caller(served)(path, q)
    mine = sorted(figures(i) for i in prog["items"])
    assert sorted(figures(i) for i in due["items"]) == mine
    # The fleet list carries what needs attention: every item but those within their limit.
    attention = sorted(figures(i) for i in prog["items"] if i["state"] != "ok")
    assert sorted(figures(i) for i in fleet_due if i["aircraft_key"] == key) == attention
    assert sorted(figures(i) for i in told["items"]) == mine
    assert due["status"] == told["status"] == row["status"] == prog["status"]
    assert row["status_reasons"] == prog["status_reasons"]


def test_the_cue_of_a_bounded_item_says_what_its_sentence_says(served: str) -> None:
    query = urllib.parse.urlencode({"as_of": "2026-07-01T00:00:00Z", "cues": "true"})
    status, cued = call(served, "GET", f"/aircraft/X-2/due?{query}")
    assert status == 200, cued
    assert part_item(cued["items"], "P-2")["cue"] == "P-2 1.0 h past 300 h limit"
    assert hundred(cued["items"])["cue"] == "100 h insp due in 49.0 h"
    assert cued["status_cue"] == "unserviceable"


# ---- the many small shapes, through the library in one process ---------------------------

KEY = "X-1"
T = datetime(2026, 1, 1, tzinfo=UTC)
D = timedelta(days=1)


def rec(
    key: str, ref: str, utc: datetime | None, flight_h: float | None, span_h: float
) -> FlightRecord:
    """A flight record of `span_h`, with no UTC start when `utc` is None and a flight time
    that is not known when `flight_h` is None."""
    start = utc if utc is not None else Unknown("no GPS fix")
    time = Unknown(NOT_STAT) if flight_h is None else flight_h * H
    return dataclasses.replace(flight(key, 1, start, time, ref=ref), log_span_s=span_h * H)


def prop(installations: tuple[Installation, ...], before_h: float = 0.0) -> Component:
    return Component("PROP-M", "propeller set", date(2026, 1, 1), before_h * H, 0, installations,
                     True)  # fmt: skip


def plain(key: str, inspections: tuple[InspectionDone, ...] = ()) -> MaintenanceRecord:
    return MaintenanceRecord(key, 0.0, inspections, (), True)


def due(
    key: str,
    parts: tuple[Component, ...],
    logs: dict[str, list[FlightRecord]],
    inspections: tuple[InspectionDone, ...] = (),
    as_of: datetime = T + 120 * D,
) -> Any:
    return due_list(
        key,
        maintenance=plain(key, inspections),
        components=parts,
        flights_of=lambda k: logs.get(k, []),
        policy=POLICY,
        as_of=as_of,
    )


# A part moved between airframes counts each log of unknown flight time that started while
# it was fitted, on either airframe, at its span; a log before its fitting, after its
# removal, or on the other airframe at the time, is not counted.
def test_a_moved_part_counts_the_span_of_each_log_in_its_fittings() -> None:
    moved = prop((Installation("X-1", T, T + 60 * D), Installation("X-2", T + 61 * D, None)))
    logs = {
        "X-1": [
            rec("X-1", "a-known", T + 31 * D, 10.0, 10.5),
            rec("X-1", "a-in", T + 40 * D, None, 1.0),
            rec("X-1", "a-after", T + 68 * D, None, 4.0),
        ],
        "X-2": [
            rec("X-2", "b-before", T + 50 * D, None, 8.0),
            rec("X-2", "b-known", T + 63 * D, 20.0, 21.0),
            rec("X-2", "b-in", T + 68 * D, None, 2.0),
        ],
    }
    p = item(due("X-2", (moved,), logs), "propeller set PROP-M", "hours")
    assert (p.used, p.remaining, p.state) == (33.0, 267.0, "ok")
    assert p.message == (
        "propeller set PROP-M on aircraft X-2 has 267.0 h left of its 300 h life limit"
        + bounded(2, 3.0, 30.0, 33.0)
    )
    # Each log counted is named: the two in the part's fittings, and X-2's own log before it,
    # which counts in X-2's time in service. X-1's log after the removal counts nowhere here.
    notes = due("X-2", (moved,), logs).notes
    for ref, key, span in (("a-in", "X-1", 1.0), ("b-before", "X-2", 8.0), ("b-in", "X-2", 2.0)):
        assert span_note(ref, key, span) in notes
    assert not any("a-after" in n for n in notes)


# With no log of unknown flight time the item, its sentence and the time in service are what
# they were: no clause, no note.
def test_known_flight_time_reads_as_before() -> None:
    fitted = prop((Installation(KEY, T, None),))
    logs = {KEY: [rec(KEY, "k", T + 31 * D, 10.0, 12.0)]}
    d = due(KEY, (fitted,), logs)
    p = item(d, "propeller set PROP-M", "hours")
    assert p.message == (
        "propeller set PROP-M on aircraft X-1 has 290.0 h left of its 300 h life limit"
    )
    assert d.time_in_service_s == 10.0 * H
    assert not any("flight time" in n for n in d.notes)


# The interval of an inspection on day 50: 10 h known before it, a log of unknown flight time
# spanning 5 h before it, 20 h known after it and a log spanning 3 h after it. A log after it
# counts its whole span. A log before it counts as far as the inspection's hours exceed the
# 10 h the dated logs give at its date: nothing for a derived figure (it left the log out) or
# a stated 10 h; 2 h for a stated 12 h, which says that log flew 2 h; all 5 h for 15 h. The
# most the interval can hold is then 23 h whatever the stated figure, and the least is what
# main read.
@pytest.mark.parametrize(
    ("at_h", "derived", "least", "x", "n"),
    [
        (10.0, True, 20.0, 3.0, 1),
        (10.0, False, 20.0, 3.0, 1),
        (12.0, False, 18.0, 5.0, 2),
        (15.0, False, 15.0, 8.0, 2),
    ],
    ids=["derived", "stated-as-dated", "stated-2h-more", "stated-the-whole-span-more"],
)
def test_an_interval_counts_what_can_lie_inside_it(
    at_h: float, derived: bool, least: float, x: float, n: int
) -> None:
    used = 23.0
    logs = {
        KEY: [
            rec(KEY, "jan", T + 10 * D, 10.0, 10.0),
            rec(KEY, "feb", T + 40 * D, None, 5.0),  # before the inspection on day 50
            rec(KEY, "apr", T + 90 * D, 20.0, 20.0),
            rec(KEY, "may", T + 100 * D, None, 3.0),
        ]
    }
    done = (
        InspectionDone("100-hour inspection", T + 50 * D, at_h * H, derived=derived),
        InspectionDone("annual inspection", T + 50 * D, at_h * H, derived=derived),
    )
    hours = item(due(KEY, (), logs, done), "the 100-hour inspection", "hours")
    assert hours.used == used
    assert hours.message == (
        f"the 100-hour inspection of aircraft X-1 is due in {100 - used:.1f} h"
        + bounded(n, x, least, used)
    )
    # The time in service counts every span: 30 h known and 8 h of spans.
    assert due(KEY, (), logs, done).time_in_service_s == 38.0 * H


# A log of unknown flight time and a 0 s span adds nothing either way: no clause, but a note.
def test_a_zero_span_adds_nothing_and_is_named() -> None:
    fitted = prop((Installation(KEY, T, None),))
    d = due(KEY, (fitted,), {KEY: [rec(KEY, "z", T + 31 * D, None, 0.0)]})
    assert item(d, "propeller set PROP-M", "hours").message == (
        "propeller set PROP-M on aircraft X-1 has 300.0 h left of its 300 h life limit"
    )
    assert span_note("z", KEY, 0.0) in d.notes


# The cue of a part the bound puts past its limit, and the board, through the library.
def test_the_cue_checker_accepts_a_bounded_item() -> None:
    fitted = prop((Installation(KEY, T, None),), before_h=299.0)
    d = due(KEY, (fitted,), {KEY: [rec(KEY, "u", T + 31 * D, None, 2.0)]})
    p = item(d, "propeller set PROP-M", "hours")
    cue = item_cue(p, d.as_of)
    assert cue == "PROP-M 1.0 h past 300 h limit"
    assert problems(cue, p, d.as_of) == []
    assert board(d).status == "unserviceable"


CONFIG = load_config()
NOW = datetime(2026, 10, 1, tzinfo=UTC)
BY = "A. Tester, maintenance"


LOG_U1 = rec("X-1", "u-1", T + 31 * D, None, 2.0)  # 02-01, spanning 2 h, flight not known


def library_store(with_log: bool = True) -> Store:
    s = Store(":memory:")
    for key in ("X-1", "X-2"):
        s.add_aircraft(Aircraft(key, "Synthetic test aircraft", "px4", True, "CC0", "synthetic"))
    if with_log:
        s.add_flight("X-1", LOG_U1)
    return s


def write(store: Store, subject: str, kind: str, details: dict[str, Any] | None = None, *,
          at: datetime, supersedes: int | None = None) -> int:  # fmt: skip
    e = Entry(
        id=None,
        subject=subject,
        kind=kind,
        occurred_utc=at,
        recorded_utc=NOW,
        entered_by=BY,
        statement="" if kind == "retraction" else "work done as described",
        details=details or {},
        supersedes=supersedes,
        reason="entered in error" if supersedes is not None else None,
        synthetic=False,
    )
    stored = append(store, e, policy=CONFIG.life, now=NOW, tolerance_s=CONFIG.tolerance_s)
    assert stored.id is not None
    return stored.id


def refusal(store: Store, *args: Any, **kwargs: Any) -> str:
    count = store.entry_count()
    with pytest.raises(LedgerError) as exc:
        write(store, *args, **kwargs)
    assert exc.value.status == 409
    assert store.entry_count() == count
    return exc.value.detail


def p1(store: Store, before_h: float) -> None:
    write(store, "P-1", "component.register", {"kind": "propeller set",
          "in_service_since": "2026-01-01", "hours_s_before": before_h * H, "cycles_before": 0},
          at=T)  # fmt: skip


PAST = (
    "P-1 cannot be fitted to X-2 at 2026-02-11 00:00 UTC: it may have flown 300.0 h, the whole "
    "of its 300 h life limit, counting 1 log whose flight time is not known at the most the "
    "records allow (2.0 h): at least 298.0 h flown; a life-limited part that has reached its "
    "life limit is replaced, not fitted again"
)
INSTALL_4 = "entry 4 (component.install on P-1, 2026-02-11 00:00 UTC)"


def fitted_then_moved(with_log: bool = True) -> Store:
    """P-1 registered with 298 h (entry 1), on X-1 from 01-02 (2) to 02-10 (3), while a log of
    unknown flight time spanning 2 h was flown on 02-01 (held from the start, or not yet)."""
    s = library_store(with_log)
    p1(s, 298.0)
    write(s, "P-1", "component.install", {"aircraft_key": "X-1"}, at=T + D)
    write(s, "P-1", "component.remove", {"aircraft_key": "X-1"}, at=T + 40 * D)
    return s


# A new install: P-1 had 298 h and the log may have added 2 h, so it may have reached its
# 300 h limit. Main accepted it (298.0 h).
def test_a_new_install_faces_the_bound() -> None:
    s = fitted_then_moved()
    refused_with = refusal(s, "P-1", "component.install", {"aircraft_key": "X-2"}, at=T + 41 * D)
    assert refused_with == PAST


# The refit to X-2 (entry 4) was accepted before the log of 02-01 was uploaded; a log is never
# refused, so the store now holds a fitting the bound would refuse, and the board reads the
# part past. A correction of that install is judged as an install is: refused. The route is
# the sentence's: the part comes off (a removal is accepted). Undoing a retraction of the
# refit would bring it back: refused, naming it. Main accepted the correction and the undo.
def test_a_correction_and_an_undo_of_an_install_face_the_bound() -> None:
    for route in ("removal", "retraction"):
        s = fitted_then_moved(with_log=False)
        assert write(s, "P-1", "component.install", {"aircraft_key": "X-2"}, at=T + 41 * D) == 4
        s.add_flight("X-1", LOG_U1)
        on_x2 = due_list("X-2", maintenance=None, components=s.components(T + 50 * D),
                         flights_of=s.flights, policy=CONFIG.life, as_of=T + 50 * D)  # fmt: skip
        assert board(on_x2).status == "unserviceable"
        if route == "removal":
            assert refusal(s, "P-1", "component.install", {"aircraft_key": "X-2"},
                           at=T + 41 * D, supersedes=4) == PAST  # fmt: skip
            assert write(s, "P-1", "component.remove", {"aircraft_key": "X-2"}, at=T + 42 * D) == 5
        else:
            assert write(s, "P-1", "retraction", at=NOW - D, supersedes=4) == 5
            assert refusal(s, "P-1", "retraction", at=NOW - D, supersedes=5) == (
                f"this would bring back {INSTALL_4}, which entry 5 retracted: {PAST}; correct "
                "the records it conflicts with first, or record a new entry instead"
            )
