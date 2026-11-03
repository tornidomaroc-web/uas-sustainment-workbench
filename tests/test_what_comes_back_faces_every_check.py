"""An entry a write brings back faces every check a new entry of its kind and date faces.

Round 1 of issue #161 judged an entry brought back against the records at its date and the
hours bound of #160, but not against the checks that look at the whole ledger: that a part
has one live registration, that an entry is superseded by one live entry, and that a work
order id is opened once on an aircraft. So undoing a retraction could fork a chain of
corrections, bring back a second registration, or open a work order id a second time, and
the fold read both. Measured on bd115ac, in real processes:

- a part fitted to two aircraft at once, its life counting the hours of both;
- two corrections of one close, closing two work orders where the records close one: the
  aircraft read serviceable while a work order in work stood open;
- two corrections of one entry of the hours before the first log: the older one folded
  last and won, 5.0 h overdue within tolerance where the records give 205.0 h overdue;
- two registrations of one part: the one dated later reset the part, dropping its install,
  and the aircraft lost its 85.0 h past-limit grounding;
- two opens of one work order id; and the default id (WO-<entry id>) was never checked at
  all, so an order typed by hand as WO-3 and left awaiting parts was replaced by the one
  entry 3 opened without an id, and the aircraft read serviceable with deferred defects
  instead of AOG.

Superseding a correction undoes it, whatever the kind of the entry superseding it, so the
entry it had corrected comes back. A correction of a correction therefore left two live
entries at one place in the fold (the first entry and the new one, both at the first one's
date), ordered by nothing and neither judged against the other: a part corrected from X-1
to X-2 and then to X-3 stood on X-1 and X-3, and an entry of the hours before the first log
corrected from 200 h to 300 h and then to 250 h read 200 h, the first entry folding last.

Each is now refused 409, naming the entry that would come back, the entry that ended it and
the entry it would clash with, with the way through. Operator-facing shapes and each route
run real `uasw` processes; the many small shapes run through the library in one process,
the code path `uasw` and the service call.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from test_inspection_hours_follow_time_in_service import WHO, ingest, ok, programme, refused, rows
from test_undoing_a_retraction_is_judged import (
    BY,
    CONFIG,
    LATER,
    NOW,
    add,
    entry,
    library_store,
    retract,
    sentence,
    undo_refused,
)
from test_utc_offsets import write_as_0_6_6

from uas_workbench.fleet.model import Aircraft
from uas_workbench.ledger import Entry, LedgerError, append
from uas_workbench.ledger.model import RETRACTION
from uas_workbench.service.store import Store

H = 3600.0
JULY = "2026-07-01T00:00Z"
PROP = "propeller set P-1"
JAN = [datetime(2026, 1, d, tzinfo=UTC) for d in range(1, 32)]
WORK = "work done as described"


def fix(n: int, why: str = "restated") -> list[str]:
    return ["--supersedes", str(n), "--reason", why]


def life(cwd: Path, key: str) -> tuple[Any, ...] | None:
    """P-1's life item on `key` read on 2026-07-01, or None when it is not fitted there."""
    found = [i for i in programme(cwd, key, JULY)["items"] if i["subject"] == PROP]
    return (found[0]["used"], found[0]["remaining"], found[0]["message"]) if found else None


def on(key: str, used: float) -> tuple[Any, ...]:
    left = 300.0 - used
    return (used, left, f"{PROP} on aircraft {key} has {left:.1f} h left of its 300 h life limit")


def status(cwd: Path, as_of: str) -> tuple[Any, list[str]]:
    p = programme(cwd, "X-1", as_of)
    return p["status"], p["status_reasons"]


def in_maintenance(since: str) -> tuple[str, list[str]]:
    return ("in maintenance", [f"aircraft X-1 is in maintenance since {since}: {WORK}"])


def annual(cwd: Path) -> None:
    ok(cwd, "record", "inspection", "X-1", "annual inspection", *WHO, "--at",
       "2026-01-01T00:00Z", "--at-hours", "0")  # fmt: skip


# ---- the install fork, in real processes ---------------------------------------------------


