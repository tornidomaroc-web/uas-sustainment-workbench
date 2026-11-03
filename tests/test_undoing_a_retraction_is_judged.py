"""An entry that a write brings back is judged as a new entry of its date would be (issue #161).

#160 bounds an inspection's stated hours by the time in service the aircraft can have had at
its date, and refuses a new entry, a correction or a retraction that would break that bound.
Superseding a retraction (undoing it) brought the retracted entry back without that check,
because the check looked only at the kind of the entry superseded, itself a retraction. The
records: 200 h before the first log entered on 2025-12-01, a 100-hour inspection on
2026-01-01 stated at 150 h, 10 h flown in February and 95 h in April. Read on 2026-07-01 the
item is 55.0 h overdue, beyond its 10 h tolerance, and the aircraft unserviceable. Bringing
back a lower entry of 100 h (way A), or the 150 h inspection retracted while 100 h stood (way
B, 105.0 h overdue), turned a certain overdue into not known.

Down a chain of retractions every level flips what is live, so the same gap went further: a
retraction of a retraction of a retraction ended the 200 h entry, and the item read 5.0 h
overdue within its tolerance, the aircraft serviceable with deferred defects. A correction of
a retraction, of any kind, brought the retracted entry back the same way; a correction of the
hours entry by an entry of another kind ended it unjudged; undoing the retraction of a
component install could leave one part on two aircraft; and the refusal of an undone
correction told the operator to retract or correct an entry that can no longer be either.

The rule now: what a write brings back or ends, at any depth, is judged as if it were
written today: an entry brought back against the records as they stood at its date, the hours
bound of #160, and every entry after the earliest one the write changes. Every refusal names
an entry that can still be corrected, or a new record to make instead.

Operator-facing cases (both ways in, the undo of the undo, depth 3, each new refusal and the
route through it, a store written before this, the HTTP route) run real `uasw` processes,
each on a store of its own. The many small shapes (each depth, each kind of correction) run
through the library in one process: the code path is the one `uasw` and the service call.
"""

from __future__ import annotations

import os
import socket
import subprocess
import time
import urllib.parse
import urllib.request
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from test_hours_before_the_first_log_bound import CAN_HAVE, DISAGREE_BEFORE, OVERDUE_55, STAMP
from test_inspection_hours_follow_time_in_service import (
    FIXTURES,
    OVERDUE_5,
    UASW,
    WHO,
    figures,
    hundred,
    ingest,
    ok,
    programme,
    refused,
    rows,
)
from test_life_rules import flight
from test_utc_offsets import call, stop, write_as_0_6_6

from uas_workbench.fleet import load_config
from uas_workbench.fleet.model import Aircraft
from uas_workbench.ledger import Entry, LedgerError, append
from uas_workbench.ledger.model import RETRACTION
from uas_workbench.service.store import Store

H = 3600.0
BY = "A. Tester, maintenance"
RET = ["--by", BY, "--reason", "entered in error"]
JULY = "2026-07-01T00:00Z"
OVERDUE_105 = (
    "the 100-hour inspection of aircraft X-1 is 105.0 h overdue, beyond its 10 h tolerance"
)
OVERDUE_205 = OVERDUE_105.replace("105.0 h", "205.0 h")
WITHIN_5 = OVERDUE_5.format(key="X-1")
DEFERRED = "serviceable with deferred defects"
NOT_KNOWN = "the 100-hour inspection of aircraft X-1 cannot be measured on 2026-07-01: "
REFUSED_A = (
    f"refused (409): this would make entry 5 impossible (inspection.done on X-1, {STAMP}): "
    f"{CAN_HAVE}; retract or correct entry 5 first"
)
REFUSED_DEPTH_3 = (
    f"refused (409): this would make entry 5 impossible (inspection.done on X-1, {STAMP}): "
    f"at_hours_s 150.0 h is more than the time in service of X-1 at {STAMP}, 0.0 h; retract or "
    "correct entry 5 first"
)


def brought_back(by: int, ended: str, counted: int) -> str:
    """The refusal of a write that would bring back the 150 h inspection, entry 3."""
    return (
        f"this would bring back entry 3 (inspection.done on X-1, {STAMP}), which entry {by} "
        f"{ended}: {CAN_HAVE}, counting the 100.0 h before the first log that entry {counted} "
        f"records; correct entry {counted} first, or record the inspection again stating at "
        "most 100.0 h"
    )


# ---- the records, in real processes -------------------------------------------------------


