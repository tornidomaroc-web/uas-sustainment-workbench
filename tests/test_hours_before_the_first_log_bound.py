"""An inspection done before the first log states no more hours than the aircraft can have had
then, whatever order the records arrive in (issue #151, second round).

Before the first log the time in service at a date is not known, but it is bounded: the hours
before the first log are reached only by that log, so on an earlier date the aircraft had at
most those hours, plus what the logs with no UTC start add, which may have been flown at any
date. An inspection there stating more is impossible. 0.6.6 and the first round of #151 did
not look: 200 h entered on 2025-12-01, a 100-hour inspection on 2026-01-01 stated at 150 h,
10 h flown in February and 95 h in April read "55.0 h overdue, beyond its 10 h tolerance",
unserviceable; a later entry of 100 h then turned it into "due in 45.0 h", serviceable.

A store written before this can hold such records, or the case A disagreement of the first
round. Its items read what the records still give: the hours flown since the inspection.
After the first log the least time in service at the inspection's date is what the logs place
by then, so the item reads from that figure, saying the records disagree. Before the first log
the dated logs since are the least the interval holds: overdue when they alone put it past its
tolerance, not known otherwise. Never later than the records allow.

On such a store a write is refused only when it puts an inspection further past the bound
than the records already did: an entry that removes the disagreement, or leaves it as it was,
is accepted.

Every row runs real `uasw` processes, each store of its own.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_inspection_hours_follow_time_in_service import (
    DUE_IN_5,
    WHO,
    as_0_6_6_later_hours,
    case_a,
    figures,
    hundred,
    ingest,
    inspection,
    log,
    ok,
    programme,
    refused,
    rows,
)
from test_utc_offsets import write_as_0_6_6

from uas_workbench.ledger.model import Entry

H = 3600.0
STAMP = "2026-01-01 00:00 UTC"
CAN_HAVE = (
    "at_hours_s 150.0 h is more than the 100.0 h time in service X-1 can have reached by "
    f"{STAMP}, before its first log on 2026-02-01"
)
REFUSED_LATER = (
    f"refused (409): this would make entry 3 impossible (inspection.done on X-1, {STAMP}): "
    f"{CAN_HAVE}; retract or correct entry 3 first"
)
REFUSED_EARLIER = (
    f"refused (409): {CAN_HAVE}, counting the 100.0 h before the first log that entry 3 "
    "records; state fewer hours or correct entry 3 first"
)
REFUSED_OWN = (
    f"refused (422): {CAN_HAVE}, counting the 100.0 h before the first log that entry 2 records"
)
OVERDUE_55 = "the 100-hour inspection of aircraft X-1 is 55.0 h overdue, beyond its 10 h tolerance"
DISAGREE_A = (
    "the records disagree: the 100-hour inspection of 2026-03-01 is recorded at 210.0 h, more "
    "than the 10.0 h time in service the records give by then, counting 0.0 h before the first "
    "log; correct the inspection or the hours before the first log"
)
DISAGREE_BEFORE = (
    "the records disagree: the 100-hour inspection of 2026-01-01 is recorded at 150.0 h, more "
    "than the 100.0 h time in service the records give by then, counting 100.0 h before the "
    "first log; correct the inspection or the hours before the first log"
)
FROM_LEAST = ", counted from 10.0 h, the least time in service the records give at its date; "


def early(cwd: Path) -> None:
    """The verifier's records up to the later entry: 10 h flown on 2026-02-01 and 95 h on
    2026-04-01 (the logs make the aircraft); an annual inspection on 2025-11-15, derived at
    0 h (entry 1); 200 h before the first log entered on 2025-12-01 (entry 2); the 100-hour
    inspection on 2026-01-01 stated at 150 h (entry 3)."""
    logs(cwd)
    inspection(cwd, "X-1", "annual inspection", "2025-11-15T00:00Z")
    ok(cwd, "record", "time-in-service", "X-1", "200", *WHO, "--at", "2025-12-01T00:00Z")
    inspection(cwd, "X-1", "100-hour inspection", "2026-01-01T00:00Z", "--at-hours", "150")


def logs(cwd: Path) -> None:
    ingest(cwd, "X-1", "feb", datetime(2026, 2, 1, tzinfo=UTC), 10.0)
    ingest(cwd, "X-1", "apr", datetime(2026, 4, 1, tzinfo=UTC), 95.0)


def hours_entry(before_h: float, at: datetime) -> Entry:
    return Entry(
        id=None,
        subject="X-1",
        kind="time_in_service.set",
        occurred_utc=at,
        recorded_utc=at.replace(minute=5),
        entered_by="A. Tester, maintenance",
        statement="the hours were miscounted",
        details={"before_s": before_h * H},
        supersedes=None,
        reason=None,
        synthetic=False,
    )


# ---- objection 1: the bound before the first log, both orders ------------------------------


# The later entry of 100 h, or a correction of the 200 h to 100 h, would leave the inspection
# stating 150 h at a date when the aircraft had at most 100 h: refused, naming it; nothing is
# written, and the item still reads 55.0 h overdue, unserviceable. 0.6.6 and the first round
# accepted both, and the item then read "due in 45.0 h", serviceable.
@pytest.mark.parametrize("how", ["later", "correction"])
def test_an_hours_entry_below_an_earlier_inspection_is_refused(tmp_path: Path, how: str) -> None:
    early(tmp_path)
    before = rows(tmp_path / "fleet.sqlite")
    more = ["--at", "2026-06-01T00:00Z"] if how == "later" else [
        "--supersedes", "2", "--reason", "the 200 h were miscounted"]  # fmt: skip
    line = refused(tmp_path, "record", "time-in-service", "X-1", "100", *WHO, *more)
    assert line == REFUSED_LATER
    assert rows(tmp_path / "fleet.sqlite") == before
    prog = programme(tmp_path, "X-1", "2026-07-01T00:00Z")
    assert figures(hundred(prog["items"])) == (155.0, -55.0, "overdue", OVERDUE_55)
    assert prog["status"] == "unserviceable"


# The other order: the 100 h on record first (a later entry, or the only one), then the
# inspection stating 150 h before the first log. Refused, naming the entry of the hours.
@pytest.mark.parametrize("which", ["later", "only"])
def test_an_inspection_above_the_hours_before_the_first_log_is_refused(
    tmp_path: Path, which: str
) -> None:
    logs(tmp_path)
    inspection(tmp_path, "X-1", "annual inspection", "2025-11-15T00:00Z")
    first = "200" if which == "later" else "100"
    ok(tmp_path, "record", "time-in-service", "X-1", first, *WHO, "--at", "2025-12-01T00:00Z")
    if which == "later":
        ok(tmp_path, "record", "time-in-service", "X-1", "100", *WHO, "--at", "2026-06-01T00:00Z")
    before = rows(tmp_path / "fleet.sqlite")
    line = refused(tmp_path, "record", "inspection", "X-1", "100-hour inspection", *WHO,
                   "--at", "2026-01-01T00:00Z", "--at-hours", "150")  # fmt: skip
    assert line == (REFUSED_EARLIER if which == "later" else REFUSED_OWN)
    assert rows(tmp_path / "fleet.sqlite") == before


# A log with no UTC start may have been flown before the first log: 100 h entered and a 20 h
# undated log allow an inspection before the first log at 120 h, not at 125 h.
def test_the_bound_counts_the_logs_with_no_utc_start(tmp_path: Path) -> None:
    ingest(tmp_path, "X-1", "feb", datetime(2026, 2, 1, tzinfo=UTC), 10.0)
    inspection(tmp_path, "X-1", "annual inspection", "2025-11-15T00:00Z")
    ok(tmp_path, "record", "time-in-service", "X-1", "100", *WHO, "--at", "2025-12-01T00:00Z")
    ok(tmp_path, "ingest", "X-1", str(log(tmp_path / "no-gps-fix.bin", None, 20.0)))
    line = refused(tmp_path, "record", "inspection", "X-1", "100-hour inspection", *WHO,
                   "--at", "2026-01-01T00:00Z", "--at-hours", "125")  # fmt: skip
    assert line == (
        "refused (422): at_hours_s 125.0 h is more than the 120.0 h time in service X-1 can "
        f"have reached by {STAMP}, before its first log on 2026-02-01, counting the 100.0 h "
        "before the first log that entry 2 records"
    )
    inspection(tmp_path, "X-1", "100-hour inspection", "2026-01-01T00:00Z", "--at-hours", "120")


# ---- objection 2: a store written before this reads what the records still give ------------


# Case A as 0.6.6 left it (the later 0 h entry on record), with 220 h more flown in May: 0.6.6
# read 15.0 h overdue, the first round not known. The hours flown since the inspection are 315
# h, whatever the hours before the first log: 215.0 h overdue, unserviceable, and the sentence
# says the records disagree.
def test_a_disagreement_after_the_first_log_reads_the_hours_flown_since(tmp_path: Path) -> None:
    case_a(tmp_path)
    db = tmp_path / "fleet.sqlite"
    as_0_6_6_later_hours(db, "X-1")
    ingest(tmp_path, "X-1", "may", datetime(2026, 5, 1, tzinfo=UTC), 220.0)
    head = ok(tmp_path, "verify")
    before = rows(db)
    prog = programme(tmp_path, "X-1", "2026-07-01T00:00Z")
    message = (
        "the 100-hour inspection of aircraft X-1 is 215.0 h overdue, beyond its 10 h tolerance"
        + FROM_LEAST
        + DISAGREE_A
    )
    assert figures(hundred(prog["items"])) == (315.0, -215.0, "overdue", message)
    assert prog["status"] == "unserviceable"
    assert ok(tmp_path, "verify") == head
    assert rows(db) == before


# The verifier's records as 0.6.6 accepted them (the later 100 h entry on record). The dated
# logs since the inspection give 105 h: at least 5 h overdue, maybe past the tolerance. The
# records cannot say whether the aircraft is grounded: not known, never "due in 45.0 h". With
# 300 h more flown in May they can: at least 305.0 h overdue, unserviceable (0.6.6: 255.0 h).
@pytest.mark.parametrize("may", [0.0, 300.0], ids=["cannot-say", "overdue"])
def test_a_disagreement_before_the_first_log_reads_overdue_only_when_certain(
    tmp_path: Path, may: float
) -> None:
    early(tmp_path)
    db = tmp_path / "fleet.sqlite"
    write_as_0_6_6(db, hours_entry(100.0, datetime(2026, 6, 1, tzinfo=UTC)))
    if may:
        ingest(tmp_path, "X-1", "may", datetime(2026, 5, 1, tzinfo=UTC), may)
    head = ok(tmp_path, "verify")
    before = rows(db)
    prog = programme(tmp_path, "X-1", "2026-07-01T00:00Z")
    item = hundred(prog["items"])
    if may:
        message = (
            "the 100-hour inspection of aircraft X-1 is at least 305.0 h overdue, beyond its "
            "10 h tolerance, counting only the hours the dated logs give since it; "
            + DISAGREE_BEFORE
        )
        assert figures(item) == (405.0, -305.0, "overdue", message)
        assert prog["status"] == "unserviceable"
    else:
        message = (
            "the 100-hour inspection of aircraft X-1 cannot be measured on 2026-07-01: "
            + DISAGREE_BEFORE
        )
        unknown = {"unknown": DISAGREE_BEFORE}
        assert figures(item) == (unknown, unknown, "unknown", message)
        assert prog["status"] == {"unknown": message}
    assert ok(tmp_path, "verify") == head
    assert rows(db) == before


# ---- objection 3: on such a store, an entry that does not worsen it is accepted ------------


# Case A as 0.6.6 left it. A new entry of 200 h puts the records back in agreement: accepted
# (the first round refused every new entry of the hours), and the item reads 5.0 h left. A
# correction of the inspection stating 100 h leaves it less far past the bound: accepted,
# 5.0 h left with the sentence. A correction stating 250 h puts it further past: refused,
# naming the 0 h entry.
@pytest.mark.parametrize("write", ["hours-200", "inspection-100", "inspection-250"])
def test_on_a_store_holding_case_a_only_a_worse_entry_is_refused(
    tmp_path: Path, write: str
) -> None:
    case_a(tmp_path)
    db = tmp_path / "fleet.sqlite"
    as_0_6_6_later_hours(db, "X-1")
    before = rows(db)
    correct = ("record", "inspection", "X-1", "100-hour inspection", *WHO, "--supersedes", "3",
               "--reason", "restated", "--at-hours")  # fmt: skip
    if write == "inspection-250":
        assert refused(tmp_path, *correct, "250") == (
            "refused (409): at_hours_s 250.0 h is more than the time in service of X-1 at "
            "2026-03-01 00:00 UTC, 10.0 h, counting the 0.0 h before the first log that entry "
            "4 records; state fewer hours or correct entry 4 first"
        )
        assert rows(db) == before
        return
    if write == "hours-200":
        ok(tmp_path, "record", "time-in-service", "X-1", "200", *WHO, "--at", "2026-06-15T00:00Z")
        expected = DUE_IN_5
    else:
        ok(tmp_path, *correct, "100")
        expected = (
            DUE_IN_5 + FROM_LEAST + DISAGREE_A.replace("recorded at 210.0 h", "recorded at 100.0 h")
        )
    prog = programme(tmp_path, "X-1", "2026-07-01T00:00Z")
    assert figures(hundred(prog["items"])) == (95.0, 5.0, "due_soon", expected)
    assert prog["status"] == "serviceable"