def install(cwd: Path, aircraft: str, *more: str) -> None:
    ok(cwd, "record", "component", "install", "P-1", aircraft, *WHO, "--at",
       "2026-01-02T00:00Z", *more)  # fmt: skip


def install_fork(cwd: Path) -> None:
    """P-1 registered (1) and fitted to X-1 (2); entry 3 corrects the install to X-2, entry
    4 retracts entry 3, entry 5 corrects entry 2 again, restating X-1. X-1 flew 50 h and X-2
    100 h, both on 2026-02-01."""
    ingest(cwd, "X-1", "x1", datetime(2026, 2, 1, tzinfo=UTC), 50.0)
    ingest(cwd, "X-2", "x2", datetime(2026, 2, 1, tzinfo=UTC), 100.0)
    ok(cwd, "record", "component", "register", "P-1", "--kind", "propeller set", "--since",
       "2026-01-01", *WHO, "--at", "2026-01-01T00:00Z")  # fmt: skip
    install(cwd, "X-1")
    install(cwd, "X-2", *fix(2, "it went on X-2"))
    retract(cwd, 3)
    install(cwd, "X-1", *fix(2, "it went on X-1 after all"))


# Undoing the retraction would bring entry 3 back beside entry 5, both superseding entry 2:
# P-1 on X-1 and X-2 at once, its life counting the 150 h both flew (bd115ac accepted it).
# Refused, naming both, and P-1 stays on X-1 with 50.0 h used. The route: retract entry 5,
# after which the undo is accepted and P-1 is on X-2 alone. (Correcting entry 5 to X-2 is no
# way through: a correction of a correction undoes it, see below.)
def test_an_undo_that_would_fork_an_install_is_refused(tmp_path: Path) -> None:
    install_fork(tmp_path)
    assert undo_refused(tmp_path, 4) == (
        "refused (409): this would bring back entry 3 (component.install on P-1, 2026-01-02 "
        "00:00 UTC), which entry 4 retracted: entry 2 is already superseded by entry 5; "
        "retract entry 5 first"
    )
    assert (life(tmp_path, "X-1"), life(tmp_path, "X-2")) == (on("X-1", 50.0), None)
    retract(tmp_path, 5)
    retract(tmp_path, 4)
    assert (life(tmp_path, "X-1"), life(tmp_path, "X-2")) == (None, on("X-2", 100.0))


# ---- a close fork, in real processes -------------------------------------------------------


def close(cwd: Path, wid: str, *more: str) -> None:
    ok(cwd, "record", "work-order", "close", "X-1", wid, *WHO, "--at", "2026-02-01T00:00Z", *more)


# WO-A and WO-B in work; the close of WO-A (4) corrected to close WO-B (5), that correction
# retracted (6), and entry 4 corrected again, restating WO-A (7). Undoing the retraction would
# bring back the close of WO-B beside entry 7: both orders closed and the aircraft
# serviceable while WO-B is still in work (bd115ac accepted it). Refused; the aircraft stays in
# maintenance. The route: retract entry 7, then the undo closes WO-B alone, and WO-A is open.
def test_an_undo_that_would_fork_a_close_is_refused(tmp_path: Path) -> None:
    ingest(tmp_path, "X-1", "x1", datetime(2026, 2, 1, tzinfo=UTC), 10.0)
    annual(tmp_path)
    for day, wid in ((10, "WO-A"), (11, "WO-B")):
        ok(tmp_path, "record", "work-order", "open", "X-1", "--state", "in_work", "--work-id",
           wid, *WHO, "--at", f"2026-01-{day}T00:00Z")  # fmt: skip
    close(tmp_path, "WO-A")
    close(tmp_path, "WO-B", *fix(4, "it was WO-B"))
    retract(tmp_path, 5)
    close(tmp_path, "WO-A", *fix(4))
    assert undo_refused(tmp_path, 6) == (
        "refused (409): this would bring back entry 5 (work_order.close on X-1, 2026-02-01 "
        "00:00 UTC), which entry 6 retracted: entry 4 is already superseded by entry 7; "
        "retract entry 7 first"
    )
    assert status(tmp_path, JULY) == in_maintenance("2026-01-11")
    retract(tmp_path, 7)
    retract(tmp_path, 6)
    assert status(tmp_path, JULY) == in_maintenance("2026-01-10")


