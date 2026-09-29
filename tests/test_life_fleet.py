"""The seeded fleet's board states are computed from its maintenance records, not seeded.

Every synthetic aircraft carries one case; the generator fixes which. The expected states
below are stable in time from 2026-09-01 on: the overdue and due-soon items are by hours and
cycles, and the one calendar item passed its date on 2026-08-31, so the demo does not change
its story as the clock runs.

The story the seed tells about its batteries, pinned here to the minute: BAT-04A began on
SYN-01 two cycles short of its limit, crossed it on SYN-01's third flight on 2026-08-06, came
off on 2026-08-13 into storage and is never fitted again, since a part past a life limit
cannot be fitted (ledger.validate). SYN-01 lends BAT-01B to SYN-04 that morning; its cycles
from SYN-01 count on SYN-04 (14 CFR 43.10) and stay under the limit. SYN-04 is grounded by
BAT-04B alone, whose 24-calendar-month life ends on 2026-08-31 while it is fitted.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from uas_workbench.fleet import Fleet, load_config
from uas_workbench.fleet.showcase import showcase
from uas_workbench.fleet.synthetic import generate
from uas_workbench.flight import Unknown, is_known
from uas_workbench.ledger import Entry, LedgerError, append
from uas_workbench.life import Board, DueList, board, due_list
from uas_workbench.service.store import Store

FIXTURES = Path(__file__).parent / "fixtures"
CONFIG = load_config()
AS_OF = datetime(2026, 10, 1, tzinfo=UTC)
H = 3600.0
EXPECTED = {
    "SYN-01": "serviceable",
    "SYN-02": "serviceable",  # a propeller set close to its hours limit: due soon, still fit
    "SYN-03": "serviceable with deferred defects",  # 100-hour inspection overflown, in tolerance
    "SYN-04": "unserviceable",  # a battery pack whose calendar life ended while fitted
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


def seeded() -> Store:
    s = Store(":memory:")
    s.add_fleet(generate(CONFIG))
    s.add_fleet(showcase(FIXTURES))
    return s


def state(store: Store, key: str, at: datetime) -> Board:
    p = store.projection(at)
    return board(
        due_list(
            key,
            maintenance=p.maintenance.get(key),
            components=p.components,
            flights_of=store.flights,
            policy=CONFIG.life,
            as_of=at,
            tolerance_s=CONFIG.tolerance_s,
        )
    )


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


def test_the_lent_battery_keeps_its_cycles_from_the_first_airframe_and_stays_under_its_limit() -> (
    None
):
    fleet = generate(CONFIG)
    moved = [c for c in fleet.components if len(c.installations) == 2]
    assert [c.id for c in moved] == ["BAT-01B"]
    (lent,) = moved
    first, second = lent.installations
    assert (first.aircraft_key, second.aircraft_key) == ("SYN-01", "SYN-04")
    assert first.to_utc == second.from_utc == datetime(2026, 8, 13, 8, tzinfo=UTC)
    assert second.to_utc is None
    due, result = compute(fleet, "SYN-04")
    counted = 0
    for inst in lent.installations:
        for r in fleet.flights[inst.aircraft_key]:
            if not is_known(r.utc_start) or r.utc_start < inst.from_utc:
                continue
            if inst.to_utc is None or r.utc_start < inst.to_utc:
                counted += 1
    assert counted == 5 + 3  # five flights on SYN-01 before the handover, three on SYN-04 after
    cycles = next(i for i in due.items if i.component_id == lent.id and i.basis == "cycles")
    assert cycles.used == lent.cycles_before + counted
    assert cycles.state == "ok" and cycles.message not in result.reasons
    # On SYN-01 the lent pack is no longer installed, so it does not appear on its list.
    on_lender, _ = compute(fleet, "SYN-01")
    assert lent.id not in {i.component_id for i in on_lender.items}


def test_syn_01_crosses_the_cycle_limit_on_2026_08_06_between_11_07_and_11_08_utc() -> None:
    s = seeded()
    before = state(s, "SYN-01", datetime(2026, 8, 6, 11, 7, tzinfo=UTC))
    assert before.status == "serviceable" and before.reasons == ()
    after = state(s, "SYN-01", datetime(2026, 8, 6, 11, 8, tzinfo=UTC))
    assert after.status == "unserviceable"
    assert after.reasons == (
        "battery pack BAT-04A on aircraft SYN-01 is 1 cycle past its 300-cycle life limit",
    )
    # It stays grounded by that pack until the pack comes off on 2026-08-13 at 08:00 UTC.
    still = state(s, "SYN-01", datetime(2026, 8, 13, 7, 59, tzinfo=UTC))
    assert still.status == "unserviceable"
    assert still.reasons == (
        "battery pack BAT-04A on aircraft SYN-01 is 3 cycles past its 300-cycle life limit",
    )
    freed = state(s, "SYN-01", datetime(2026, 8, 13, 8, tzinfo=UTC))
    assert freed.status == "serviceable" and freed.reasons == ()
    assert state(s, "SYN-01", AS_OF).status == "serviceable"


def test_syn_04_is_grounded_on_2026_09_01_by_the_calendar_life_of_bat_04b_alone() -> None:
    s = seeded()
    # Before its first log on 2026-08-13 09:00 its time in service is not known; from the
    # first log to the end of August it is serviceable, its pack B due soon.
    assert isinstance(state(s, "SYN-04", datetime(2026, 8, 13, tzinfo=UTC)).status, Unknown)
    flying = state(s, "SYN-04", datetime(2026, 8, 14, tzinfo=UTC))
    assert flying.status == "serviceable" and flying.reasons == ()
    last_day = state(s, "SYN-04", datetime(2026, 8, 31, 23, 59, tzinfo=UTC))
    assert last_day.status == "serviceable" and last_day.reasons == ()
    grounded = state(s, "SYN-04", datetime(2026, 9, 1, tzinfo=UTC))
    assert grounded.status == "unserviceable"
    assert grounded.reasons == (
        "battery pack BAT-04B on aircraft SYN-04 passed its 24-calendar-month life limit on "
        "2026-08-31",
    )
    later = state(s, "SYN-04", AS_OF)
    assert later.status == "unserviceable" and later.reasons == grounded.reasons
    due, _ = compute(generate(CONFIG), "SYN-04")
    assert not any(i.basis == "cycles" and i.state == "overdue" for i in due.items)
    assert {i.component_id for i in due.items if i.component_id} == {
        "BAT-01B",
        "BAT-04B",
        "PROP-04",
    }


def test_bat_04a_is_in_storage_after_2026_08_13_and_cannot_be_fitted_again() -> None:
    fleet = generate(CONFIG)
    (pack,) = [c for c in fleet.components if c.id == "BAT-04A"]
    assert [(i.aircraft_key, i.to_utc) for i in pack.installations] == [
        ("SYN-01", datetime(2026, 8, 13, 8, tzinfo=UTC))
    ]
    assert pack.cycles_before == 298
    s = seeded()
    removal = next(
        e
        for e in s.entries("BAT-04A")
        if e.kind == "component.remove" and e.details["aircraft_key"] == "SYN-01"
    )
    assert removal.statement == (
        "[synthetic] battery pack BAT-04A removed from SYN-01 and placed in storage"
    )
    for key in ("SYN-01", "SYN-04"):
        before = s.entry_count()
        with pytest.raises(LedgerError) as exc:
            append(
                s,
                Entry(
                    id=None,
                    subject="BAT-04A",
                    kind="component.install",
                    occurred_utc=datetime(2026, 9, 1, 9, tzinfo=UTC),
                    recorded_utc=AS_OF,
                    entered_by="A. Tester, maintenance",
                    statement="refitted",
                    details={"aircraft_key": key},
                    supersedes=None,
                    reason=None,
                    synthetic=False,
                ),
                policy=CONFIG.life,
                now=AS_OF,
                tolerance_s=CONFIG.tolerance_s,
            )
        assert exc.value.status == 409
        assert exc.value.detail == (
            f"BAT-04A cannot be fitted to {key} at 2026-09-01 09:00 UTC: it has flown 303 "
            "cycles, past its 300-cycle life limit; a life-limited part that has reached its "
            "life limit is replaced, not fitted again"
        )
        assert s.entry_count() == before


def test_inspection_hours_before_the_first_log_describe_one_steady_rate() -> None:
    """The hours stated at every inspection done before an aircraft's first log fit one
    steady rate of flying, from the earliest inspection to the total reached by the first
    log: never above that total, never decreasing, and zero only where that rate runs out."""
    fleet = generate(CONFIG)
    assert set(fleet.maintenance) == {"SYN-01", "SYN-02", "SYN-03", "SYN-04", "SYN-05", "SYN-07"}
    for key, record in fleet.maintenance.items():
        first_log = min(r.utc_start for r in fleet.flights[key] if is_known(r.utc_start))
        total = record.time_in_service_before_s
        done = sorted(record.inspections, key=lambda i: i.done_utc)
        assert [i.name for i in done] == ["annual inspection", "100-hour inspection"], key
        assert all(i.done_utc < first_log for i in done), key
        hundred = done[-1]
        rate = (total - hundred.at_hours_s) / (first_log - hundred.done_utc).total_seconds()
        assert 0 < rate * 86400 < 24 * H, key  # between nothing and a full day's flying per day
        for i in done:
            expected = max(0.0, total - rate * (first_log - i.done_utc).total_seconds())
            assert i.at_hours_s == pytest.approx(expected, abs=1e-6), (key, i.name)
            assert 0.0 <= i.at_hours_s <= total, (key, i.name)
        assert done[0].at_hours_s <= done[1].at_hours_s, key
    # Where the rate does not run out, the annual was done with hours on the clock.
    with_hours = {
        k for k, r in fleet.maintenance.items() if min(i.at_hours_s for i in r.inspections) > 0
    }
    assert with_hours == {"SYN-01", "SYN-02", "SYN-04", "SYN-07"}


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
    assert prop.state == "due_soon" and is_known(prop.remaining)
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


def test_the_seeded_fleet_is_entries_only_and_names_no_real_aircraft() -> None:
    from uas_workbench.fleet.showcase import ALFA_KEY, PX4_KEY

    fleet = generate(CONFIG)
    assert fleet.entries and all(e.synthetic for e in fleet.entries)
    assert all(e.statement.startswith("[synthetic]") for e in fleet.entries)
    assert all("synthetic" in e.entered_by for e in fleet.entries)
    subjects = {e.subject for e in fleet.entries}
    named = {str(v) for e in fleet.entries for v in e.details.values()} | subjects
    assert not named & {ALFA_KEY, PX4_KEY}
    # One synthetic correction, so the demo shows what superseding looks like: SYN-01's
    # time in service was entered wrong and corrected to the value the board uses.
    corrections = [e for e in fleet.entries if e.supersedes is not None]
    assert len(corrections) == 1
    (fix,) = corrections
    assert fix.subject == "SYN-01" and fix.kind == "time_in_service.set"
    assert fix.reason is not None and fix.reason.startswith("[synthetic]")
    assert fleet.maintenance["SYN-01"].time_in_service_before_s == fix.details["before_s"]
    # The hours before the first log are entered the day before the earliest inspection that
    # states hours, the earliest date the write rules accept them at (LIMITS.md).
    for key, record in fleet.maintenance.items():
        hours = [e for e in fleet.entries if e.subject == key and e.kind == "time_in_service.set"]
        earliest = min(i.done_utc for i in record.inspections)
        assert {e.occurred_utc for e in hours} == {earliest - timedelta(days=1)}, key


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
