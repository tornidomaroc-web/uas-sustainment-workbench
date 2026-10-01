"""A limit counted in completed units is reached when its last unit completes.

One rule, three consequences, each under test here on hand-built records:

  reached   a part whose cycles or hours have reached its limit, exactly or past it, grounds
            the aircraft: 300 of 300 cycles is the whole of the life, and the next flight would
            be flown on a part with nothing left. The sentence says "has reached", the cue says
            "reached", and the board agrees with the ledger, which has refused to fit such a
            part since 0.3.0 (14 CFR 43.10(c): "after it has reached its life limit").
  landed    a flight's cycle and hours count once the flight is over. The record holds no
            landing time: flight time is a total airborne within the log, not an interval. The
            end of the log is the last instant its flight can have ended, so a log counts from
            its end, never from its start and never partway through.
  calendar  a calendar life is counted in days, and its last day completes at midnight: on the
            last day something is left, so the part is valid on the board and may be fitted;
            from the next day it is past, on the board and at the install alike (14 CFR
            91.409(a): a calendar month runs to its last day).

So the board and the install refusal agree at the limit on every basis.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from uas_workbench.fleet import load_config
from uas_workbench.fleet.model import Aircraft, Fleet
from uas_workbench.fleet.synthetic import generate
from uas_workbench.flight import FlightRecord, LifetimeCounter, Maybe, Unknown
from uas_workbench.ledger import Entry, LedgerError, append
from uas_workbench.life import (
    Board,
    Component,
    DueItem,
    DueList,
    Installation,
    MaintenanceRecord,
    board,
    due_list,
)
from uas_workbench.life.cue import CUE_LIMIT, item_cue, problems, reason_cues
from uas_workbench.life.engine import component_usage, time_in_service
from uas_workbench.service.app import aircraft_view
from uas_workbench.service.store import Store

CONFIG = load_config()
POLICY = CONFIG.life
H = 3600.0
S = timedelta(seconds=1)
D = timedelta(days=1)
KEY = "AC-1"
FITTED = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)
TAKEOFF = datetime(2026, 3, 10, 10, 0, tzinfo=UTC)  # the log starts here
FLIGHT_S, SPAN_S = 1800.0, 1900.0
LOG_END = TAKEOFF + timedelta(seconds=SPAN_S)  # 10:31:40, the log's last instant
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
BY = "A. Tester, maintenance"
TAIL = "a life-limited part that has reached its life limit is replaced, not fitted again"


def flight(
    n: int,
    utc: Maybe[datetime],
    flight_s: Maybe[float] = FLIGHT_S,
    *,
    at_boot: float = 0.0,
    key: str = KEY,
) -> FlightRecord:
    return FlightRecord(
        source="px4",
        log_ref=f"{key}-{n:02d}",
        licence="CC0",
        attribution="synthetic",
        firmware="test",
        log_span_s=SPAN_S,
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


def pack(id_: str, cycles_before: int, since: date = date(2025, 6, 1)) -> Component:
    return Component(
        id_, "battery pack", since, 0.0, cycles_before, (Installation(KEY, FITTED, None),), True
    )


def prop(id_: str, hours_before: float) -> Component:
    return Component(
        id_,
        "propeller set",
        date(2025, 6, 1),
        hours_before * H,
        0,
        (Installation(KEY, FITTED, None),),
        True,
    )


def compute(
    parts: Sequence[Component],
    logs: Sequence[FlightRecord] = (),
    as_of: datetime = NOW,
    maintenance: MaintenanceRecord | None = None,
) -> DueList:
    return due_list(
        KEY,
        maintenance=maintenance,
        components=parts,
        flights_of=lambda key: logs if key == KEY else (),
        policy=POLICY,
        as_of=as_of,
    )


def item(due: DueList, component_id: str, basis: str) -> DueItem:
    (found,) = [i for i in due.items if i.component_id == component_id and i.basis == basis]
    return found


# ---- reached: exactly at the limit grounds the aircraft --------------------------------


def test_a_pack_at_exactly_300_of_300_cycles_is_unserviceable_with_the_reached_sentence() -> None:
    due = compute([pack("BAT-A", 300)])
    cycles = item(due, "BAT-A", "cycles")
    assert (cycles.used, cycles.limit, cycles.remaining, cycles.state) == (300, 300, 0, "overdue")
    assert cycles.message == (
        "battery pack BAT-A on aircraft AC-1 has reached its 300-cycle life limit"
    )
    assert board(due) == Board("unserviceable", (cycles.message,))


def test_one_cycle_short_of_the_limit_is_serviceable_and_one_past_keeps_its_sentence() -> None:
    short = compute([pack("BAT-A", 299)])
    cycles = item(short, "BAT-A", "cycles")
    assert (cycles.remaining, cycles.state) == (1, "due_soon")
    assert cycles.message == (
        "battery pack BAT-A on aircraft AC-1 has 1 cycle left of its 300-cycle life limit"
    )
    assert board(short) == Board("serviceable", ())
    past = compute([pack("BAT-A", 301)])
    cycles = item(past, "BAT-A", "cycles")
    assert (cycles.remaining, cycles.state) == (-1, "overdue")
    assert cycles.message == (
        "battery pack BAT-A on aircraft AC-1 is 1 cycle past its 300-cycle life limit"
    )
    assert board(past) == Board("unserviceable", (cycles.message,))


def test_a_propeller_set_at_exactly_its_hours_limit_is_unserviceable() -> None:
    due = compute([prop("PROP-A", 300.0)])
    hours = item(due, "PROP-A", "hours")
    assert (hours.used, hours.remaining, hours.state) == (300.0, 0.0, "overdue")
    assert hours.message == "propeller set PROP-A on aircraft AC-1 has reached its 300 h life limit"
    assert board(due) == Board("unserviceable", (hours.message,))
    short = compute([prop("PROP-A", 299.9)])
    hours = item(short, "PROP-A", "hours")
    assert hours.state == "due_soon"
    assert hours.message == (
        "propeller set PROP-A on aircraft AC-1 has 0.1 h left of its 300 h life limit"
    )
    assert board(short) == Board("serviceable", ())
    past = compute([prop("PROP-A", 300.3)])
    assert item(past, "PROP-A", "hours").message == (
        "propeller set PROP-A on aircraft AC-1 is 0.3 h past its 300 h life limit"
    )


def test_the_last_day_of_a_calendar_life_is_valid_and_the_next_day_is_past() -> None:
    """In service 2024-03-15, 24 calendar months: the life runs to 2026-03-31, that day
    included. On that day part of the last unit is left; it completes at midnight."""
    part = pack("BAT-C", 0, since=date(2024, 3, 15))
    last_day = compute([part], as_of=datetime(2026, 3, 31, 23, 0, tzinfo=UTC))
    cal = item(last_day, "BAT-C", "calendar")
    assert (cal.remaining, cal.state) == (0, "due_soon")
    assert cal.message == (
        "battery pack BAT-C on aircraft AC-1 has 0 days left of its 24-calendar-month life "
        "limit, due by 2026-03-31"
    )
    assert board(last_day) == Board("serviceable", ())
    next_day = compute([part], as_of=datetime(2026, 4, 1, 0, 0, tzinfo=UTC))
    cal = item(next_day, "BAT-C", "calendar")
    assert (cal.remaining, cal.state) == (-1, "overdue")
    assert cal.message == (
        "battery pack BAT-C on aircraft AC-1 passed its 24-calendar-month life limit on 2026-03-31"
    )
    assert board(next_day) == Board("unserviceable", (cal.message,))


def test_a_reached_limit_grounds_the_aircraft_before_its_first_log_too() -> None:
    """Before the first log the time in service is not known, unless a work order or a life
    limit grounds the aircraft, which wins: reached grounds as past does."""
    record = MaintenanceRecord(KEY, 50.0 * H, (), (), True)
    due = compute([pack("BAT-A", 300)], [flight(1, TAKEOFF)], as_of=TAKEOFF - D, maintenance=record)
    state = board(due)
    assert state.status == "unserviceable"
    assert state.reasons[0] == (
        "battery pack BAT-A on aircraft AC-1 has reached its 300-cycle life limit"
    )


# ---- landed: a flight counts once its log has ended -------------------------------------


def test_a_cycle_is_not_counted_before_landing_and_the_board_turns_when_the_log_ends() -> None:
    """The 300th cycle of a pack: serviceable with one cycle left at take-off, through the
    flight and to the last second of the log; unserviceable, reached, from the log's end."""
    parts = [pack("BAT-A", 299)]
    logs = [flight(1, TAKEOFF)]
    for at in (TAKEOFF - S, TAKEOFF, TAKEOFF + 60 * S, TAKEOFF + FLIGHT_S * S, LOG_END - S):
        due = compute(parts, logs, as_of=at)
        assert item(due, "BAT-A", "cycles").used == 299, at
        assert due.time_in_service_s == 0.0, at
        assert board(due) == Board("serviceable", ()), at
    landed = compute(parts, logs, as_of=LOG_END)
    assert item(landed, "BAT-A", "cycles").used == 300
    assert landed.time_in_service_s == FLIGHT_S
    assert landed.usage["BAT-A"].hours_s == FLIGHT_S
    assert board(landed) == Board(
        "unserviceable",
        ("battery pack BAT-A on aircraft AC-1 has reached its 300-cycle life limit",),
    )


