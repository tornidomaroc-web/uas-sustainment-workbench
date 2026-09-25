"""The seeded fleet's board states are computed from its maintenance records, not seeded.

Every synthetic aircraft carries one case; the generator fixes which. The expected states
below are stable in time: the overdue and due-soon items are by hours and cycles, and the
one calendar item passed its date in 2026, so the demo does not change its story as the
clock runs.
"""

from __future__ import annotations

from datetime import UTC, datetime

from uas_workbench.fleet import Fleet, load_config
from uas_workbench.fleet.synthetic import generate
from uas_workbench.flight import Unknown, is_known
from uas_workbench.life import Board, DueList, board, due_list

CONFIG = load_config()
AS_OF = datetime(2026, 10, 1, tzinfo=UTC)
EXPECTED = {
    "SYN-01": "serviceable",
    "SYN-02": "serviceable",  # a propeller set close to its hours limit: due soon, still fit
    "SYN-03": "serviceable with deferred defects",  # 100-hour inspection overflown, in tolerance
    "SYN-04": "unserviceable",  # a battery pack past its cycles, another past its calendar life
    "SYN-05": "in maintenance",  # work order opened after the fault events
    "SYN-06": Unknown("no maintenance record entered for this aircraft"),
    "SYN-07": "AOG",  # awaiting parts
}


def compute(fleet: Fleet, key: str, as_of: datetime = AS_OF) -> tuple[DueList, Board]:
    due = due_list(
        key,
        maintenance=fleet.maintenance.get(key),
        components=fleet.components,
        flights_of=lambda k: fleet.flights.get(k, ()),
        policy=CONFIG.life,
        as_of=as_of,
        tolerance_s=CONFIG.tolerance_s,
    )
    return due, board(due)


def test_every_civil_state_appears_once_and_is_computed() -> None:
    fleet = generate(CONFIG)
    assert [a.key for a in fleet.aircraft] == list(EXPECTED)
    states = {key: compute(fleet, key)[1].status for key in EXPECTED}
    assert states == EXPECTED
    assert set(CONFIG.states) <= {s for s in states.values() if is_known(s)}
    for key, status in states.items():
        _, result = compute(fleet, key)
        if status in ("serviceable", "AOG") or isinstance(status, Unknown):
            continue
        assert result.reasons, key
        for reason in result.reasons:
            assert reason.endswith(tuple("abcdefghijklmnopqrstuvwxyz0123456789)")), reason
            assert key in reason


def test_the_moved_battery_keeps_its_cycles_from_the_first_airframe() -> None:
    fleet = generate(CONFIG)
    moved = next(c for c in fleet.components if len(c.installations) == 2)
    first, second = moved.installations
    assert (first.aircraft_key, second.aircraft_key) == ("SYN-01", "SYN-04")
    assert first.to_utc == second.from_utc and second.to_utc is None
    due, result = compute(fleet, "SYN-04")
    counted = 0
    for inst in moved.installations:
        for r in fleet.flights[inst.aircraft_key]:
            if not is_known(r.utc_start) or r.utc_start < inst.from_utc:
                continue
            if inst.to_utc is None or r.utc_start < inst.to_utc:
                counted += 1
    cycles = next(i for i in due.items if i.component_id == moved.id and i.basis == "cycles")
    assert cycles.used == moved.cycles_before + counted
    assert cycles.state == "overdue"
    assert cycles.message in result.reasons
    assert result.status == "unserviceable"


def test_unlogged_flight_from_reconcile_wears_the_parts() -> None:
    fleet = generate(CONFIG)
    due, _ = compute(fleet, "SYN-02")
    logged = sum(r.flight_time_s for r in fleet.flights["SYN-02"] if is_known(r.flight_time_s))
    before = fleet.maintenance["SYN-02"].time_in_service_before_s
    # The counter is flushed every 30 s, so reconcile() sees the 420 s less than one flush.
    assert is_known(due.time_in_service_s)
    credited = due.time_in_service_s - before - logged
    assert 420.0 - CONFIG.tolerance_s < credited <= 420.0
    prop = next(i for i in due.items if i.basis == "hours" and i.component_id)
    assert prop.state == "due_soon"
    assert 0 < prop.remaining <= CONFIG.life.due_soon_fraction * prop.limit


def test_the_duplicate_upload_is_not_counted_twice() -> None:
    fleet = generate(CONFIG)
    due, _ = compute(fleet, "SYN-03")
    refs = [r.log_ref for r in fleet.flights["SYN-03"]]
    duplicate = next(r for r in refs if "upload2" in r)
    assert any(duplicate in n and "duplicate" in n for n in due.notes)
    unique = {(r.utc_start, r.log_span_s): r for r in fleet.flights["SYN-03"]}.values()
    logged = sum(r.flight_time_s for r in unique if is_known(r.flight_time_s))
    assert due.time_in_service_s == fleet.maintenance["SYN-03"].time_in_service_before_s + logged


def test_every_synthetic_record_is_labelled() -> None:
    fleet = generate(CONFIG)
    assert fleet.components and fleet.maintenance
    for c in fleet.components:
        assert c.synthetic and c.kind in CONFIG.life.component_kinds
    for m in fleet.maintenance.values():
        assert m.synthetic
        for w in m.work_orders:
            assert w.synthetic and w.description.startswith("[synthetic]")
        for i in m.inspections:
            assert i.name in CONFIG.life.inspections
    assert "SYN-06" not in fleet.maintenance