def base(cwd: Path) -> None:
    """The logs (10 h on 2026-02-01, 95 h on 2026-04-01), 200 h before the first log entered
    on 2025-12-01 (entry 1) and an annual inspection on 2025-12-15 at 0 h (entry 2)."""
    ingest(cwd, "X-1", "feb", datetime(2026, 2, 1, tzinfo=UTC), 10.0)
    ingest(cwd, "X-1", "apr", datetime(2026, 4, 1, tzinfo=UTC), 95.0)
    ok(cwd, "record", "time-in-service", "X-1", "200", *WHO, "--at", "2025-12-01T00:00Z")
    ok(cwd, "record", "inspection", "X-1", "annual inspection", *WHO, "--at",
       "2025-12-15T00:00Z", "--at-hours", "0")  # fmt: skip


def hours_100(cwd: Path) -> None:
    ok(cwd, "record", "time-in-service", "X-1", "100", *WHO, "--at", "2026-06-01T00:00Z")


def inspection(cwd: Path, at_hours: str, *more: str) -> None:
    ok(cwd, "record", "inspection", "X-1", "100-hour inspection", *WHO, "--at",
       "2026-01-01T00:00Z", "--at-hours", at_hours, *more)  # fmt: skip


def retract(cwd: Path, n: int) -> None:
    ok(cwd, "record", "retract", str(n), *RET)


def way_a(cwd: Path) -> None:
    """Way A up to the undo: 100 h entered (3) and retracted (4), then the 150 h inspection
    (5), accepted since entry 3 is retracted."""
    base(cwd)
    hours_100(cwd)
    retract(cwd, 3)
    inspection(cwd, "150")


def way_b(cwd: Path) -> None:
    """Way B up to the undo: the 150 h inspection (3) retracted (4), then 100 h entered (5)."""
    base(cwd)
    inspection(cwd, "150")
    retract(cwd, 3)
    hours_100(cwd)


def reads(cwd: Path) -> tuple[tuple[Any, ...], Any]:
    prog = programme(cwd, "X-1", JULY)
    return figures(hundred(prog["items"])), prog["status"]


def undo_refused(cwd: Path, n: int) -> str:
    """Retract entry n, which must be refused with nothing written; the refusal line."""
    db = cwd / "fleet.sqlite"
    before = rows(db)
    line = refused(cwd, "record", "retract", str(n), *RET)
    assert rows(db) == before
    return line


# ---- way A: a lower entry of the hours before the first log comes back ---------------------


# Undoing the retraction of the 100 h entry is refused as a new 100 h entry is (#160), naming
# the inspection, and the item stays 55.0 h overdue, unserviceable (bd115ac accepted it and
# read not known; 0.6.6 read "due in 45.0 h", serviceable). The routes the refusal names:
# correct the inspection to the 100 h the records allow, or retract it; either way the undo
# is then accepted and the item reads what the records then give.
@pytest.mark.parametrize("route", ["correct-entry-5", "retract-entry-5"])
def test_way_a_bringing_back_a_lower_hours_entry_is_refused(tmp_path: Path, route: str) -> None:
    way_a(tmp_path)
    assert undo_refused(tmp_path, 4) == REFUSED_A
    assert reads(tmp_path) == ((155.0, -55.0, "overdue", OVERDUE_55), "unserviceable")
    if route == "correct-entry-5":
        inspection(tmp_path, "100", "--supersedes", "5", "--reason", "restated")
        expected = ((105.0, -5.0, "overdue_within_tolerance", WITHIN_5), DEFERRED)
    else:
        retract(tmp_path, 5)
        expected = ((205.0, -105.0, "overdue", OVERDUE_105), "unserviceable")
    retract(tmp_path, 4)
    assert reads(tmp_path) == expected


# ---- way B: the 150 h inspection comes back ------------------------------------------------