def test_hours_are_not_counted_before_landing_either() -> None:
    parts = [prop("PROP-A", 299.5)]  # the flight's 0.5 h is the whole of what is left
    logs = [flight(1, TAKEOFF)]
    airborne = compute(parts, logs, as_of=TAKEOFF + 900 * S)
    assert item(airborne, "PROP-A", "hours").used == 299.5
    assert board(airborne) == Board("serviceable", ())
    landed = compute(parts, logs, as_of=LOG_END)
    assert item(landed, "PROP-A", "hours").used == 300.0
    assert board(landed) == Board(
        "unserviceable", ("propeller set PROP-A on aircraft AC-1 has reached its 300 h life limit",)
    )


def test_the_public_usage_functions_count_a_log_from_its_end() -> None:
    """What the ledger's write rules read (ledger.validate) follows the same instant."""
    logs = [flight(1, TAKEOFF)]
    part = pack("BAT-A", 299)
    assert time_in_service(KEY, 0.0, logs, until=LOG_END - S) == 0.0
    assert time_in_service(KEY, 0.0, logs, until=LOG_END) == FLIGHT_S
    assert component_usage(part, lambda _key: logs, until=LOG_END - S).cycles == 299
    assert component_usage(part, lambda _key: logs, until=LOG_END).cycles == 300
    assert time_in_service(KEY, 0.0, logs) == FLIGHT_S  # no date asked: every log counts