# ---- a work order id opened twice, in real processes ---------------------------------------


# WO-A opened in work on 2026-01-10 (2), retracted (3), opened again as deferred on
# 2026-02-10 (4). Undoing the retraction would leave two live opens of WO-A, a third open
# being refused (bd115ac accepted it, and read the aircraft in maintenance from 2026-01-10).
# Refused, naming entry 4. The route: retract entry 4, then the undo; WO-A is then in work
# from 2026-01-10.
def test_an_undo_that_would_open_a_work_order_id_twice_is_refused(tmp_path: Path) -> None:
    ingest(tmp_path, "X-1", "x1", datetime(2026, 2, 1, tzinfo=UTC), 10.0)
    annual(tmp_path)
    opened = ["record", "work-order", "open", "X-1", "--work-id", "WO-A", *WHO, "--at"]
    ok(tmp_path, *opened, "2026-01-10T00:00Z", "--state", "in_work")
    retract(tmp_path, 2)
    ok(tmp_path, *opened, "2026-02-10T00:00Z", "--state", "deferred")
    assert undo_refused(tmp_path, 3) == (
        "refused (409): this would bring back entry 2 (work_order.open on X-1, 2026-01-10 "
        "00:00 UTC), which entry 3 retracted: work order WO-A already exists on X-1, opened "
        "by entry 4; retract entry 4 first"
    )
    deferred = (
        "serviceable with deferred defects",
        [f"aircraft X-1 carries a deferred defect since 2026-02-10: {WORK}"],
    )
    jan, mar = "2026-01-20T00:00Z", "2026-03-01T00:00Z"
    assert (status(tmp_path, jan), status(tmp_path, mar)) == (("serviceable", []), deferred)
    retract(tmp_path, 4)
    retract(tmp_path, 3)
    in_work = in_maintenance("2026-01-10")
    assert (status(tmp_path, jan), status(tmp_path, mar)) == (in_work, in_work)


# An order typed by hand as WO-3 and left awaiting parts (2); entry 3 opened without an id
# would take WO-3 as its default and replace it, the aircraft reading serviceable with
# deferred defects instead of AOG (bd115ac and 0.6.6 accepted it). Refused; the aircraft
# stays AOG. The route: give the new order an id of its own; both orders then stand.
def test_a_default_work_id_already_taken_is_refused(tmp_path: Path) -> None:
    ingest(tmp_path, "X-1", "x1", datetime(2026, 2, 1, tzinfo=UTC), 10.0)
    annual(tmp_path)
    opened = ["record", "work-order", "open", "X-1", *WHO]
    ok(tmp_path, *opened, "--work-id", "WO-3", "--state", "awaiting_parts", "--at",
       "2026-01-10T00:00Z")  # fmt: skip
    later = ["--state", "deferred", "--at", "2026-01-20T00:00Z"]
    db = tmp_path / "fleet.sqlite"
    before = rows(db)
    assert refused(tmp_path, *opened, *later) == (
        "refused (409): work order WO-3 already exists on X-1, so this work order cannot take "
        "it as its default id; give it a work id of its own"
    )
    assert rows(db) == before
    aog = f"aircraft X-1 is on the ground awaiting parts since 2026-01-10: {WORK}"
    assert status(tmp_path, "2026-03-01T00:00Z") == ("AOG", [aog])
    ok(tmp_path, *opened, "--work-id", "WO-4", *later)
    deferred = f"aircraft X-1 carries a deferred defect since 2026-01-20: {WORK}"
    assert status(tmp_path, "2026-03-01T00:00Z") == ("AOG", [aog, deferred])
    history = ok(tmp_path, "record", "history", "X-1")
    assert (history.count("[WO-3]"), history.count("[WO-4]")) == (1, 1)


# ---- a part registered twice, in real processes --------------------------------------------