# Undoing the retraction of the 150 h inspection while 100 h stands is refused, naming the
# inspection, the entry that retracted it and the entry it is counted with, and the item stays
# 105.0 h overdue. The routes: correct the 100 h entry (to the 200 h the records first gave),
# after which the undo is accepted and reads 55.0 h overdue, and undoing that undo returns the
# reading it replaced; or record the inspection again at the 100 h the records allow.
@pytest.mark.parametrize("route", ["correct-entry-5", "record-again"])
def test_way_b_bringing_back_the_inspection_is_refused(tmp_path: Path, route: str) -> None:
    way_b(tmp_path)
    assert undo_refused(tmp_path, 4) == "refused (409): " + brought_back(4, "retracted", 5)
    assert reads(tmp_path) == ((205.0, -105.0, "overdue", OVERDUE_105), "unserviceable")
    if route == "record-again":
        inspection(tmp_path, "100")
        assert reads(tmp_path) == ((105.0, -5.0, "overdue_within_tolerance", WITHIN_5), DEFERRED)
        return
    ok(tmp_path, "record", "time-in-service", "X-1", "200", *WHO, "--supersedes", "5",
       "--reason", "the 100 h were miscounted")  # fmt: skip
    before_undo = reads(tmp_path)
    assert before_undo == ((305.0, -205.0, "overdue", OVERDUE_205), "unserviceable")
    retract(tmp_path, 4)  # entry 7: the undo, accepted now
    assert reads(tmp_path) == ((155.0, -55.0, "overdue", OVERDUE_55), "unserviceable")
    retract(tmp_path, 7)  # the undo of the undo
    assert reads(tmp_path) == before_undo


# ---- depth 3: a retraction of a retraction of a retraction ---------------------------------


# The 200 h entry retracted (3), the retraction undone (4), then the 150 h inspection (5).
# Retracting entry 4 ends the 200 h entry again, three levels down: refused as retracting
# entry 1 itself is, naming the inspection, and the item stays 55.0 h overdue (bd115ac
# accepted it and read 5.0 h overdue within tolerance, serviceable with deferred defects).
# The route: retract the inspection first; the write is then accepted.
def test_depth_3_ending_the_hours_entry_is_refused(tmp_path: Path) -> None:
    base(tmp_path)
    retract(tmp_path, 1)
    retract(tmp_path, 3)
    inspection(tmp_path, "150")
    assert undo_refused(tmp_path, 4) == REFUSED_DEPTH_3
    assert reads(tmp_path) == ((155.0, -55.0, "overdue", OVERDUE_55), "unserviceable")
    retract(tmp_path, 5)
    retract(tmp_path, 4)
    assert reads(tmp_path) == ((105.0, -5.0, "overdue_within_tolerance", WITHIN_5), DEFERRED)


# ---- undoing a correction: the refusal names a route that exists ---------------------------


# The 150 h inspection (3) corrected to 100 h (4), then 100 h entered (5). Undoing the
# correction was refused before this, but told the operator to retract or correct entry 3,
# which entry 4 supersedes: both are refused. The refusal now names entry 5, and correcting
# it lets the undo through.
def test_undoing_a_correction_names_an_entry_that_can_be_corrected(tmp_path: Path) -> None:
    base(tmp_path)
    inspection(tmp_path, "150")
    inspection(tmp_path, "100", "--supersedes", "3", "--reason", "restated")
    hours_100(tmp_path)
    assert undo_refused(tmp_path, 4) == "refused (409): " + brought_back(4, "corrected", 5)
    ok(tmp_path, "record", "time-in-service", "X-1", "200", *WHO, "--supersedes", "5",
       "--reason", "the 100 h were miscounted")  # fmt: skip
    retract(tmp_path, 4)
    assert reads(tmp_path) == ((155.0, -55.0, "overdue", OVERDUE_55), "unserviceable")


# ---- a store written before this -----------------------------------------------------------


# A store written by bd115ac (or 0.6.6) can already hold the undo. It loads, its journal
# verifies with the same head, no row is rewritten, and the item reads not known with the
# sentence saying the records disagree: the dated logs since the inspection give 105 h, too
# few to say it is beyond its tolerance, so never "due in 45.0 h" and never later than the
# records allow. A write that does not touch the hours is accepted; undoing the undo is
# accepted and reads what it read before the undo.
@pytest.mark.parametrize("way", ["a", "b"])
def test_a_store_holding_an_undone_retraction_reads_not_known(tmp_path: Path, way: str) -> None:
    (way_a if way == "a" else way_b)(tmp_path)
    db = tmp_path / "fleet.sqlite"
    write_as_0_6_6(
        db,
        Entry(
            id=None,
            subject="X-1",
            kind=RETRACTION,
            occurred_utc=datetime(2026, 7, 1, tzinfo=UTC),
            recorded_utc=datetime(2026, 7, 1, 0, 5, tzinfo=UTC),
            entered_by=BY,
            statement="",
            details={},
            supersedes=4,
            reason="entered in error",
            synthetic=False,
        ),
    )
    head = ok(tmp_path, "verify")
    assert head.startswith("journal intact: 8 links"), head  # 2 logs, 6 entries
    before = rows(db)
    disagree = NOT_KNOWN + DISAGREE_BEFORE
    unknown = {"unknown": DISAGREE_BEFORE}
    assert reads(tmp_path) == ((unknown, unknown, "unknown", disagree), {"unknown": disagree})
    assert ok(tmp_path, "verify") == head
    assert rows(db) == before
    ok(tmp_path, "record", "work-order", "open", "X-1", "--state", "deferred", *WHO)
    retract(tmp_path, 6)  # entry 8: the undo of the undo
    if way == "a":
        assert reads(tmp_path) == ((155.0, -55.0, "overdue", OVERDUE_55), "unserviceable")
    else:
        assert reads(tmp_path) == ((205.0, -105.0, "overdue", OVERDUE_105), "unserviceable")


