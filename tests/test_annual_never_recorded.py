"""An inspection with a calendar limit only and no completion on record is not known, never
skipped (issue #150).

The annual inspection has a calendar limit only (14 CFR 91.409(a), "within the preceding 12
calendar months"). With no completed annual on record, the engine skipped its calendar basis,
the aircraft carried no annual item at all, and the board read serviceable; the note said
its interval was "counted from zero time in service", which for a calendar-only rule counts
nothing. Now such an inspection has an item in the not-known state with a sentence saying
no completion is on record, and the board is not known with that reason unless something
grounds the aircraft, which wins. An annual on record is counted as before.

The command-line cases run real `uasw` processes on a store of their own: one aircraft, the
committed PX4 log (2026-06-12), its hours before that log entered as 0 h on 2026-06-01.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

from test_life_rules import POLICY, T0, compute, flight, item, record

from uas_workbench.flight import Unknown
from uas_workbench.life import InspectionDone, WorkOrder, board
from uas_workbench.life.cue import item_cue, problems, reason_cues, status_cue

FIXTURES = Path(__file__).parent / "fixtures"
LOG = next((FIXTURES / "px4").glob("*.ulg"))
WHO = ["--by", "A. Tester, maintenance", "--statement", "work done as described"]
H = 3600.0
D = timedelta(days=1)


def never_done(key: str, on: str) -> str:
    return (
        f"the annual inspection of aircraft {key} cannot be counted on {on}: no completed "
        "annual inspection is recorded, so its 12-calendar-month interval has no date to "
        "count from"
    )


NOTE = "no completed annual inspection is recorded for aircraft {}; its interval cannot be counted"


def uasw(cwd: Path, *args: str) -> int:
    """Run one `uasw` command in a process of its own; its exit code."""
    env = {k: v for k, v in os.environ.items() if k != "UASW_WRITE_TOKEN"}
    r = subprocess.run(
        [sys.executable, "-m", "uas_workbench.service.cli", "--db", "fleet.sqlite", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    return r.returncode


def one_aircraft(tmp_path: Path, *annual_at: str) -> None:
    assert uasw(tmp_path, "ingest", "X-1", str(LOG)) == 0
    assert uasw(tmp_path, "record", "time-in-service", "X-1", "0", *WHO,
                "--at", "2026-06-01T00:00Z") == 0  # fmt: skip
    for at in annual_at:
        assert uasw(tmp_path, "record", "inspection", "X-1", "annual inspection", *WHO,
                    "--at", at) == 0  # fmt: skip


def programme(tmp_path: Path, as_of: str) -> dict[str, Any]:
    name = f"pack-{as_of[:10]}"
    assert uasw(tmp_path, "evidence", "X-1", "--out", f"{name}.html", "--json",
                "--as-of", as_of) == 0  # fmt: skip
    pack = json.loads((tmp_path / f"{name}.json").read_text(encoding="utf-8"))
    out: dict[str, Any] = pack["programme"]
    return out


def annual_items(prog: dict[str, Any]) -> list[dict[str, Any]]:
    return [i for i in prog["items"] if i["subject"] == "the annual inspection"]


# 1. The case of the issue: no annual on record. The board is not known with the annual's
#    sentence, the item is there in the not-known state, and the note no longer says the
#    calendar interval is counted from zero time in service.
def test_no_annual_on_record_is_not_known_and_never_serviceable(tmp_path: Path) -> None:
    one_aircraft(tmp_path)
    prog = programme(tmp_path, "2026-07-01T00:00Z")
    sentence = never_done("X-1", "2026-07-01")
    assert prog["status"] == {"unknown": sentence}
    assert prog["status_reasons"] == [sentence]
    (annual,) = annual_items(prog)
    assert annual["state"] == "unknown"
    assert annual["basis"] == "calendar" and annual["unit"] == "days"
    assert annual["limit"] == 12.0 and annual["tolerance"] == 0.0
    assert annual["used"] == {"unknown": sentence} and annual["remaining"] == {"unknown": sentence}
    assert annual["message"] == sentence
    assert len(prog["items"]) == 2  # the 100-hour inspection, still counted from zero
    assert NOTE.format("X-1") in prog["notes"]
    assert sum("counted from zero time in service" in n for n in prog["notes"]) == 1
    assert sum("annual inspection is recorded" in n for n in prog["notes"]) == 2


# 2. A calendar-only rule satisfied by a recorded inspection is counted as before.
def test_an_annual_on_record_is_counted_from_its_date(tmp_path: Path) -> None:
    one_aircraft(tmp_path, "2026-06-01T00:00Z")
    prog = programme(tmp_path, "2026-07-01T00:00Z")
    assert prog["status"] == "serviceable" and prog["status_reasons"] == []
    (annual,) = annual_items(prog)
    assert annual["state"] == "ok" and annual["remaining"] == 364.0
    assert annual["message"] == (
        "the annual inspection of aircraft X-1 is due by 2027-06-30, in 364 days"
    )
    assert prog["notes"] == []


# 3. A date before the first annual: the records then hold none (they are folded from the
#    entries that had occurred by that date), so the annual is not known there, and counted
#    from its date once it is on record.
def test_a_date_before_the_first_annual_is_not_known(tmp_path: Path) -> None:
    one_aircraft(tmp_path, "2026-06-20T00:00Z")
    before = programme(tmp_path, "2026-06-15T00:00Z")
    assert before["status"] == {"unknown": never_done("X-1", "2026-06-15")}
    assert [a["state"] for a in annual_items(before)] == ["unknown"]
    after = programme(tmp_path, "2026-07-01T00:00Z")
    assert after["status"] == "serviceable"
    assert [a["remaining"] for a in annual_items(after)] == [364.0]


# 4. The engine on hand-built records: the item, its cue, the board's cue, and precedence.
def test_the_engine_item_cue_and_board() -> None:
    rec = record(
        "A", inspections=(InspectionDone("100-hour inspection", T0 - D, 0.0),)
    )
    due = compute("A", rec)
    annual = item(due, "the annual inspection", "calendar")
    sentence = never_done("A", "2026-04-01")
    assert annual.state == "unknown" and annual.message == sentence
    assert annual.used == Unknown(sentence) and annual.remaining == Unknown(sentence)
    result = board(due)
    assert result.status == Unknown(sentence) and result.reasons == (sentence,)
    # The 100-hour inspection is on record, so no note at all: the item says it.
    assert due.notes == ()
    cue = item_cue(annual, due.as_of)
    assert cue == "annual insp: none recorded, not known"
    assert problems(cue, annual, due.as_of) == []
    assert status_cue(result.status) == "not known: inspection not recorded"
    assert reason_cues(due, result) == (cue,)
    # An open work order still wins; the sentence stays among the reasons.
    grounded = compute(
        "A",
        record(
            "A",
            inspections=(InspectionDone("100-hour inspection", T0 - D, 0.0),),
            work_orders=(WorkOrder(T0, "[synthetic] motor mount check", "in_work", True),),
        ),
    )
    assert board(grounded).status == "in maintenance"
    assert sentence in board(grounded).reasons


# 5. With the time in service not known as well (before the first log, hours entered), the
#    board keeps that reason first, and the annual's sentence is a reason of its own.
def test_before_the_first_log_both_reasons_are_given() -> None:
    rec = record("A", time_in_service_before_s=50.0 * H, inspections=())
    due = compute("A", rec, flights={"A": [flight("A", 1, T0, 1800.0)]}, as_of=T0 - D)
    assert [(i.subject, i.state) for i in due.items] == [
        ("the 100-hour inspection", "unknown"),
        ("the annual inspection", "unknown"),
    ]
    result = board(due)
    assert result.status == due.time_in_service_s
    assert result.reasons[1] == never_done("A", "2026-02-28")
    assert POLICY.inspections["annual inspection"].limits() == (("calendar", 12),)
    assert due.notes == (NOTE.format("A"),)