def test_a_log_whose_flight_time_is_not_known_still_counts_one_cycle_from_its_end() -> None:
    logs = [flight(1, TAKEOFF, Unknown("vehicle_land_detected has no landed field"))]
    parts = [pack("BAT-A", 299)]
    assert item(compute(parts, logs, as_of=LOG_END - S), "BAT-A", "cycles").used == 299
    landed = compute(parts, logs, as_of=LOG_END)
    assert item(landed, "BAT-A", "cycles").used == 300
    assert landed.time_in_service_s == 0.0  # the log cannot say how long it flew


def test_a_log_with_no_utc_start_has_no_end_and_counts_whenever_for_the_airframe_only() -> None:
    logs = [flight(1, Unknown("no GPS fix"))]
    due = compute([pack("BAT-A", 299)], logs, as_of=FITTED)
    assert due.time_in_service_s == FLIGHT_S
    assert item(due, "BAT-A", "cycles").used == 299
    assert (
        "flight AC-1-01 of aircraft AC-1 has no UTC start and is not counted against any component"
    ) in due.notes


def test_unlogged_flight_counts_from_the_end_of_the_log_it_followed() -> None:
    """400 s were flown after log 1 stopped, which only log 2's counter shows. When that
    flight ended the records do not hold; it is counted with the log it followed, the
    earliest it can have been flown, so a limit is never reported later than it was reached."""
    logs = [
        flight(1, TAKEOFF, 1000.0, at_boot=0.0),
        flight(2, TAKEOFF + D, 1000.0, at_boot=1400.0),
    ]
    parts = [prop("PROP-A", 0.0)]
    assert compute(parts, logs, as_of=LOG_END - S).time_in_service_s == 0.0
    after_first = compute(parts, logs, as_of=LOG_END)
    assert after_first.time_in_service_s == 1400.0
    assert after_first.usage["PROP-A"].hours_s == 1400.0
    assert compute(parts, logs, as_of=TAKEOFF + D + 60 * S).time_in_service_s == 1400.0
    assert compute(parts, logs, as_of=LOG_END + D).time_in_service_s == 2400.0