# P-1 registered with 0 h (2, dated 2026-01-05), retracted (3), registered again with 280 h
# before (4, 2026-01-01) and fitted to X-1 (5, 2026-01-02); X-1 flew 105 h. Undoing the
# retraction would bring back a second registration, dated after the install: the fold reset
# P-1 to 0 h and dropped the install, and X-1 lost its 85.0 h past-limit grounding (bd115ac
# accepted it, reading serviceable with deferred defects). Refused, naming entry 4; P-1 stays
# 85.0 h past its limit. The route the refusals give, step by step: retracting entry 4 is
# refused while the install depends on it; so retract the install, then entry 4, then undo;
# fit P-1 again: 105.0 h used.
def test_an_undo_that_would_register_a_part_twice_is_refused(tmp_path: Path) -> None:
    ingest(tmp_path, "X-1", "x1", datetime(2026, 2, 1, tzinfo=UTC), 105.0)
    annual(tmp_path)
    register = ["record", "component", "register", "P-1", "--kind", "propeller set", "--since",
                "2026-01-01", *WHO]  # fmt: skip
    ok(tmp_path, *register, "--at", "2026-01-05T00:00Z")
    retract(tmp_path, 2)
    ok(tmp_path, *register, "--hours-before", "280", "--at", "2026-01-01T00:00Z")
    install(tmp_path, "X-1")
    assert undo_refused(tmp_path, 3) == (
        "refused (409): this would bring back entry 2 (component.register on P-1, 2026-01-05 "
        "00:00 UTC), which entry 3 retracted: component P-1 is already registered, by entry 4; "
        "retract entry 4 first"
    )
    past = "propeller set P-1 on aircraft X-1 is 85.0 h past its 300 h life limit"
    assert life(tmp_path, "X-1") == (385.0, -85.0, past)
    assert undo_refused(tmp_path, 4) == (
        "refused (409): entry 4 cannot be superseded while entries 5 depend on it; correct "
        "those first"
    )
    retract(tmp_path, 5)
    retract(tmp_path, 4)
    retract(tmp_path, 3)
    ok(tmp_path, "record", "component", "install", "P-1", "X-1", *WHO, "--at", "2026-01-06T00:00Z")
    assert life(tmp_path, "X-1") == on("X-1", 105.0)


# ---- a correction of a correction, in real processes --------------------------------------


def overdue(cwd: Path) -> float:
    """How far past due the 100-hour inspection of X-1 reads on 2026-07-01, the aircraft
    unserviceable."""
    p = programme(cwd, "X-1", JULY)
    (item,) = [i for i in p["items"] if i["subject"] == "the 100-hour inspection"]
    assert (p["status"], item["state"]) == ("unserviceable", "overdue")
    assert item["message"] == (
        f"the 100-hour inspection of aircraft X-1 is {-item['remaining']:.1f} h overdue, beyond "
        "its 10 h tolerance"
    )
    return float(-item["remaining"])


# 200 h before the first log (1), an annual inspection at 100 h (2), entry 1 corrected to
# 300 h (3). Correcting entry 3 to 250 h undoes it and would bring entry 1 back beside the new
# entry: bd115ac accepted it and read 200 h, 105.0 h overdue, where the records state 250 h.
# Refused, naming entry 1 and entry 3; the item stays 205.0 h overdue. The route: retract
# entry 3 (entry 1 is back: 105.0 h overdue), then correct entry 1 to 250 h: 155.0 h overdue.
def test_a_correction_of_a_correction_is_refused_with_its_route(tmp_path: Path) -> None:
    ingest(tmp_path, "X-1", "feb", datetime(2026, 2, 1, tzinfo=UTC), 10.0)
    ingest(tmp_path, "X-1", "apr", datetime(2026, 4, 1, tzinfo=UTC), 95.0)
    hours = ["record", "time-in-service", "X-1"]
    dec_1 = ["--at", "2025-12-01T00:00Z"]
    ok(tmp_path, *hours, "200", *WHO, *dec_1)
    ok(tmp_path, "record", "inspection", "X-1", "annual inspection", *WHO, "--at",
       "2025-12-15T00:00Z", "--at-hours", "100")  # fmt: skip
    ok(tmp_path, *hours, "300", *WHO, *dec_1, *fix(1))
    db = tmp_path / "fleet.sqlite"
    before = rows(db)
    assert refused(tmp_path, *hours, "250", *WHO, *dec_1, *fix(3)) == (
        "refused (409): this would bring back entry 1 (time_in_service.set on X-1, 2025-12-01 "
        "00:00 UTC), which entry 3 corrected: superseding entry 3 undoes it, so it and this "
        "entry would both stand in one place in the records; to replace entry 3, retract it "
        "first, then correct entry 1"
    )
    assert rows(db) == before
    assert overdue(tmp_path) == 205.0
    retract(tmp_path, 3)
    assert overdue(tmp_path) == 105.0
    ok(tmp_path, *hours, "250", *WHO, *dec_1, *fix(1))
    assert overdue(tmp_path) == 155.0


