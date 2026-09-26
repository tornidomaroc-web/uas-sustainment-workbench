"""Component life and inspection due list, and the board state derived from it.

Usage comes from the flight records: logged flight time, plus the flight that reconcile()
found no log covers (the parts wore all the same), minus logs that reconcile() identified
as duplicate uploads. Every limit applies on every basis it is set for, and whichever comes
first decides (EASA MoC to OSO #3). An inspection with a tolerance may be overflown by
that much, and the overflown hours count toward the next interval (14 CFR 91.409(b)).
Calendar limits run to the end of the month (14 CFR 91.409(a), "calendar months").
"""

from __future__ import annotations

import calendar
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime

from uas_workbench.flight.reconcile import FLUSH_INTERVAL_S, reconcile
from uas_workbench.flight.record import FlightRecord, Maybe, Unknown, is_known

from .model import (
    Board,
    Component,
    DueItem,
    DueList,
    DueState,
    InspectionDone,
    MaintenanceRecord,
    Unit,
    Usage,
    WorkOrder,
)
from .policy import LifePolicy, LifeRule

NO_RECORD = "no maintenance record entered for this aircraft"
H = 3600.0


@dataclass(frozen=True)
class _Flown:
    log_ref: str
    utc: Maybe[datetime]
    seconds: float  # flight time, or 0 when the log cannot say
    cycle: bool  # counts as one flight cycle


@dataclass(frozen=True)
class _AircraftUsage:
    flown: tuple[_Flown, ...]
    unlogged: tuple[tuple[Maybe[datetime], float], ...]  # (start of the log it followed, s)
    notes: tuple[str, ...]


def _before(utc: Maybe[datetime], until: datetime | None) -> bool:
    """A flight counts up to `until`; a flight with no UTC start is counted whenever."""
    return until is None or not is_known(utc) or utc <= until


def _usage(
    key: str, records: Sequence[FlightRecord], tolerance_s: float, until: datetime | None = None
) -> _AircraftUsage:
    result = reconcile(records, aircraft_key=key, tolerance_s=tolerance_s)
    by_ref = {r.log_ref: r for r in records}
    flown: list[_Flown] = []
    notes: list[str] = []
    for r in records:
        why = result.unchecked.get(r.log_ref, "")
        if why.startswith("duplicate of "):
            notes.append(f"log {r.log_ref} of aircraft {key} is a {why} and is not counted")
            continue
        if not _before(r.utc_start, until):
            continue
        seconds = r.flight_time_s if is_known(r.flight_time_s) else 0.0
        cycle = not is_known(r.flight_time_s) or r.flight_time_s > 0
        flown.append(_Flown(r.log_ref, r.utc_start, seconds, cycle))
    unlogged = tuple(
        (by_ref[f.log_ref].utc_start, f.seconds)
        for f in result.findings
        if f.kind == "unlogged_flight"
        and f.log_ref in by_ref
        and _before(by_ref[f.log_ref].utc_start, until)
    )
    return _AircraftUsage(tuple(flown), unlogged, tuple(notes))


def time_in_service(
    key: str,
    before_s: float,
    records: Sequence[FlightRecord],
    tolerance_s: float = FLUSH_INTERVAL_S,
    until: datetime | None = None,
) -> float:
    """Total time in service (14 CFR 91.417(a)(2)(i)): the hours before the first log, plus
    the logged flight up to `until`, plus the flight reconcile() found no log covers."""
    own = _usage(key, records, tolerance_s, until)
    return before_s + sum(f.seconds for f in own.flown) + sum(s for _, s in own.unlogged)


def _installed_on(component: Component, key: str, as_of: datetime) -> bool:
    return any(
        i.aircraft_key == key and i.from_utc <= as_of and (i.to_utc is None or as_of < i.to_utc)
        for i in component.installations
    )


def _in_window(utc: Maybe[datetime], start: datetime, end: datetime | None) -> bool:
    return is_known(utc) and start <= utc and (end is None or utc < end)


def _component_usage(component: Component, usage: dict[str, _AircraftUsage]) -> Usage:
    hours_s = component.hours_s_before
    cycles = component.cycles_before
    for inst in component.installations:
        flown = usage[inst.aircraft_key]
        for f in flown.flown:
            if _in_window(f.utc, inst.from_utc, inst.to_utc):
                hours_s += f.seconds
                cycles += int(f.cycle)
        for utc, seconds in flown.unlogged:
            if _in_window(utc, inst.from_utc, inst.to_utc):
                hours_s += seconds
    return Usage(hours_s, cycles)


