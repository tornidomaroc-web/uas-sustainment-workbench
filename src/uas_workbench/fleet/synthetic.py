"""Seeded synthetic fleet: reproducible aircraft and flights with known reconciliation cases.

Nothing here describes a real aircraft. The generator fixes which aircraft carries which
case (an unlogged flight, a duplicate upload, unlogged boots, fields a log cannot give) so
the demo always shows them; the seed varies everything else. Counter behaviour follows
what was measured on real logs (LIMITS.md): ArduPilot flushes its counter every 30 s and
logs a boot count, PX4 saves it at disarm and has no boot count.

The maintenance records (time in service, inspections done, components fitted, open work)
are generated too, from a second stream of the same seed, and fixed so that every civil
board state appears once when the life engine computes it. No board state is written
anywhere: the engine derives it from these records and the flights.
"""

from __future__ import annotations

import dataclasses
import random
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from uas_workbench.flight.record import (
    FaultEvent,
    FlightRecord,
    LifetimeCounter,
    Source,
    Unknown,
    is_known,
)
from uas_workbench.ledger.model import Entry
from uas_workbench.life import Component, InspectionDone, Installation, MaintenanceRecord, WorkOrder

from .config import FleetConfig
from .model import Aircraft, Fleet

FLUSH_S = 30.0
NO_ENERGY = Unknown("no EnrgTot field in BAT/CURR messages")
NO_BATTERY = Unknown("battery monitor disabled (BATT_MONITOR=0)")
NO_FIX = Unknown("no GPS fix with a valid week in the log")
NO_BOOT = Unknown("PX4 logs no boot counter")
NO_ARM_EVENTS = Unknown("ARMING_REQUIRE=0: the aircraft is always armed and logs no arm events")

# Aircraft index -> the reconciliation case it demonstrates. Extra aircraft are clean.
CASES: dict[int, str] = {
    2: "unlogged_flight",  # flight continued 420 s after its log stopped
    3: "duplicate_upload",  # the same log uploaded twice
    4: "unlogged_boots",  # two power cycles with no log at all, 900 s of flight
    5: "fault_events",  # error messages in one flight
    6: "unknown_fields",  # no battery monitor, one flight without a GPS fix
}
# Aircraft index -> the maintenance case it demonstrates; the board state follows from it.
LIFE_CASES: dict[int, str] = {
    2: "propeller_due_soon",  # the propeller set is within 10 % of its hours limit
    3: "inspection_in_tolerance",  # the 100-hour inspection overflown by less than 10 h
    4: "battery_expired",  # one pack past its cycles (moved from SYN-01), one past its date
    5: "work_open",  # a work order opened after the fault events: in maintenance
    6: "no_record",  # nothing entered: status unknown, with that reason
    7: "awaiting_parts",  # AOG
}
KINDS = ("fixed-wing", "VTOL", "fixed-wing", "quadrotor", "fixed-wing", "VTOL")
UNLOGGED_S = 420.0
H = 3600.0


def _source(index: int) -> Source:
    return "px4" if index % 2 else "ardupilot"


def generate(config: FleetConfig) -> Fleet:
    if config.aircraft < len(CASES) + 1:
        raise ValueError(f"the synthetic fleet needs at least {len(CASES) + 1} aircraft")
    rng = random.Random(config.seed)
    attribution = f"synthetic fleet generator, seed {config.seed}"
    aircraft: list[Aircraft] = []
    flights: dict[str, tuple[FlightRecord, ...]] = {}
    for index in range(1, config.aircraft + 1):
        key = f"SYN-{index:02d}"
        source = _source(index)
        case = CASES.get(index, "clean")
        aircraft.append(
            Aircraft(
                key=key,
                label=f"Synthetic {KINDS[(index - 1) % len(KINDS)]} {index:02d}",
                source=source,
                synthetic=True,
                licence="CC0",
                attribution=attribution,
            )
        )
        flights[key] = tuple(_flights(rng, config, key, source, case, attribution))
    # A second stream of the same seed: the maintenance records never alter the flights.
    parts = random.Random(config.seed ^ 0x5F1E)
    entries: list[Entry] = []
    for index in range(1, config.aircraft + 1):
        key = f"SYN-{index:02d}"
        life_case = LIFE_CASES.get(index, "clean")
        if life_case == "no_record":
            continue
        record, fitted = _maintenance(parts, config, key, index, life_case, flights)
        entries.extend(_as_entries(record, fitted, attribution))
    return Fleet(tuple(aircraft), flights, tuple(entries))