# ---- a store that already holds a fork, in real processes ----------------------------------


# A store written by bd115ac (or 0.6.6) can already hold the install fork. It loads, its
# journal verifies with the same head, no row is rewritten, and it reads as bd115ac read it:
# P-1 on both aircraft, its life counting the 150 h both flew, never later than either
# install allows. A write elsewhere is accepted. The repair: retract one of the two
# corrections; P-1 is then on the other aircraft alone.
def test_a_store_holding_a_fork_reads_both_and_is_repaired_by_a_retraction(
    tmp_path: Path,
) -> None:
    install_fork(tmp_path)
    db = tmp_path / "fleet.sqlite"
    write_as_0_6_6(
        db,
        Entry(
            id=None,
            subject="P-1",
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
    assert (life(tmp_path, "X-1"), life(tmp_path, "X-2")) == (on("X-1", 150.0), on("X-2", 150.0))
    assert (ok(tmp_path, "verify"), rows(db)) == (head, before)
    annual(tmp_path)
    retract(tmp_path, 3)
    assert (life(tmp_path, "X-1"), life(tmp_path, "X-2")) == (on("X-1", 50.0), None)


# ---- the many shapes, through the library in one process ----------------------------------

PACK = {"kind": "propeller set", "in_service_since": "2026-01-01", "hours_s_before": 0.0,
        "cycles_before": 0}  # fmt: skip


def part(kind: str, details: dict[str, Any], at: datetime, **kw: Any) -> Entry:
    return entry(kind, details, at=at, subject="P-1", **kw)


def fitted(s: Store, aircraft: str, **kw: Any) -> int:
    return add(s, part("component.install", {"aircraft_key": aircraft}, JAN[1], **kw))


def retracting(s: Store, of: int, subject: str = "P-1") -> int:
    return add(s, entry(RETRACTION, subject=subject, supersedes=of))


def where(s: Store) -> list[str]:
    (p,) = [c for c in s.projection().components if c.id == "P-1"]
    return sorted(i.aircraft_key for i in p.installations if i.to_utc is None)


def forked_install() -> tuple[Store, list[int]]:
    """The install fork up to the write that would make it: P-1 registered (1) and fitted to
    X-1 (2); entry 3 corrects it to X-2 and entry 4 retracts entry 3."""
    s = library_store()
    assert add(s, part("component.register", PACK, JAN[0])) == 1
    assert fitted(s, "X-1") == 2
    assert fitted(s, "X-2", supersedes=2) == 3
    assert retracting(s, 3) == 4
    return s, [1, 2, 3, 4]


# At every depth the chain reaches, bringing entry 3 back while another correction of entry 2
# stands is refused, naming the retraction that had ended entry 3 (entry 4, at every odd
# depth) and the other correction.
@pytest.mark.parametrize("depth", [1, 3, 5])
def test_a_fork_is_refused_at_any_depth(depth: int) -> None:
    s, chain = forked_install()
    for _ in range(depth - 1):
        chain.append(retracting(s, chain[-1]))
    assert where(s) == ["X-1"]
    other = fitted(s, "X-1", supersedes=2)
    assert sentence(s, entry(RETRACTION, subject="P-1", supersedes=chain[-1])) == (
        "this would bring back entry 3 (component.install on P-1, 2026-01-02 00:00 UTC), "
        f"which entry 4 retracted: entry 2 is already superseded by entry {other}; retract "
        f"entry {other} first"
    )
    assert where(s) == ["X-1"]


# A correction of the retraction brings entry 3 back as its undo does: refused with the same
# sentence, though its own check of P-1's place passes.
def test_a_correction_of_the_retraction_that_would_fork_is_refused() -> None:
    s, _ = forked_install()
    assert fitted(s, "X-1", supersedes=2) == 5
    removed = part("component.remove", {"aircraft_key": "X-1"}, LATER, supersedes=4)
    assert sentence(s, removed) == (
        "this would bring back entry 3 (component.install on P-1, 2026-01-02 00:00 UTC), which "
        "entry 4 retracted: entry 2 is already superseded by entry 5; retract entry 5 first"
    )


def hundred_hour(s: Store, at_h: float, **kw: Any) -> int:
    details = {"name": "100-hour inspection", "at_hours_s": at_h * H}
    return add(s, entry("inspection.done", details, at=datetime(2026, 3, 1, tzinfo=UTC), **kw))


def hours_before(s: Store, before_h: float, **kw: Any) -> int:
    at = datetime(2025, 12, 1, tzinfo=UTC)
    return add(s, entry("time_in_service.set", {"before_s": before_h * H}, at=at, **kw))


def removal(s: Store, **kw: Any) -> int:
    return add(s, part("component.remove", {"aircraft_key": "X-1"}, JAN[20], **kw))


def make_twins(kind: str) -> tuple[Store, str, int, int, int, int]:
    """An entry `first` of `kind`, corrected by `a`, `a` retracted by `ret`, then `first`
    corrected again by `b`: undoing `ret` would make `a` and `b` twins."""
    s = library_store()
    subject = "X-1"
    if kind == "inspection":
        annual = {"name": "annual inspection", "at_hours_s": 0.0}
        add(s, entry("inspection.done", annual, at=JAN[0]))
        first = hundred_hour(s, 10)
        a = hundred_hour(s, 5, supersedes=first)
        ret = retracting(s, a, subject)
        b = hundred_hour(s, 8, supersedes=first)
    elif kind == "hours":
        first = hours_before(s, 200)
        a = hours_before(s, 100, supersedes=first)
        ret = retracting(s, a, subject)
        b = hours_before(s, 300, supersedes=first)
    else:  # a removal
        subject = "P-1"
        add(s, part("component.register", PACK, JAN[0]))
        fitted(s, "X-1")
        first = removal(s)
        a = removal(s, supersedes=first)
        ret = retracting(s, a, subject)
        b = removal(s, supersedes=first)
    return s, subject, first, a, ret, b


# The same fork for an inspection, an entry of the hours before the first log and a removal.
@pytest.mark.parametrize("kind", ["inspection", "hours", "removal"])
def test_a_fork_of_any_kind_is_refused(kind: str) -> None:
    s, subject, first, a, ret, b = make_twins(kind)
    (came,) = [e for e in s.entries() if e.id == a]
    assert sentence(s, entry(RETRACTION, subject=subject, supersedes=ret)) == (
        f"this would bring back entry {a} ({came.kind} on {subject}, "
        f"{came.occurred_utc:%Y-%m-%d %H:%M} UTC), which entry {ret} retracted: entry {first} "
        f"is already superseded by entry {b}; retract entry {b} first"
    )


# A retraction that comes back is one of two live retractions of one entry when that entry
# was retracted again meanwhile: refused as a second retraction of it is.
def test_a_retraction_that_comes_back_beside_another_is_refused() -> None:
    s = library_store()
    first = add(s, entry("work_order.open", {"state": "deferred", "work_id": "WO-A"}, at=JAN[9]))
    ret = retracting(s, first, "X-1")
    undo = retracting(s, ret, "X-1")
    again = retracting(s, first, "X-1")
    assert sentence(s, entry(RETRACTION, supersedes=undo)) == (
        f"this would bring back entry {ret} (retraction on X-1, 2026-09-01 00:00 UTC), which "
        f"entry {undo} retracted: entry {first} is already superseded by entry {again}; "
        f"retract entry {again} first"
    )


# A part corrected from X-1 to X-2 and then to X-3 stood on X-1 and X-3 (bd115ac and 0.6.6):
# refused, and P-1 stays on X-2.
def test_a_correction_of_a_corrected_install_is_refused() -> None:
    s = library_store()
    s.add_aircraft(Aircraft("X-3", "Synthetic test aircraft", "px4", True, "CC0", "synthetic"))
    add(s, part("component.register", PACK, JAN[0]))
    first = fitted(s, "X-1")
    moved = fitted(s, "X-2", supersedes=first)
    again = part("component.install", {"aircraft_key": "X-3"}, JAN[1], supersedes=moved)
    assert sentence(s, again) == (
        f"this would bring back entry {first} (component.install on P-1, 2026-01-02 00:00 "
        f"UTC), which entry {moved} corrected: superseding entry {moved} undoes it, so it and "
        "this entry would both stand in one place in the records; to replace entry "
        f"{moved}, retract it first, then correct entry {first}"
    )
    assert where(s) == ["X-2"]


def held_by_0_6_6(path: Path) -> tuple[Store, int, int, int]:
    """A store as 0.6.6 left it after a correction of a correction: P-1 fitted to X-1
    (`first`), corrected to X-2 (`moved`), that corrected to X-3 (`third`, written as 0.6.6
    wrote it); `first` and `third` are both live."""
    s = Store(str(path))
    for key in ("X-1", "X-2", "X-3"):
        s.add_aircraft(Aircraft(key, "Synthetic test aircraft", "px4", True, "CC0", "synthetic"))
    add(s, part("component.register", PACK, JAN[0]))
    first = fitted(s, "X-1")
    moved = fitted(s, "X-2", supersedes=first)
    third = part("component.install", {"aircraft_key": "X-3"}, JAN[1], supersedes=moved)
    write_as_0_6_6(path, third)
    assert where(s) == ["X-1", "X-3"]
    return s, first, moved, s.entry_count()


# On such a store, retracting the first install is accepted; undoing that would bring it back
# to the place the correction of the correction holds: refused, naming it.
def test_an_entry_brought_back_to_a_place_another_holds_is_refused(tmp_path: Path) -> None:
    s, first, _, third = held_by_0_6_6(tmp_path / "fleet.sqlite")
    try:
        ret = retracting(s, first)
        assert where(s) == ["X-3"]
        assert sentence(s, entry(RETRACTION, subject="P-1", supersedes=ret)) == (
            f"this would bring back entry {first} (component.install on P-1, 2026-01-02 00:00 "
            f"UTC), which entry {ret} retracted: entry {third} stands in its place already; "
            f"retract entry {third} first"
        )
    finally:
        s.close()


# The repair of such a store: retract the correction of the correction. The first correction
# comes back alone and ends the first install: P-1 on X-2.
def test_a_store_holding_a_correction_of_a_correction_is_repaired(tmp_path: Path) -> None:
    s, _, _, third = held_by_0_6_6(tmp_path / "fleet.sqlite")
    try:
        retracting(s, third)
        assert where(s) == ["X-2"]
    finally:
        s.close()


# An entry brought back must still be named in fleet.toml, as a new one must: a part kind or
# an inspection dropped from it since is refused, naming the entry and the way through.
@pytest.mark.parametrize("what", ["part kind", "inspection"])
def test_what_comes_back_must_still_be_in_fleet_toml(what: str) -> None:
    s = library_store()
    if what == "part kind":
        subject, kind = "P-1", "component.register"
        came = add(s, part(kind, PACK, JAN[0]))
        kinds = {k: v for k, v in CONFIG.life.component_kinds.items() if k != "propeller set"}
        policy = dataclasses.replace(CONFIG.life, component_kinds=kinds)
        why = "component kind 'propeller set' is not in fleet.toml; the kinds are battery pack"
    else:
        subject, kind = "X-1", "inspection.done"
        came = add(s, entry(kind, {"name": "annual inspection", "at_hours_s": 0.0}, at=JAN[0]))
        names = {k: v for k, v in CONFIG.life.inspections.items() if k != "annual inspection"}
        policy = dataclasses.replace(CONFIG.life, inspections=names)
        why = (
            "inspection 'annual inspection' is not in fleet.toml; the inspections are "
            "100-hour inspection"
        )
    ret = retracting(s, came, subject)
    count = s.entry_count()
    undo = entry(RETRACTION, subject=subject, supersedes=ret)
    with pytest.raises(LedgerError) as exc:
        append(s, undo, policy=policy, now=NOW, tolerance_s=CONFIG.tolerance_s)
    assert (exc.value.status, s.entry_count()) == (409, count)
    assert exc.value.detail == (
        f"this would bring back entry {came} ({kind} on {subject}, 2026-01-01 00:00 UTC), "
        f"which entry {ret} retracted: {why}; name it in fleet.toml first"
    )


# A new entry cannot supersede an entry others depend on; an entry brought back does not face
# that check, because what it protects is judged directly: every later entry is judged again
# with the write in place (round 1). Here a correction of an open that keeps its id comes
# back over the open a close depends on: the close still holds, one WO-A, closed. Pinned so
# the choice is seen; it passes on 788e2d2 too.
def test_what_comes_back_over_an_entry_others_depend_on_is_judged_by_them() -> None:
    s = library_store()
    opened = add(s, entry("work_order.open", {"state": "in_work", "work_id": "WO-A"}, at=JAN[9]))
    restated = {"state": "deferred", "work_id": "WO-A"}
    again = add(s, entry("work_order.open", restated, at=JAN[9], supersedes=opened))
    ret = retracting(s, again, "X-1")
    feb = datetime(2026, 2, 1, tzinfo=UTC)
    add(s, entry("work_order.close", {"work_id": "WO-A"}, at=feb))
    retracting(s, ret, "X-1")
    (wo,) = s.projection().work_orders["X-1"]
    assert (wo.work_id, wo.state, wo.closed_utc) == ("WO-A", "deferred", feb)


# An install brought back is judged with the rest of the ledger, as a new one is: when its
# part is registered only later, the refusal names that date, not "not registered by then".
def test_what_comes_back_is_judged_with_the_whole_ledger() -> None:
    s = library_store()
    first = add(s, part("component.register", PACK, JAN[0]))
    put = fitted(s, "X-1")
    ret = retracting(s, put)
    retracting(s, first)
    add(s, part("component.register", PACK, JAN[5]))
    assert sentence(s, entry(RETRACTION, subject="P-1", supersedes=ret)) == (
        f"this would bring back entry {put} (component.install on P-1, 2026-01-02 00:00 UTC), "
        f"which entry {ret} retracted: P-1 cannot be on an aircraft at 2026-01-02 00:00 UTC: "
        "it was registered on 2026-01-06 00:00 UTC; correct the records it conflicts with "
        "first, or record a new entry instead"
    )


# An entry brought back is judged against the store as it is now, as a new one is: when an
# aircraft has since been added under the part's id, a registration of that id is refused.
def test_what_comes_back_faces_the_subjects_of_the_store_now() -> None:
    s = library_store()
    came = add(s, part("component.register", PACK, JAN[0]))
    ret = retracting(s, came)
    s.add_aircraft(Aircraft("P-1", "Synthetic test aircraft", "px4", True, "CC0", "synthetic"))
    assert sentence(s, entry(RETRACTION, subject="P-1", supersedes=ret)) == (
        f"this would bring back entry {came} (component.register on P-1, 2026-01-01 00:00 UTC), "
        f"which entry {ret} retracted: P-1 is an aircraft; component.register needs a "
        "component id; correct the records it conflicts with first, or record a new entry "
        "instead"
    )