def end_of_month_after(start: date, months: int) -> date:
    """`months` calendar months after `start`, to the end of that month (14 CFR 91.409(a))."""
    index = start.month - 1 + months
    year, month = start.year + index // 12, index % 12 + 1
    return date(year, month, calendar.monthrange(year, month)[1])


def _state(remaining: float, limit: float, tolerance: float, fraction: float) -> DueState:
    if remaining < 0:
        return "overdue_within_tolerance" if 0 < -remaining <= tolerance else "overdue"
    if remaining <= fraction * limit:
        return "due_soon"
    return "ok"


def _plural(n: float, unit: str) -> str:
    return f"{n:.0f} {unit}{'' if n == 1 else 's'}"


def _component_items(
    key: str, c: Component, rule: LifeRule, usage: Usage, as_of: datetime, fraction: float
) -> list[DueItem]:
    subject = f"{c.kind} {c.id}"
    items: list[DueItem] = []
    unit: Unit
    for basis, limit in rule.limits():
        if basis == "hours":
            used = round(usage.hours_s / H, 3)
            remaining = round(limit - used, 3)
            unit, tolerance = "h", 0.0
            message = (
                f"{subject} on aircraft {key} has {remaining:.1f} h left of its {limit:.0f} h "
                "life limit"
                if remaining >= 0
                else f"{subject} on aircraft {key} is {-remaining:.1f} h past its {limit:.0f} h "
                "life limit"
            )
            scale = limit
        elif basis == "cycles":
            used, remaining = float(usage.cycles), limit - usage.cycles
            unit, tolerance = "cycles", 0.0
            message = (
                f"{subject} on aircraft {key} has {remaining:.0f} cycles left of its "
                f"{limit:.0f}-cycle life limit"
                if remaining >= 0
                else f"{subject} on aircraft {key} is {-remaining:.0f} cycles past its "
                f"{limit:.0f}-cycle life limit"
            )
            scale = limit
        else:
            due = end_of_month_after(c.in_service_since, int(limit))
            scale = float((due - c.in_service_since).days)
            remaining = float((due - as_of.date()).days)
            used = scale - remaining
            unit, tolerance = "days", 0.0
            message = (
                f"{subject} on aircraft {key} has {_plural(remaining, 'day')} left of its "
                f"{limit:.0f}-calendar-month life limit, due by {due}"
                if remaining >= 0
                else f"{subject} on aircraft {key} passed its {limit:.0f}-calendar-month life "
                f"limit on {due}"
            )
        items.append(
            DueItem(
                key,
                subject,
                c.id,
                basis,
                unit,
                used,
                limit,
                remaining,
                tolerance,
                _state(remaining, scale, tolerance, fraction),
                rule.source,
                message,
            )
        )
    return items


def _inspection_items(
    key: str,
    rule: LifeRule,
    done: InspectionDone | None,
    time_in_service_s: float,
    as_of: datetime,
    fraction: float,
) -> list[DueItem]:
    subject = f"the {rule.name}"
    items: list[DueItem] = []
    unit: Unit
    for basis, limit in rule.limits():
        if basis == "hours":
            since_s = (done.at_hours_s - done.carried_over_s) if done else 0.0
            used = round((time_in_service_s - since_s) / H, 3)
            remaining = round(limit - used, 3)
            tolerance = rule.tolerance_hours
            unit = "h"
            scale = limit
            if remaining >= 0:
                message = f"{subject} of aircraft {key} is due in {remaining:.1f} h"
            elif 0 < -remaining <= tolerance:
                message = (
                    f"{subject} of aircraft {key} is {-remaining:.1f} h overdue, within the "
                    f"{tolerance:.0f} h tolerance that allows flight only to reach a place where "
                    "the inspection can be done"
                )
            elif tolerance:
                message = (
                    f"{subject} of aircraft {key} is {-remaining:.1f} h overdue, beyond its "
                    f"{tolerance:.0f} h tolerance"
                )
            else:
                message = f"{subject} of aircraft {key} is {-remaining:.1f} h overdue"
        elif basis == "calendar":
            if done is None:
                continue  # no date to count from; the note says so
            start = done.done_utc.date()
            due = end_of_month_after(start, int(limit))
            scale = float((due - start).days)
            remaining = float((due - as_of.date()).days)
            used = scale - remaining
            tolerance, unit = 0.0, "days"
            message = (
                f"{subject} of aircraft {key} is due by {due}, in {_plural(remaining, 'day')}"
                if remaining >= 0
                else f"{subject} of aircraft {key} was due by {due} and is "
                f"{_plural(-remaining, 'day')} overdue"
            )
        else:
            continue  # an inspection interval in cycles is not modelled
        items.append(
            DueItem(
                key,
                subject,
                None,
                basis,
                unit,
                used,
                limit,
                remaining,
                tolerance,
                _state(remaining, scale, tolerance, fraction),
                rule.source,
                message,
            )
        )
    return items