# ---- the HTTP route --------------------------------------------------------------------------


@pytest.fixture
def served_way_b(tmp_path: Path) -> Iterator[tuple[Path, str]]:
    """`uasw serve` on a store holding way B up to the undo."""
    way_b(tmp_path)
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
    base_url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 120
        while True:
            assert p.poll() is None, (tmp_path / "serve.log").read_text()[-600:]
            try:
                urllib.request.urlopen(base_url + "/health", timeout=2).close()
                break
            except OSError:
                assert time.monotonic() < deadline, "the service never answered"
                time.sleep(0.2)
        yield tmp_path, base_url
    finally:
        stop(p)
        out.close()


# POST /entries refuses the undo with the same status and sentence as the command line, and
# the due list still reads 105.0 h overdue, unserviceable.
def test_the_service_refuses_the_undo_with_the_same_sentence(
    served_way_b: tuple[Path, str],
) -> None:
    cwd, base_url = served_way_b
    before = rows(cwd / "fleet.sqlite")
    undo = {
        "subject": "X-1",
        "kind": RETRACTION,
        "occurred_utc": "2026-07-01T00:00:00Z",
        "entered_by": BY,
        "statement": "",
        "details": {},
        "supersedes": 4,
        "reason": "entered in error",
    }
    status, body = call(base_url, "POST", "/entries", undo)
    assert (status, body["detail"]) == (409, brought_back(4, "retracted", 5))
    assert rows(cwd / "fleet.sqlite") == before
    query = urllib.parse.urlencode({"as_of": "2026-07-01T00:00:00Z"})
    status, due = call(base_url, "GET", f"/aircraft/X-1/due?{query}")
    assert status == 200, due
    assert figures(hundred(due["items"])) == (205.0, -105.0, "overdue", OVERDUE_105)
    assert due["status"] == "unserviceable"


# ---- a component install that comes back --------------------------------------------------


def component(cwd: Path) -> None:
    """X-1 and X-2 (a log each), propeller set P-1 registered on 2026-01-01 (entry 1)."""
    ingest(cwd, "X-1", "x1", datetime(2026, 2, 1, tzinfo=UTC), 1.0)
    ingest(cwd, "X-2", "x2", datetime(2026, 2, 1, tzinfo=UTC), 1.0)
    ok(cwd, "record", "component", "register", "P-1", "--kind", "propeller set", "--since",
       "2026-01-01", *WHO, "--at", "2026-01-01T00:00Z")  # fmt: skip


def install(cwd: Path, aircraft: str, at: str) -> None:
    ok(cwd, "record", "component", "install", "P-1", aircraft, *WHO, "--at", at)


def live_ids(cwd: Path) -> list[int]:
    lines = ok(cwd, "record", "history", "P-1").splitlines()
    return [int(line.split()[0][1:]) for line in lines if line.startswith("#")]


# The install on X-1 (2) retracted (3), then P-1 installed on X-2 a day later (4). Undoing the
# retraction would put P-1 on both aircraft (bd115ac accepted it): refused, naming the install
# on X-2. The route: retract that install first; the undo is then accepted.
def test_bringing_back_an_install_before_a_later_one_is_refused(tmp_path: Path) -> None:
    component(tmp_path)
    install(tmp_path, "X-1", "2026-01-02T00:00Z")
    retract(tmp_path, 2)
    install(tmp_path, "X-2", "2026-01-03T00:00Z")
    assert undo_refused(tmp_path, 3) == (
        "refused (409): this would make entry 4 impossible (component.install on P-1, "
        "2026-01-03 00:00 UTC): P-1 is installed on X-1 since 2026-01-02 00:00 UTC; retract or "
        "correct entry 4 first"
    )
    retract(tmp_path, 4)
    retract(tmp_path, 3)
    assert live_ids(tmp_path) == [1, 2, 5, 6]