def test_the_board_row_lists_a_log_once_it_has_ended() -> None:
    """The service's board row counts the logs that had ended by `as_of`: a log is a file
    that exists once it is closed, and the row never counts a flight the due list does not."""
    fleet = generate(CONFIG)
    store = Store(":memory:")
    store.add_fleet(fleet)
    first = fleet.flights["SYN-01"][0]
    assert isinstance(first.utc_start, datetime)
    end = first.utc_start + timedelta(seconds=first.log_span_s)
    aircraft = store.get_aircraft("SYN-01")
    assert aircraft is not None
    assert aircraft_view(store, aircraft, CONFIG, first.utc_start + 60 * S).flights == 0
    assert aircraft_view(store, aircraft, CONFIG, end - S).flights == 0
    assert aircraft_view(store, aircraft, CONFIG, end).flights == 1


# ---- the cue of a reached limit -----------------------------------------------------------


def test_the_cue_of_a_reached_limit_says_reached_and_passes_the_checker() -> None:
    due = compute([pack("BAT-04A", 300), prop("PROP-01", 300.0)])
    cycles, hours = item(due, "BAT-04A", "cycles"), item(due, "PROP-01", "hours")
    assert item_cue(cycles, NOW) == "BAT-04A reached 300-cycle limit"
    assert item_cue(hours, NOW) == "PROP-01 reached 300 h limit"
    for i in (cycles, hours):
        cue = item_cue(i, NOW)
        assert len(cue) <= CUE_LIMIT
        assert problems(cue, i, NOW) == []
    state = board(due)
    assert state.status == "unserviceable"
    assert reason_cues(due, state) == (
        "BAT-04A reached 300-cycle limit",
        "PROP-01 reached 300 h limit",
    )


def test_a_long_part_id_keeps_the_reached_cue_within_the_limit() -> None:
    long_id = "BATTERY-PACK-0004A"  # 18 characters
    due = compute([pack(long_id, 300)])
    cue = item_cue(item(due, long_id, "cycles"), NOW)
    assert cue == "BATTERY-PACK-0004A reached 300 cycles"
    assert len(cue) <= CUE_LIMIT
    assert problems(cue, item(due, long_id, "cycles"), NOW) == []


def test_the_checker_refuses_reached_for_another_state_and_another_word_for_reached() -> None:
    reached = item(compute([pack("BAT-A", 300)]), "BAT-A", "cycles")
    past = item(compute([pack("BAT-A", 301)]), "BAT-A", "cycles")
    left = item(compute([pack("BAT-A", 299)]), "BAT-A", "cycles")
    # Weaker than the sentence: a part one cycle past is not merely at its limit.
    assert any("reached" in p for p in problems("BAT-A reached 300-cycle limit", past, NOW))
    # A part with a cycle left has not reached anything.
    assert any("state" in p for p in problems("BAT-A reached 300-cycle limit", left, NOW))
    # A reached limit is not "left" and not "past".
    assert any("state" in p for p in problems("BAT-A 0 cycles left of 300", reached, NOW))
    assert any("reached" in p for p in problems("BAT-A past 300-cycle limit", reached, NOW))