def due_list(
    aircraft_key: str,
    *,
    maintenance: MaintenanceRecord | None,
    components: Sequence[Component],
    flights_of: Callable[[str], Sequence[FlightRecord]],
    policy: LifePolicy,
    as_of: datetime,
    tolerance_s: float = FLUSH_INTERVAL_S,
) -> DueList:
    """Every limit that applies to `aircraft_key` at `as_of`, with how much of it is used.

    `components` may be the whole fleet's; the ones installed on this aircraft at `as_of` are
    selected here. `flights_of` is asked for every aircraft a selected component has been on,
    because its life status travelled with it (14 CFR 43.10). Flights after `as_of` do not
    count; a flight with no UTC start counts whenever.
    """
    installed = [c for c in components if _installed_on(c, aircraft_key, as_of)]
    if maintenance is None and not installed:
        return DueList(aircraft_key, as_of, False, Unknown(NO_RECORD), ())
    keys = {aircraft_key} | {i.aircraft_key for c in installed for i in c.installations}
    usage = {k: _usage(k, flights_of(k), tolerance_s, until=as_of) for k in keys}
    own = usage[aircraft_key]
    notes = list(own.notes)
    if installed:
        notes.extend(
            f"flight {f.log_ref} of aircraft {aircraft_key} has no UTC start and is not counted "
            "against any component"
            for f in own.flown
            if not is_known(f.utc)
        )
    before = maintenance.time_in_service_before_s if maintenance else 0.0
    time_in_service_s = before + sum(f.seconds for f in own.flown) + sum(s for _, s in own.unlogged)

    items: list[DueItem] = []
    by_id: dict[str, Usage] = {}
    for c in installed:
        rule = policy.component_kinds.get(c.kind)
        if rule is None:
            notes.append(
                f"component kind {c.kind!r} of {c.id} is not in fleet.toml; no limits apply"
            )
            continue
        by_id[c.id] = _component_usage(c, usage)
        items.extend(
            _component_items(aircraft_key, c, rule, by_id[c.id], as_of, policy.due_soon_fraction)
        )
    done_by_name: dict[str, InspectionDone] = {}
    for d in maintenance.inspections if maintenance else ():
        if d.name not in done_by_name or d.done_utc > done_by_name[d.name].done_utc:
            done_by_name[d.name] = d
    for name, rule in policy.inspections.items():
        done = done_by_name.get(name)
        if done is None:
            notes.append(
                f"no completed {name} is recorded for aircraft {aircraft_key}; its interval is "
                "counted from zero time in service"
            )
        items.extend(
            _inspection_items(
                aircraft_key, rule, done, time_in_service_s, as_of, policy.due_soon_fraction
            )
        )
    return DueList(
        aircraft_key,
        as_of,
        True,
        time_in_service_s,
        tuple(items),
        by_id,
        maintenance.work_orders if maintenance else (),
        tuple(notes),
    )


def _work_sentence(key: str, w: WorkOrder) -> str:
    since = w.opened_utc.date().isoformat()
    if w.state == "awaiting_parts":
        return f"aircraft {key} is on the ground awaiting parts since {since}: {w.description}"
    if w.state == "in_work":
        return f"aircraft {key} is in maintenance since {since}: {w.description}"
    return f"aircraft {key} carries a deferred defect since {since}: {w.description}"


def board(due: DueList) -> Board:
    """Worst condition wins: AOG, in maintenance, unserviceable, deferred defects, serviceable.

    "in maintenance" and "AOG" come from open work orders, which only the operator can
    enter; the other three are computed from the due list. Every reason is a full sentence.
    """
    if not due.has_record:
        return Board(Unknown(NO_RECORD), ())
    key = due.aircraft_key
    awaiting = [w for w in due.work_orders if w.state == "awaiting_parts"]
    in_work = [w for w in due.work_orders if w.state == "in_work"]
    deferred = [w for w in due.work_orders if w.state == "deferred"]
    overdue = [i for i in due.items if i.state == "overdue"]
    within = [i for i in due.items if i.state == "overdue_within_tolerance"]
    reasons = (
        *(_work_sentence(key, w) for w in awaiting),
        *(_work_sentence(key, w) for w in in_work),
        *(i.message for i in overdue),
        *(i.message for i in within),
        *(_work_sentence(key, w) for w in deferred),
    )
    if awaiting:
        status = "AOG"
    elif in_work:
        status = "in maintenance"
    elif overdue:
        status = "unserviceable"
    elif within or deferred:
        status = "serviceable with deferred defects"
    else:
        status = "serviceable"
    return Board(status, reasons)
