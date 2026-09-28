"""Time in service before an aircraft's first log is not known, and an annual inspection
counts toward the 100-hour rule.

An operator records the hours flown before the first log this tool holds as one total. The
tool knows that total was reached by the first log, and nothing about the dates before it.
Until 0.3.0 the engine used the whole total at any earlier date, so an aircraft whose 110 h
were entered read as 110 h on a date months before its first log, and its 100-hour
inspection as 10.0 h overdue there: a number no record supports, printed with a tolerance
state that reads as leave to fly. Now the time in service at such a date is not known, the
inspection items that depend on it are not known, and the board is not known with that
reason, unless an open work order or a life limit already grounds the aircraft, which wins.

14 CFR 91.409(b) accepts "an annual or 100-hour inspection" within the preceding 100 hours
of time in service; the engine counted only the 100-hour inspection.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from test_life_rules import POLICY, T0, battery, compute, flight, item, record

from uas_workbench.evidence import build_pack, render_html
from uas_workbench.fleet import load_config
from uas_workbench.fleet.showcase import showcase
from uas_workbench.fleet.synthetic import generate
from uas_workbench.flight import Unknown, is_known
from uas_workbench.ledger import Entry, LedgerError, append, project
from uas_workbench.life import InspectionDone, WorkOrder, board, due_list
from uas_workbench.life.engine import time_in_service
from uas_workbench.service.app import create_app
from uas_workbench.service.store import Store

FIXTURES = Path(__file__).parent / "fixtures"
CONFIG = load_config()
H = 3600.0
D = timedelta(days=1)
EARLY = datetime(2026, 7, 1, tzinfo=UTC)  # before every synthetic aircraft's first log
PLAN_EARLY = (datetime(2026, 8, 3, tzinfo=UTC), datetime(2026, 8, 6, 10, tzinfo=UTC))
LATER = datetime(2026, 10, 1, tzinfo=UTC)  # after every synthetic flight

FIRST_LOG = T0  # the hand-built aircraft A flies once, at T0
NOT_KNOWN = (
    "the time in service of aircraft A before its first log on 2026-03-01 is not known: "
    "50.0 h had been flown by then, on dates the records do not hold"
)
CANNOT_MEASURE = (
    "the 100-hour inspection of aircraft A cannot be measured on 2026-02-28: " + NOT_KNOWN
)


def flights_a() -> dict[str, Any]:
    return {"A": [flight("A", 1, FIRST_LOG, 1800.0)]}


# 1. Before the first log, with hours entered, time in service is not known; from the first
#    log on it is the total plus the flights. With no hours entered it is known to be zero.
def test_time_in_service_before_the_first_log_is_not_known() -> None:
    before = FIRST_LOG - D
    rec = record("A", time_in_service_before_s=50.0 * H, inspections=())
    due = compute("A", rec, flights=flights_a(), as_of=before)
    assert due.has_record is True
    assert due.time_in_service_s == Unknown(NOT_KNOWN)
    assert time_in_service("A", 50.0 * H, flights_a()["A"], until=before) == Unknown(NOT_KNOWN)

    at_first = compute("A", rec, flights=flights_a(), as_of=FIRST_LOG)
    assert at_first.time_in_service_s == 50.0 * H + 1800.0
    assert time_in_service("A", 50.0 * H, flights_a()["A"], until=FIRST_LOG) == 50.0 * H + 1800.0
    assert time_in_service("A", 50.0 * H, flights_a()["A"]) == 50.0 * H + 1800.0  # now

    zero = compute("A", record("A", inspections=()), flights=flights_a(), as_of=before)
    assert zero.time_in_service_s == 0.0
    assert item(zero, "the 100-hour inspection", "hours").state == "ok"
    # An aircraft with no dated log has no first log; the total stands, as before.
    none = compute("A", rec, as_of=before)
    assert none.time_in_service_s == 50.0 * H


# 2. The inspection items that depend on it are not known, in a state of their own, with
#    used and remaining not known; the calendar item does not depend on it and is computed.
def test_inspection_items_that_depend_on_it_are_not_known() -> None:
    rec = record(
        "A",
        time_in_service_before_s=50.0 * H,
        inspections=(InspectionDone("annual inspection", T0 - 200 * D, 0.0),),
    )
    due = compute("A", rec, flights=flights_a(), as_of=FIRST_LOG - D)
    hours = item(due, "the 100-hour inspection", "hours")
    assert hours.state == "unknown"
    assert hours.used == Unknown(NOT_KNOWN) and hours.remaining == Unknown(NOT_KNOWN)
    assert hours.limit == 100.0 and hours.tolerance == 10.0
    assert hours.message == CANNOT_MEASURE
    annual = item(due, "the annual inspection", "calendar")
    assert annual.state == "ok" and is_known(annual.remaining)
    assert not any("counted from zero" in n for n in due.notes)


# 3. The board: not known, with the reason, and never serviceable. An open work order or a
#    life limit already past wins, and the not-known sentence stays among the reasons.
def test_the_board_is_not_known_unless_something_known_grounds_the_aircraft() -> None:
    before = FIRST_LOG - D
    rec = record("A", time_in_service_before_s=50.0 * H, inspections=())
    plain = board(compute("A", rec, flights=flights_a(), as_of=before))
    assert plain.status == Unknown(NOT_KNOWN)
    assert plain.reasons == (CANNOT_MEASURE,)

    deferred = dataclasses.replace(
        rec, work_orders=(WorkOrder(T0 - 10 * D, "[synthetic] cover damage", "deferred", True),)
    )
    b = board(compute("A", deferred, flights=flights_a(), as_of=before))
    assert b.status == Unknown(NOT_KNOWN)  # deferred defects cannot be asserted either
    assert b.reasons == (
        CANNOT_MEASURE,
        "aircraft A carries a deferred defect since 2026-02-19: [synthetic] cover damage",
    )

    expired = battery("BAT-1", "A", date(2020, 1, 1))
    b = board(compute("A", rec, [expired], flights=flights_a(), as_of=before))
    assert b.status == "unserviceable"
    assert b.reasons[0].startswith("battery pack BAT-1 on aircraft A passed its 24-calendar-month")
    assert CANNOT_MEASURE in b.reasons

    in_work = dataclasses.replace(
        rec, work_orders=(WorkOrder(T0 - 10 * D, "[synthetic] connector check", "in_work", True),)
    )
    b = board(compute("A", in_work, flights=flights_a(), as_of=before))
    assert b.status == "in maintenance" and CANNOT_MEASURE in b.reasons


# 4. An annual inspection counts toward the 100-hour rule (14 CFR 91.409(b): "an annual or
#    100-hour inspection"); the later of the two starts the interval.
def test_an_annual_inspection_counts_toward_the_100_hour_rule() -> None:
    rec = record(
        "A",
        time_in_service_before_s=90.0 * H,
        inspections=(
            InspectionDone("100-hour inspection", T0 - 30 * D, 0.0),
            InspectionDone("annual inspection", T0 - 1 * D, 40.0 * H),
        ),
    )
    due = compute("A", rec)
    hours = item(due, "the 100-hour inspection", "hours")
    assert hours.used == 50.0 and hours.remaining == 50.0 and hours.state == "ok"
    assert POLICY.inspections["100-hour inspection"].satisfied_by == ("annual inspection",)
    assert CONFIG.life.inspections["100-hour inspection"].satisfied_by == ("annual inspection",)
    # An annual with a carry-over of its own is honoured the same way.
    carried = dataclasses.replace(
        rec,
        inspections=(InspectionDone("annual inspection", T0 - 1 * D, 40.0 * H, 5.0 * H),),
    )
    assert item(compute("A", carried), "the 100-hour inspection", "hours").used == 55.0
    # With neither recorded, the note names both.
    neither = compute("A", record("A", time_in_service_before_s=50.0 * H, inspections=()))
    assert (
        "no completed 100-hour inspection or annual inspection is recorded for aircraft A; "
        "its interval is counted from zero time in service"
    ) in neither.notes


# 5. The seeded fleet: SYN-03 on 2026-07-01, before its first log, is not known, and the
#    "10.0 h overdue" sentence is gone; from its first log on, nothing changed.
def seeded() -> Store:
    s = Store(":memory:")
    s.add_fleet(generate(CONFIG))
    s.add_fleet(showcase(FIXTURES))
    return s


def state(store: Store, key: str, at: datetime) -> tuple[Any, tuple[str, ...], Any]:
    p = store.projection(at)
    due = due_list(
        key,
        maintenance=p.maintenance.get(key),
        components=p.components,
        flights_of=store.flights,
        policy=CONFIG.life,
        as_of=at,
        tolerance_s=CONFIG.tolerance_s,
    )
    b = board(due)
    return b.status, b.reasons, due


def test_syn_03_before_its_first_log_is_not_known_not_overdue_within_tolerance() -> None:
    s = seeded()
    status, reasons, due = state(s, "SYN-03", EARLY)
    assert isinstance(status, Unknown)
    assert status.reason == (
        "the time in service of aircraft SYN-03 before its first log on 2026-08-11 is not "
        "known: 110.0 h had been flown by then, on dates the records do not hold"
    )
    assert not any("overdue" in r for r in reasons)
    assert reasons == (
        "the 100-hour inspection of aircraft SYN-03 cannot be measured on 2026-07-01: "
        + status.reason,
    )
    assert due.time_in_service_s == Unknown(status.reason)
    for at in PLAN_EARLY:  # the scene plan's early times lie before SYN-03's first log too
        assert isinstance(state(s, "SYN-03", at)[0], Unknown)
    # From the first log on: known, and the same values as before this change.
    after = state(s, "SYN-03", datetime(2026, 8, 11, 10, tzinfo=UTC))
    assert after[0] == "serviceable with deferred defects"
    assert is_known(after[2].time_in_service_s) and after[2].time_in_service_s > 110.0 * H
    late = state(s, "SYN-03", LATER)
    assert late[0] == "serviceable with deferred defects"
    assert late[1] == (
        "the 100-hour inspection of aircraft SYN-03 is 3.6 h overdue, within the 10 h tolerance "
        "that allows flight only to reach a place where the inspection can be done",
    )


# 6. Through the API: the due list, the board list and the fleet due list, at that date.
def test_the_api_carries_the_not_known_state() -> None:
    api = TestClient(create_app(seeded(), write_token=None), client=("127.0.0.1", 50000))
    at = {"as_of": "2026-07-01T00:00:00Z"}
    due = api.get("/aircraft/SYN-03/due", params=at).json()
    assert set(due["status"]) == {"unknown"} and "not known" in due["status"]["unknown"]
    assert set(due["time_in_service_s"]) == {"unknown"}
    hours = next(i for i in due["items"] if i["basis"] == "hours" and i["component_id"] is None)
    assert hours["state"] == "unknown"
    assert set(hours["used"]) == {"unknown"} and set(hours["remaining"]) == {"unknown"}
    assert hours["message"] in due["status_reasons"]
    row = next(a for a in api.get("/aircraft", params=at).json() if a["key"] == "SYN-03")
    assert row["status"] == due["status"] and row["status_reasons"] == due["status_reasons"]
    assert row["overdue"] == 0 and row["due_soon"] == 0
    fleet = api.get("/fleet/due", params=at).json()
    unknowns = [i for i in fleet if i["state"] == "unknown"]
    assert {i["aircraft_key"] for i in unknowns} >= {"SYN-03"}
    ranks = [
        {"overdue": 0, "unknown": 1, "overdue_within_tolerance": 2, "due_soon": 3}[i["state"]]
        for i in fleet
    ]
    assert ranks == sorted(ranks)
    # At the current time nothing about SYN-03 changed.
    now = api.get("/aircraft/SYN-03/due").json()
    assert now["status"] == "serviceable with deferred defects"


# 7. The evidence pack at that date says not known, in the JSON and in the HTML.
def test_the_evidence_pack_carries_the_not_known_state() -> None:
    pack = build_pack(seeded(), "SYN-03", CONFIG, as_of=EARLY, commit=None, generated_utc=LATER)
    assert set(pack["usage"]["time_in_service_s"]) == {"unknown"}
    assert "not known" in pack["usage"]["time_in_service_s"]["unknown"]
    assert set(pack["programme"]["status"]) == {"unknown"}
    assert pack["programme"]["status_reasons"]
    hours = next(i for i in pack["programme"]["items"] if i["basis"] == "hours")
    assert hours["state"] == "unknown" and set(hours["used"]) == {"unknown"}
    html = render_html(pack)
    assert "not known" in html and "10.0 h overdue" not in html


# 8. Writes: an inspection whose hours the tool would derive at a date before the first log
#    is refused, since the value would be invented; stated hours are accepted.
def test_deriving_inspection_hours_before_the_first_log_is_refused() -> None:
    s = seeded()

    def entry(details: dict[str, Any], at: datetime) -> Entry:
        return Entry(
            id=None,
            subject="SYN-03",
            kind="inspection.done",
            occurred_utc=at,
            recorded_utc=LATER,
            entered_by="A. Tester, maintenance",
            statement="as described",
            details=details,
            supersedes=None,
            reason=None,
            synthetic=False,
        )

    before = s.entry_count()
    with pytest.raises(LedgerError) as exc:
        append(s, entry({"name": "100-hour inspection"}, EARLY), policy=CONFIG.life, now=LATER)
    assert exc.value.status == 422
    assert exc.value.detail == (
        "at_hours_s cannot be derived: the time in service of aircraft SYN-03 before its first "
        "log on 2026-08-11 is not known: 110.0 h had been flown by then, on dates the records "
        "do not hold; state at_hours_s"
    )
    assert s.entry_count() == before
    stated = append(
        s,
        entry({"name": "100-hour inspection", "at_hours_s": 100.0 * H}, EARLY),
        policy=CONFIG.life,
        now=LATER,
    )
    assert stated.details["at_hours_s"] == 100.0 * H and stated.details["derived"] is False
    # From the first log on, derivation works as before.
    derived = append(
        s,
        entry({"name": "100-hour inspection"}, datetime(2026, 8, 11, 10, tzinfo=UTC)),
        policy=CONFIG.life,
        now=LATER,
    )
    assert derived.details["derived"] is True and derived.details["at_hours_s"] > 110.0 * H


# 9. Nothing dated after every first log changes: the states at 2026-10-01 are the same
#    as before, so the published evidence sample and the recorded runs stand.
def test_states_after_every_first_log_are_unchanged() -> None:
    s = seeded()
    expected = {
        "SYN-01": "serviceable",
        "SYN-02": "serviceable",
        "SYN-03": "serviceable with deferred defects",
        "SYN-04": "unserviceable",
        "SYN-05": "in maintenance",
        "SYN-06": Unknown("no maintenance record entered for this aircraft"),
        "SYN-07": "AOG",
    }
    assert {k: state(s, k, LATER)[0] for k in expected} == expected
    p = project(s.entries())
    assert all(
        is_known(state(s, k, LATER)[2].time_in_service_s) for k in expected if k in p.maintenance
    )