# The install on X-1 dated 2026-01-05 (2) retracted (3), then P-1 installed on X-2 on
# 2026-01-03 (4). The install that would come back is itself impossible: P-1 is on X-2 then.
# Refused, naming it and the retraction. The route: record P-1's removal from X-2 before it.
def test_bringing_back_an_install_the_records_no_longer_allow_is_refused(tmp_path: Path) -> None:
    component(tmp_path)
    install(tmp_path, "X-1", "2026-01-05T00:00Z")
    retract(tmp_path, 2)
    install(tmp_path, "X-2", "2026-01-03T00:00Z")
    assert undo_refused(tmp_path, 3) == (
        "refused (409): this would bring back entry 2 (component.install on P-1, 2026-01-05 "
        "00:00 UTC), which entry 3 retracted: P-1 is installed on X-2 since 2026-01-03 00:00 "
        "UTC; correct the records it conflicts with first, or record a new entry instead"
    )
    ok(tmp_path, "record", "component", "remove", "P-1", "X-2", *WHO, "--at", "2026-01-04T00:00Z")
    retract(tmp_path, 3)
    assert live_ids(tmp_path) == [1, 2, 4, 5, 6]


# ---- the many shapes, through the library in one process ----------------------------------

CONFIG = load_config()
NOW = datetime(2026, 10, 1, tzinfo=UTC)
LATER = datetime(2026, 9, 1, tzinfo=UTC)  # when every retraction below is entered


def library_store() -> Store:
    s = Store(":memory:")
    for key in ("X-1", "X-2"):
        s.add_aircraft(Aircraft(key, "Synthetic test aircraft", "px4", True, "CC0", "synthetic"))
    s.add_flight("X-1", flight("X-1", 1, datetime(2026, 2, 1, tzinfo=UTC), 10 * H))
    s.add_flight("X-1", flight("X-1", 2, datetime(2026, 4, 1, tzinfo=UTC), 95 * H))
    return s


def entry(
    kind: str,
    details: dict[str, Any] | None = None,
    *,
    at: datetime = LATER,
    subject: str = "X-1",
    supersedes: int | None = None,
) -> Entry:
    return Entry(
        id=None,
        subject=subject,
        kind=kind,
        occurred_utc=at,
        recorded_utc=NOW,
        entered_by=BY,
        statement="" if kind == RETRACTION else "work done as described",
        details=details or {},
        supersedes=supersedes,
        reason="entered in error" if supersedes is not None else None,
        synthetic=False,
    )


def add(store: Store, e: Entry) -> int:
    stored = append(store, e, policy=CONFIG.life, now=NOW, tolerance_s=CONFIG.tolerance_s)
    assert stored.id is not None
    return stored.id


def sentence(store: Store, e: Entry) -> str:
    """The refusal of `e`, a 409, with nothing written."""
    count = store.entry_count()
    with pytest.raises(LedgerError) as exc:
        add(store, e)
    assert store.entry_count() == count
    assert exc.value.status == 409, exc.value.detail
    return exc.value.detail


def hours(store: Store, before_h: float, at: datetime) -> int:
    return add(store, entry("time_in_service.set", {"before_s": before_h * H}, at=at))


def done(store: Store, name: str, at_h: float, at: datetime, **kw: Any) -> int:
    return add(store, entry("inspection.done", {"name": name, "at_hours_s": at_h * H}, at=at, **kw))


def retraction(store: Store, of: int) -> int:
    return add(store, entry(RETRACTION, supersedes=of))


DEC_1 = datetime(2025, 12, 1, tzinfo=UTC)
DEC_15 = datetime(2025, 12, 15, tzinfo=UTC)
JAN_1 = datetime(2026, 1, 1, tzinfo=UTC)
JUN_1 = datetime(2026, 6, 1, tzinfo=UTC)


def library_base() -> Store:
    s = library_store()
    assert hours(s, 200, DEC_1) == 1
    assert done(s, "annual inspection", 0, DEC_15) == 2
    return s


