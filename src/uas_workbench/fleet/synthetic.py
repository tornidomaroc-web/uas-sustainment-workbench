"""Seeded synthetic fleet: reproducible aircraft and flights with known reconciliation cases.

Nothing here describes a real aircraft. The generator fixes which aircraft carries which
case (an unlogged flight, a duplicate upload, unlogged boots, fields a log cannot give) so
the demo always shows them; the seed varies everything else. Counter behaviour follows
what was measured on real logs (LIMITS.md): ArduPilot flushes its counter every 30 s and
logs a boot count, PX4 saves it at disarm and has no boot count.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, time, timedelta

from uas_workbench.flight.record import FaultEvent, FlightRecord, LifetimeCounter, Source, Unknown

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
KINDS = ("fixed-wing", "VTOL", "fixed-wing", "quadrotor", "fixed-wing", "VTOL")


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
        status: str | Unknown = rng.choice(config.states)
        if case == "unknown_fields":
            status = Unknown("no maintenance record entered for this aircraft")
        aircraft.append(
            Aircraft(
                key=key,
                label=f"Synthetic {KINDS[(index - 1) % len(KINDS)]} {index:02d}",
                source=source,
                synthetic=True,
                licence="CC0",
                attribution=attribution,
                status=status,
            )
        )
        flights[key] = tuple(_flights(rng, config, key, source, case, attribution))
    return Fleet(tuple(aircraft), flights)


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
            counter_s += 420.0  # the aircraft flew on after logging stopped
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
