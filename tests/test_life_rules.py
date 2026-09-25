"""Unit tests for the component-life engine on hand-built records.

Rules under test, each from a public civil source cited in fleet.toml: limits by flight
hours, cycles and calendar time, whichever comes first (EASA MoC to OSO #3); the inspection
tolerance and carry-over of 14 CFR 91.409(b); calendar months that run to the end of the
month (14 CFR 91.409(a)); life status that travels with the part when it moves between
airframes (14 CFR 43.10); and the board state derived from all of it in civil vocabulary.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta

from uas_workbench.flight import FlightRecord, LifetimeCounter, Maybe, Unknown
from uas_workbench.life import (
    Component,
    DueItem,
    DueList,
    InspectionDone,
    Installation,
    LifePolicy,
    LifeRule,
    MaintenanceRecord,
    WorkOrder,
    board,
    due_list,
)

T0 = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)
AS_OF = datetime(2026, 4, 1, 12, 0, tzinfo=UTC)
H = 3600.0

POLICY = LifePolicy(
    due_soon_fraction=0.10,
    component_kinds={
        "battery pack": LifeRule("battery pack", cycles=300, calendar_months=24, source="EASA"),
        "propeller set": LifeRule("propeller set", hours=300, source="manual"),
    },
    inspections={
        "100-hour inspection": LifeRule(
            "100-hour inspection", hours=100, tolerance_hours=10, source="91.409(b)"
        ),
        "annual inspection": LifeRule("annual inspection", calendar_months=12, source="91.409(a)"),
    },
)


def flight(
    key: str,
    n: int,
    utc: Maybe[datetime],
    flight_s: Maybe[float] = 1800.0,
    *,
    at_boot: float = 0.0,
    ref: str | None = None,
) -> FlightRecord:
    return FlightRecord(
        source="px4",
        log_ref=ref or f"{key}-{n:02d}",
        licence="CC0",
        attribution="synthetic",
        firmware="test",
        log_span_s=1900.0,
        aircraft_key=key,
        utc_start=utc,
        flight_time_s=flight_s,
        arm_cycles=1,
        landings=1,
        battery_mah=Unknown("n/a"),
        battery_wh=Unknown("n/a"),
        fault_events=(),
        boot_count=n,
        lifetime=LifetimeCounter(at_boot, at_boot, 0.0),
        synthetic=True,
    )


def flights_of(table: dict[str, Sequence[FlightRecord]]) -> Callable[[str], Sequence[FlightRecord]]:
    return lambda key: table.get(key, ())


def record(key: str, **changes: object) -> MaintenanceRecord:
    base = MaintenanceRecord(
        aircraft_key=key,
        time_in_service_before_s=0.0,
        inspections=(
            InspectionDone("100-hour inspection", T0 - timedelta(days=1), 0.0),
            InspectionDone("annual inspection", T0 - timedelta(days=1), 0.0),
        ),
        work_orders=(),
        synthetic=True,
    )
    return dataclasses.replace(base, **changes)  # type: ignore[arg-type]


def battery(id_: str, key: str, since: date, cycles_before: int = 0) -> Component:
    return Component(
        id=id_,
        kind="battery pack",
        in_service_since=since,
        hours_s_before=0.0,
        cycles_before=cycles_before,
        installations=(Installation(key, T0 - timedelta(days=30), None),),
        synthetic=True,
    )


def item(due: DueList, subject: str, basis: str) -> DueItem:
    (found,) = [i for i in due.items if i.subject == subject and i.basis == basis]
    return found


def compute(
    key: str,
    maintenance: MaintenanceRecord | None,
    components: Sequence[Component] = (),
    flights: dict[str, Sequence[FlightRecord]] | None = None,
    as_of: datetime = AS_OF,
) -> DueList:
    return due_list(
        key,
        maintenance=maintenance,
        components=components,
        flights_of=flights_of(flights or {}),
        policy=POLICY,
        as_of=as_of,
    )


def test_whichever_comes_first_cycles_expire_before_calendar() -> None:
    logs = [flight("A", n, T0 + timedelta(days=n)) for n in range(1, 11)]
    parts = [battery("BAT-1", "A", date(2025, 6, 1), cycles_before=295)]
    due = compute("A", record("A"), parts, {"A": logs})
    cycles = item(due, "battery pack BAT-1", "cycles")
    assert (cycles.used, cycles.limit, cycles.remaining) == (305, 300, -5)
    assert cycles.state == "overdue" and cycles.source == "EASA"
    assert cycles.message == (
        "battery pack BAT-1 on aircraft A is 5 cycles past its 300-cycle life limit"
    )
    cal = item(due, "battery pack BAT-1", "calendar")
    assert cal.state == "ok" and cal.unit == "days"
    assert cal.message == (
        "battery pack BAT-1 on aircraft A has 425 days left of its 24-calendar-month "
        "life limit, due by 2027-05-31"
    )
    assert board(due).status == "unserviceable"
    assert board(due).reasons == (cycles.message,)


def test_due_soon_is_a_fraction_of_the_interval() -> None:
    logs = [flight("A", 1, T0, 2.0 * H)]
    prop = Component(
        "PROP-1",
        "propeller set",
        date(2025, 1, 1),
        290.0 * H,
        0,
        (Installation("A", T0 - timedelta(days=1), None),),
        True,
    )
    due = compute("A", record("A"), [prop], {"A": logs})
    hours = item(due, "propeller set PROP-1", "hours")
    assert (hours.used, hours.remaining, hours.state, hours.unit) == (292.0, 8.0, "due_soon", "h")
    assert hours.message == (
        "propeller set PROP-1 on aircraft A has 8.0 h left of its 300 h life limit"
    )
    assert board(due).status == "serviceable" and board(due).reasons == ()


def test_inspection_tolerance_allows_ten_hours_then_grounds() -> None:
    within = compute("A", record("A", time_in_service_before_s=104.2 * H))
    insp = item(within, "the 100-hour inspection", "hours")
    assert (round(insp.remaining, 1), insp.tolerance, insp.state) == (
        -4.2,
        10.0,
        "overdue_within_tolerance",
    )
    assert insp.message == (
        "the 100-hour inspection of aircraft A is 4.2 h overdue, within the 10 h tolerance "
        "that allows flight only to reach a place where the inspection can be done"
    )
    assert board(within).status == "serviceable with deferred defects"
    assert board(within).reasons == (insp.message,)

    beyond = compute("A", record("A", time_in_service_before_s=111.0 * H))
    insp = item(beyond, "the 100-hour inspection", "hours")
    assert insp.state == "overdue"
    assert insp.message == (
        "the 100-hour inspection of aircraft A is 11.0 h overdue, beyond its 10 h tolerance"
    )
    assert board(beyond).status == "unserviceable"


def test_overflown_hours_count_toward_the_next_inspection() -> None:
    # Done 4 h late; 14 CFR 91.409(b): the excess is included in the next 100 hours.
    late = InspectionDone("100-hour inspection", T0, 104.0 * H, carried_over_s=4.0 * H)
    due = compute("A", record("A", time_in_service_before_s=195.0 * H, inspections=(late,)))
    insp = item(due, "the 100-hour inspection", "hours")
    assert (insp.used, insp.remaining, insp.state) == (95.0, 5.0, "due_soon")
    assert insp.message == "the 100-hour inspection of aircraft A is due in 5.0 h"


def test_calendar_months_run_to_the_end_of_the_month() -> None:
    annual = InspectionDone("annual inspection", datetime(2025, 3, 10, tzinfo=UTC), 0.0)
    hundred = InspectionDone("100-hour inspection", T0, 0.0)
    rec = record("A", inspections=(annual, hundred))
    on_the_day = compute("A", rec, as_of=datetime(2026, 3, 31, 23, tzinfo=UTC))
    insp = item(on_the_day, "the annual inspection", "calendar")
    assert (insp.remaining, insp.state) == (0, "due_soon")
    assert insp.message == "the annual inspection of aircraft A is due by 2026-03-31, in 0 days"
    next_day = compute("A", rec, as_of=datetime(2026, 4, 1, 0, 30, tzinfo=UTC))
    insp = item(next_day, "the annual inspection", "calendar")
    assert (insp.remaining, insp.state) == (-1, "overdue")
    assert insp.message == (
        "the annual inspection of aircraft A was due by 2026-03-31 and is 1 day overdue"
    )
    assert board(next_day).status == "unserviceable"


def test_life_status_travels_with_the_part_between_airframes() -> None:
    moved_at = T0 + timedelta(days=5)
    a_logs = [flight("A", n, T0 + timedelta(days=n), 1000.0) for n in range(1, 9)]  # 4 before
    b_logs = [flight("B", n, T0 + timedelta(days=n), 500.0) for n in range(1, 11)]  # 5 after
    part = Component(
        "BAT-M",
        "battery pack",
        date(2025, 1, 1),
        10.0 * H,
        100,
        (Installation("A", T0, moved_at), Installation("B", moved_at, None)),
        True,
    )
    table: dict[str, Sequence[FlightRecord]] = {"A": a_logs, "B": b_logs}
    due = compute("B", record("B"), [part], table)
    cycles = item(due, "battery pack BAT-M", "cycles")
    assert cycles.used == 100 + 4 + 5
    assert cycles.component_id == "BAT-M"
    assert due.usage["BAT-M"].hours_s == 10.0 * H + 4 * 1000.0 + 5 * 500.0
    # On A the part is no longer installed, so it does not appear on A's list.
    on_a = compute("A", record("A"), [part], table)
    assert [i.subject for i in on_a.items if i.component_id] == []


def test_flights_without_a_utc_start_are_counted_for_the_airframe_only() -> None:
    logs = [flight("A", 1, T0, 1000.0), flight("A", 2, Unknown("no GPS fix"), 1000.0)]
    due = compute("A", record("A"), [battery("BAT-1", "A", date(2025, 1, 1))], {"A": logs})
    assert due.time_in_service_s == 2000.0
    assert item(due, "battery pack BAT-1", "cycles").used == 1
    assert due.notes == (
        "flight A-02 of aircraft A has no UTC start and is not counted against any component",
    )


def test_duplicate_logs_are_skipped_and_unlogged_flight_is_credited() -> None:
    logs = [
        flight("A", 1, T0, 1000.0, at_boot=0.0),
        flight("A", 1, T0 + timedelta(seconds=0.2), 1000.0, at_boot=0.0, ref="A-01-again"),
        flight("A", 2, T0 + timedelta(days=1), 1000.0, at_boot=1400.0),  # 400 s flown unlogged
    ]
    due = compute("A", record("A"), [battery("BAT-1", "A", date(2025, 1, 1))], {"A": logs})
    assert due.time_in_service_s == 2400.0
    assert due.usage["BAT-1"].hours_s == 2400.0
    assert item(due, "battery pack BAT-1", "cycles").used == 2
    assert due.notes == ("log A-01-again of aircraft A is a duplicate of A-01 and is not counted",)


def test_an_inspection_never_recorded_counts_from_zero() -> None:
    due = compute("A", record("A", time_in_service_before_s=50.0 * H, inspections=()))
    assert item(due, "the 100-hour inspection", "hours").used == 50.0
    assert "no completed 100-hour inspection is recorded for aircraft A" in due.notes[0]


def test_board_precedence_and_reasons_in_civil_vocabulary() -> None:
    orders = (
        WorkOrder(T0, "[synthetic] replacement propeller set on order", "awaiting_parts", True),
        WorkOrder(T0, "[synthetic] battery connector inspection", "in_work", True),
        WorkOrder(T0, "[synthetic] cosmetic cover damage", "deferred", True),
    )
    due = compute("A", record("A", time_in_service_before_s=104.0 * H, work_orders=orders))
    result = board(due)
    assert result.status == "AOG"
    assert result.reasons == (
        "aircraft A is on the ground awaiting parts since 2026-03-01: "
        "[synthetic] replacement propeller set on order",
        "aircraft A is in maintenance since 2026-03-01: [synthetic] battery connector inspection",
        "the 100-hour inspection of aircraft A is 4.0 h overdue, within the 10 h tolerance "
        "that allows flight only to reach a place where the inspection can be done",
        "aircraft A carries a deferred defect since 2026-03-01: [synthetic] cosmetic cover damage",
    )
    deferred = compute("A", record("A", work_orders=orders[2:]))
    assert board(deferred).status == "serviceable with deferred defects"
    in_work = compute("A", record("A", work_orders=orders[1:2]))
    assert board(in_work).status == "in maintenance"


def test_no_maintenance_record_is_unknown_with_its_reason() -> None:
    due = compute("A", None)
    assert due.items == () and due.has_record is False
    assert board(due).status == Unknown("no maintenance record entered for this aircraft")
    assert board(due).reasons == ()