def _flights(
    rng: random.Random, config: FleetConfig, key: str, source: Source, case: str, attribution: str
) -> list[FlightRecord]:
    count = rng.randint(3, 6)
    clock = datetime.combine(
        config.first_day + timedelta(days=rng.randint(0, 10)), time(rng.randint(7, 14)), tzinfo=UTC
    )
    counter_s = float(rng.randint(5, 200) * 3600 + rng.randint(0, 3599))
    boot = rng.randint(100, 400)
    records: list[FlightRecord] = []
    for n in range(1, count + 1):
        flight_s = round(rng.uniform(600, 2400), 1)
        span_s = round(flight_s + rng.uniform(60, 240), 1)
        start = clock
        record = _record(rng, key, source, n, start, flight_s, span_s, counter_s, boot, attribution)
        if case == "unknown_fields":
            record = _without_battery(record, n == 2)
        if case == "fault_events" and n == 2:
            record = _with_faults(record)
        records.append(record)
        if case == "duplicate_upload" and n == 1:
            records.append(_duplicate(record))

        # What the autopilot will have counted by the next boot.
        if source == "ardupilot":
            counter_s += (flight_s // FLUSH_S) * FLUSH_S  # last flush before power-off
        else:
            counter_s += flight_s + 0.5  # saved at disarm, a moment after landing
        boot += 1
        if case == "unlogged_flight" and n == 2:
            counter_s += UNLOGGED_S  # the aircraft flew on after logging stopped
        if case == "unlogged_boots" and n == 2:
            counter_s += 900.0
            boot += 2
        # Next flight later the same day or on a later day, never before this one ends.
        clock = start + timedelta(seconds=span_s + rng.choice((1800, 7200, 86400, 172800)))
    return records


def _record(
    rng: random.Random,
    key: str,
    source: Source,
    n: int,
    start: datetime,
    flight_s: float,
    span_s: float,
    counter_s: float,
    boot: int,
    attribution: str,
) -> FlightRecord:
    log_ref = f"{key}-{start:%Y-%m-%d}-{n:02d}.synthetic"
    mah = round(flight_s * rng.uniform(6.0, 9.0), 1)
    if source == "ardupilot":
        lifetime = LifetimeCounter(
            counter_s,
            counter_s + (flight_s // FLUSH_S) * FLUSH_S,
            round(span_s - rng.uniform(1, 29), 1),
        )
        return FlightRecord(
            source="ardupilot",
            log_ref=log_ref,
            licence="CC0",
            attribution=attribution,
            firmware="ArduPlane 4.5.7",
            log_span_s=span_s,
            aircraft_key=key,
            utc_start=start,
            flight_time_s=flight_s,
            arm_cycles=1,
            landings=1 if rng.random() < 0.8 else 2,
            battery_mah=mah,
            battery_wh=NO_ENERGY,
            fault_events=(),
            boot_count=boot,
            lifetime=lifetime,
            synthetic=True,
        )
    return FlightRecord(
        source="px4",
        log_ref=log_ref,
        licence="CC0",
        attribution=attribution,
        firmware="v1.15.4",
        log_span_s=span_s,
        aircraft_key=key,
        utc_start=start,
        flight_time_s=flight_s,
        arm_cycles=1,
        landings=1,
        battery_mah=mah,
        battery_wh=round(mah * 22.2 / 1000, 2),
        fault_events=(),
        boot_count=NO_BOOT,
        lifetime=LifetimeCounter(counter_s, counter_s, 0.0),
        synthetic=True,
    )


def _without_battery(record: FlightRecord, drop_fix: bool) -> FlightRecord:
    changes: dict[str, object] = {"battery_mah": NO_BATTERY, "battery_wh": NO_BATTERY}
    if drop_fix:
        changes["utc_start"] = NO_FIX
    return _replace(record, changes)


def _with_faults(record: FlightRecord) -> FlightRecord:
    faults = (
        FaultEvent(412.0, "error_message", "[synthetic] Battery low"),
        FaultEvent(413.5, "error_message", "[synthetic] Landing initiated"),
    )
    return _replace(record, {"fault_events": faults})


def _duplicate(record: FlightRecord) -> FlightRecord:
    return _replace(record, {"log_ref": record.log_ref.replace(".synthetic", "-upload2.synthetic")})


def _replace(record: FlightRecord, changes: dict[str, object]) -> FlightRecord:
    import dataclasses

    return dataclasses.replace(record, **changes)  # type: ignore[arg-type]


# ---- maintenance records ---------------------------------------------------------------


def _first_start(records: tuple[FlightRecord, ...]) -> datetime:
    return min(r.utc_start for r in records if is_known(r.utc_start))


def _logged_s(records: tuple[FlightRecord, ...]) -> float:
    """Flight time of the distinct logs (a duplicate upload counts once)."""
    seen: set[tuple[object, float]] = set()
    total = 0.0
    for r in records:
        mark = (r.utc_start, r.log_span_s)
        if mark in seen:
            continue
        seen.add(mark)
        if is_known(r.flight_time_s):
            total += r.flight_time_s
    return total


def _pack(
    rng: random.Random, key: str, id_: str, cycles_before: int, in_service: date, fitted: datetime
) -> Component:
    return Component(
        id=id_,
        kind="battery pack",
        in_service_since=in_service,
        hours_s_before=cycles_before * rng.uniform(900, 1500),
        cycles_before=cycles_before,
        installations=(Installation(key, fitted, None),),
        synthetic=True,
    )


def _maintenance(
    rng: random.Random,
    config: FleetConfig,
    key: str,
    index: int,
    case: str,
    flights: dict[str, tuple[FlightRecord, ...]],
) -> tuple[MaintenanceRecord, list[Component]]:
    records = flights[key]
    first = _first_start(records)
    fitted = first - timedelta(days=1)
    day0 = datetime.combine(config.first_day, time(0), tzinfo=UTC)
    logged_s = _logged_s(records)
    unlogged_s = UNLOGGED_S if CASES.get(index) == "unlogged_flight" else 0.0
    rules = config.life

    # Time in service before the first log, and the inspections done before it.
    before_s = rng.uniform(20, 80) * H
    hundred_at_s = max(0.0, before_s - rng.uniform(5, 40) * H)
    if case == "inspection_in_tolerance":
        tolerance_h = rules.inspections["100-hour inspection"].tolerance_hours
        before_s = 110.0 * H
        overflown_h = rng.uniform(2.0, tolerance_h - 3.0)
        hundred_at_s = before_s + logged_s - (100.0 + overflown_h) * H
    inspections = (
        InspectionDone(
            "100-hour inspection", day0 - timedelta(days=rng.randint(10, 60)), hundred_at_s
        ),
        InspectionDone("annual inspection", day0 - timedelta(days=rng.randint(30, 200)), 0.0),
    )

    # Two battery packs and one propeller set, fitted the day before the first log.
    packs = [
        _pack(rng, key, f"BAT-{index:02d}{s}", rng.randint(20, 150),
              (day0 - timedelta(days=rng.randint(100, 400))).date(), fitted)
        for s in ("A", "B")
    ]  # fmt: skip
    prop_before_s = rng.uniform(50, 200) * H
    if case == "propeller_due_soon":
        limit_h = rules.component_kinds["propeller set"].hours or 300.0
        left_h = rng.uniform(2.0, rules.due_soon_fraction * limit_h - 5.0)
        prop_before_s = limit_h * H - logged_s - unlogged_s - left_h * H
    prop = Component(
        id=f"PROP-{index:02d}",
        kind="propeller set",
        in_service_since=(day0 - timedelta(days=rng.randint(30, 300))).date(),
        hours_s_before=prop_before_s,
        cycles_before=0,
        installations=(Installation(key, fitted, None),),
        synthetic=True,
    )
    if case == "battery_expired":
        cycles_limit = rules.component_kinds["battery pack"].cycles or 300
        # Pack A came off SYN-01 an hour before this aircraft flew; its cycles came with it
        # (14 CFR 43.10) and cross the limit on this airframe.
        moved_at = first - timedelta(hours=1)
        packs[0] = dataclasses.replace(
            packs[0],
            cycles_before=cycles_limit - 2,
            installations=(
                Installation(
                    "SYN-01", _first_start(flights["SYN-01"]) - timedelta(days=1), moved_at
                ),
                Installation(key, moved_at, None),
            ),
        )
        # Pack B is past its calendar life: in service since spring 2024, limit 24 months.
        packs[1] = dataclasses.replace(packs[1], in_service_since=date(2024, 3, 15))

    work: tuple[WorkOrder, ...] = ()
    if case == "work_open":
        second = sorted(r.utc_start for r in records if is_known(r.utc_start))[1]
        work = (
            WorkOrder(
                second + timedelta(days=1),
                "[synthetic] low-battery message in flight 2: inspect the battery connector "
                "and the pack before the next flight",
                "in_work",
                True,
            ),
        )
    if case == "awaiting_parts":
        last = max(r.utc_start for r in records if is_known(r.utc_start))
        work = (
            WorkOrder(
                last + timedelta(days=1),
                "[synthetic] replacement propeller set on order after a ground strike",
                "awaiting_parts",
                True,
            ),
        )
    record = MaintenanceRecord(
        aircraft_key=key,
        time_in_service_before_s=before_s,
        inspections=inspections,
        work_orders=work,
        synthetic=True,
    )
    return record, [*packs, prop]


def _as_entries(record: MaintenanceRecord, parts: list[Component], by: str) -> list[Entry]:
    """The ledger entries that project back to exactly this record and these components.

    Every entry is synthetic, entered by the generator, and its statement starts with
    "[synthetic]". Removals sort before installs at the same instant, so a part that moved
    between airframes is recorded as taken off one and fitted to the other.
    """
    key = record.aircraft_key

    def entry(subject: str, kind_: str, at: datetime, statement: str, **details: Any) -> Entry:
        return Entry(
            id=None,
            subject=subject,
            kind=kind_,
            occurred_utc=at,
            recorded_utc=at,
            entered_by=by,
            statement=statement,
            details=details,
            supersedes=None,
            reason=None,
            synthetic=True,
        )

    out: list[tuple[datetime, int, Entry]] = []
    earliest = min(i.done_utc for i in record.inspections) - timedelta(days=1)
    out.append(
        (
            earliest,
            0,
            entry(
                key,
                "time_in_service.set",
                earliest,
                "[synthetic] time in service carried over from the previous logbook",
                before_s=record.time_in_service_before_s,
            ),
        )
    )
    for i in record.inspections:
        out.append(
            (
                i.done_utc,
                2,
                entry(
                    key,
                    "inspection.done",
                    i.done_utc,
                    f"[synthetic] {i.name} completed",
                    name=i.name,
                    at_hours_s=i.at_hours_s,
                    carried_over_s=i.carried_over_s,
                ),
            )
        )
    for n, w in enumerate(record.work_orders, start=1):
        statement = w.description
        if not statement.startswith("[synthetic]"):
            statement = f"[synthetic] {statement}"
        out.append(
            (
                w.opened_utc,
                2,
                entry(
                    key,
                    "work_order.open",
                    w.opened_utc,
                    statement,
                    work_id=f"WO-{key}-{n}",
                    state=w.state,
                ),
            )
        )
    for c in parts:
        registered = datetime.combine(c.in_service_since, time(0), tzinfo=UTC)
        out.append(
            (
                registered,
                0,
                entry(
                    c.id,
                    "component.register",
                    registered,
                    f"[synthetic] {c.kind} {c.id} entered into service",
                    kind=c.kind,
                    in_service_since=c.in_service_since.isoformat(),
                    hours_s_before=c.hours_s_before,
                    cycles_before=c.cycles_before,
                ),
            )
        )
        for inst in c.installations:
            out.append(
                (
                    inst.from_utc,
                    2,
                    entry(
                        c.id,
                        "component.install",
                        inst.from_utc,
                        f"[synthetic] {c.kind} {c.id} fitted to {inst.aircraft_key}",
                        aircraft_key=inst.aircraft_key,
                    ),
                )
            )
            if inst.to_utc is not None:
                out.append(
                    (
                        inst.to_utc,
                        1,
                        entry(
                            c.id,
                            "component.remove",
                            inst.to_utc,
                            f"[synthetic] {c.kind} {c.id} removed from {inst.aircraft_key}",
                            aircraft_key=inst.aircraft_key,
                        ),
                    )
                )
    out.sort(key=lambda t: (t[0], t[1]))
    return [e for _, _, e in out]