# ---- the fit refusal and the board agree at the limit, on every basis ----------------------


def ledger_store(logs: Sequence[FlightRecord] = ()) -> Store:
    """One synthetic aircraft with its logs and no entries; the test writes the ledger."""
    store = Store(":memory:")
    aircraft = Aircraft(KEY, "Synthetic test aircraft", "px4", True, "CC0", "synthetic")
    store.add_fleet(Fleet((aircraft,), {KEY: tuple(logs)}))
    return store


def entry(subject: str, kind: str, details: dict[str, Any] | None = None, *, at: datetime) -> Entry:
    return Entry(
        id=None,
        subject=subject,
        kind=kind,
        occurred_utc=at,
        recorded_utc=NOW,
        entered_by=BY,
        statement="[synthetic] as described",
        details=details or {},
        supersedes=None,
        reason=None,
        synthetic=True,
    )


def write(store: Store, e: Entry) -> Entry:
    return append(store, e, policy=POLICY, now=NOW, tolerance_s=CONFIG.tolerance_s)


def refused(store: Store, e: Entry) -> str:
    before = store.entry_count()
    with pytest.raises(LedgerError) as exc:
        write(store, e)
    assert exc.value.status == 409, exc.value.detail
    assert store.entry_count() == before
    return exc.value.detail


def register(store: Store, id_: str, kind: str, at: datetime, **usage: Any) -> None:
    details = {
        "kind": kind,
        "in_service_since": "2025-06-01",
        "hours_s_before": 0.0,
        "cycles_before": 0,
        **usage,
    }
    write(store, entry(id_, "component.register", details, at=at))


def fit(id_: str, at: datetime) -> Entry:
    return entry(id_, "component.install", {"aircraft_key": KEY}, at=at)


def state_at(store: Store, at: datetime) -> Board:
    p = store.projection(at)
    return board(
        due_list(
            KEY,
            maintenance=p.maintenance.get(KEY),
            components=p.components,
            flights_of=store.flights,
            policy=POLICY,
            as_of=at,
            tolerance_s=CONFIG.tolerance_s,
        )
    )


def test_board_and_fit_refusal_agree_at_the_cycle_limit() -> None:
    """A pack flies its 300th cycle on the aircraft. Until the log ends the board is
    serviceable and a second pack with 299 cycles would be fitted; from the log's end the
    board is unserviceable, and a pack with 300 cycles is refused at that same instant."""
    store = ledger_store([flight(1, TAKEOFF)])
    register(store, "PROP-OK", "propeller set", FITTED - D)  # keeps a record on the aircraft
    write(store, fit("PROP-OK", FITTED))
    register(store, "BAT-ON", "battery pack", FITTED - D, cycles_before=299)
    write(store, fit("BAT-ON", FITTED))
    register(store, "BAT-NEW", "battery pack", FITTED - D, cycles_before=300)
    assert state_at(store, LOG_END - S) == Board("serviceable", ())
    assert state_at(store, LOG_END) == Board(
        "unserviceable",
        ("battery pack BAT-ON on aircraft AC-1 has reached its 300-cycle life limit",),
    )
    assert refused(store, fit("BAT-NEW", LOG_END)) == (
        "BAT-NEW cannot be fitted to AC-1 at 2026-03-10 10:31 UTC: it has flown 300 cycles, the "
        f"whole of its 300-cycle life limit; {TAIL}"
    )
    # The pack that reached its limit on the aircraft comes off and goes on nothing again.
    write(store, entry("BAT-ON", "component.remove", {"aircraft_key": KEY}, at=LOG_END + D))
    assert state_at(store, LOG_END + D) == Board("serviceable", ())
    assert refused(store, fit("BAT-ON", LOG_END + 2 * D)) == (
        "BAT-ON cannot be fitted to AC-1 at 2026-03-12 10:31 UTC: it has flown 300 cycles, the "
        f"whole of its 300-cycle life limit; {TAIL}"
    )