# At every even depth the write brings the 150 h inspection back while 100 h stands: refused,
# naming it and the retraction that ended it, nothing written. At depth 2 this is way B.
@pytest.mark.parametrize("depth", [2, 4, 6])
def test_every_even_depth_that_brings_the_inspection_back_is_refused(depth: int) -> None:
    s = library_base()
    assert done(s, "100-hour inspection", 150, JAN_1) == 3
    chain = [3]
    for _ in range(depth - 1):
        chain.append(retraction(s, chain[-1]))
    counted = hours(s, 100, JUN_1)
    assert sentence(s, entry(RETRACTION, supersedes=chain[-1])) == brought_back(
        chain[1], "retracted", counted
    )


# At every odd depth past 1 the write ends the 200 h entry while the 150 h inspection stands:
# refused as retracting the 200 h entry itself is (depth 1), naming the inspection.
@pytest.mark.parametrize("depth", [1, 3, 5, 7])
def test_every_odd_depth_that_ends_the_hours_entry_is_refused(depth: int) -> None:
    s = library_base()
    chain = [1]
    for _ in range(depth - 1):
        chain.append(retraction(s, chain[-1]))
    inspected = done(s, "100-hour inspection", 150, JAN_1)
    assert sentence(s, entry(RETRACTION, supersedes=chain[-1])) == (
        f"this would make entry {inspected} impossible (inspection.done on X-1, {STAMP}): "
        f"at_hours_s 150.0 h is more than the time in service of X-1 at {STAMP}, 0.0 h; "
        f"retract or correct entry {inspected} first"
    )


# A correction of the retraction, of any kind, brings the retracted inspection back too:
# refused with the same sentence as undoing the retraction.
@pytest.mark.parametrize(
    ("kind", "details"),
    [
        ("inspection.done", {"name": "annual inspection", "at_hours_s": 0.0}),
        ("work_order.open", {"state": "deferred"}),
        ("time_in_service.set", {"before_s": 500 * H}),
    ],
    ids=["inspection", "work-order", "hours"],
)
def test_a_correction_of_a_retraction_is_judged_like_its_undo(
    kind: str, details: dict[str, Any]
) -> None:
    s = library_base()
    assert done(s, "100-hour inspection", 150, JAN_1) == 3
    assert retraction(s, 3) == 4
    assert hours(s, 100, JUN_1) == 5
    assert sentence(s, entry(kind, details, supersedes=4)) == brought_back(4, "retracted", 5)


# A correction of the 200 h entry by an entry of another kind ends the 200 h entry: judged as
# its retraction is, naming the inspection it would leave past the bound.
def test_a_correction_of_another_kind_that_ends_the_hours_entry_is_judged() -> None:
    s = library_base()
    assert done(s, "100-hour inspection", 150, JAN_1) == 3
    replaced = entry("inspection.done", {"name": "annual inspection", "at_hours_s": 0.0},
                     at=DEC_1, supersedes=1)  # fmt: skip
    assert sentence(s, replaced) == (
        f"this would make entry 3 impossible (inspection.done on X-1, {STAMP}): at_hours_s "
        f"150.0 h is more than the time in service of X-1 at {STAMP}, 0.0 h; retract or correct "
        "entry 3 first"
    )


# Three levels down, a write that ends an install leaves the later removal taking P-1 off an
# aircraft it is not on: refused, naming the removal (the install itself cannot be retracted
# while the removal depends on it, and now neither can it be ended through a chain).
def test_depth_3_ending_an_install_a_removal_depends_on_is_refused() -> None:
    s = library_store()
    pack = {"kind": "propeller set", "in_service_since": "2026-01-01", "hours_s_before": 0.0,
            "cycles_before": 0}  # fmt: skip
    jan = [datetime(2026, 1, d, tzinfo=UTC) for d in range(1, 6)]
    assert add(s, entry("component.register", pack, at=jan[0], subject="P-1")) == 1
    on = add(s, entry("component.install", {"aircraft_key": "X-1"}, at=jan[1], subject="P-1"))
    first = add(s, entry(RETRACTION, subject="P-1", supersedes=on))
    undo = add(s, entry(RETRACTION, subject="P-1", supersedes=first))
    off = add(s, entry("component.remove", {"aircraft_key": "X-1"}, at=jan[3], subject="P-1"))
    assert sentence(s, entry(RETRACTION, subject="P-1", supersedes=undo)) == (
        f"this would make entry {off} impossible (component.remove on P-1, 2026-01-04 00:00 "
        "UTC): P-1 is not installed on any aircraft at 2026-01-04 00:00 UTC, not installed on "
        f"X-1; retract or correct entry {off} first"
    )