def test_a_part_removed_before_its_flight_has_ended_is_judged_on_what_had_landed() -> None:
    """The write rules count as the board does: one second before the log ends the pack has
    299 cycles on record and may be fitted again; from the log's end it has 300 and may not."""
    store = ledger_store([flight(1, TAKEOFF)])
    register(store, "BAT-ON", "battery pack", FITTED - D, cycles_before=299)
    write(store, fit("BAT-ON", FITTED))
    write(store, entry("BAT-ON", "component.remove", {"aircraft_key": KEY}, at=LOG_END - 2 * S))
    again = write(store, fit("BAT-ON", LOG_END - S))
    assert again.id is not None
    assert state_at(store, LOG_END - S) == Board("serviceable", ())


def test_board_and_fit_refusal_agree_at_the_hours_limit() -> None:
    store = ledger_store([flight(1, TAKEOFF)])
    register(store, "PROP-ON", "propeller set", FITTED - D, hours_s_before=299.5 * H)
    write(store, fit("PROP-ON", FITTED))
    register(store, "PROP-NEW", "propeller set", FITTED - D, hours_s_before=300.0 * H)
    assert state_at(store, LOG_END - S) == Board("serviceable", ())
    assert state_at(store, LOG_END) == Board(
        "unserviceable",
        ("propeller set PROP-ON on aircraft AC-1 has reached its 300 h life limit",),
    )
    assert refused(store, fit("PROP-NEW", LOG_END)) == (
        "PROP-NEW cannot be fitted to AC-1 at 2026-03-10 10:31 UTC: it has flown 300.0 h, the "
        f"whole of its 300 h life limit; {TAIL}"
    )


def test_board_and_fit_agree_on_the_last_day_of_a_calendar_life_and_on_the_next() -> None:
    """In service 2024-06-30, 24 calendar months: the life runs to 2026-06-30. On that day a
    fitted pack is valid and another like it may be fitted; from 2026-07-01 the fitted pack
    grounds the aircraft and the other is refused."""
    store = ledger_store()
    registered = datetime(2026, 6, 1, tzinfo=UTC)
    for id_ in ("BAT-ON", "BAT-NEW", "BAT-LATE"):
        register(store, id_, "battery pack", registered, in_service_since="2024-06-30")
    write(store, fit("BAT-ON", registered + D))
    last_day = datetime(2026, 6, 30, 23, 0, tzinfo=UTC)
    assert state_at(store, last_day) == Board("serviceable", ())
    fitted = write(store, fit("BAT-NEW", last_day))
    assert fitted.id is not None
    next_day = datetime(2026, 7, 1, 0, 0, tzinfo=UTC)
    assert state_at(store, next_day) == Board(
        "unserviceable",
        (
            "battery pack BAT-NEW on aircraft AC-1 passed its 24-calendar-month life limit on "
            "2026-06-30",
            "battery pack BAT-ON on aircraft AC-1 passed its 24-calendar-month life limit on "
            "2026-06-30",
        ),
    )
    assert refused(store, fit("BAT-LATE", next_day)) == (
        "BAT-LATE cannot be fitted to AC-1 at 2026-07-01 00:00 UTC: its 24-calendar-month life "
        f"limit ended on 2026-06-30; {TAIL}"
    )


def test_under_every_limit_the_part_is_fitted_and_the_board_is_serviceable() -> None:
    store = ledger_store([flight(1, TAKEOFF)])
    register(store, "BAT-OK", "battery pack", FITTED - D, cycles_before=298)
    register(store, "PROP-OK", "propeller set", FITTED - D, hours_s_before=299.0 * H)
    write(store, fit("BAT-OK", FITTED))
    write(store, fit("PROP-OK", FITTED))
    assert state_at(store, LOG_END) == Board("serviceable", ())
    replaced = dataclasses.replace(fit("BAT-OK", LOG_END + D), kind="component.remove")
    write(store, replaced)
    assert write(store, fit("BAT-OK", LOG_END + 2 * D)).id is not None  # 299 of 300: fitted
