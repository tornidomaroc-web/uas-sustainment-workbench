"""Component life and inspection due list, and the board state derived from it.

Usage comes from the flight records: logged flight time, plus the flight that reconcile()
found no log covers (the parts wore all the same), minus logs that reconcile() identified
as duplicate uploads. Every limit applies on every basis it is set for, and whichever comes
first decides (EASA MoC to OSO #3). An inspection with a tolerance may be overflown by
that much, and the overflown hours count toward the next interval (14 CFR 91.409(b)); an
annual inspection starts the 100-hour interval too ("an annual or 100-hour inspection").
Calendar limits run to the end of the month (14 CFR 91.409(a), "calendar months").

A limit counted in completed units is reached when its last unit completes. A flight's cycle
and hours count once the flight is over: from the end of its log, the last instant its flight
can have ended (log_end), never from its start. A part whose cycles or hours have reached its
limit, exactly or past it, grounds the aircraft: nothing of its life is left for another
flight, and the ledger refuses to fit such a part for the same reason (14 CFR 43.10(c),
"after it has reached its life limit"). A calendar life is counted in days and its last day
completes at midnight UTC, so on that day the part is still within its life. Every date is
the UTC date of its instant, whatever offset the instant was written with (issue #149).

An inspection interval is counted from the completion on record that starts it. With none on
record, an hours interval is counted from zero time in service; a calendar interval has no
date to count from, so its item is not known with that reason, never left out, and the board
is not known with it unless something grounds the aircraft, which wins.

The hours before the first log are one total, reached by the first log this tool holds.
Before that log the time in service is not known, the inspection items that depend on it
are not known, and the board is not known with that reason unless an open work order or a
life limit reached or past grounds the aircraft, which wins.

A log whose flight time is not known flew at most its span, so it counts the most the records
allow (issue #152): its whole span toward the time in service and toward each part fitted
when it started, and toward an inspection's interval its whole span when it can lie inside
the interval (after the inspection, or with no UTC start), and when it was flown before the
inspection only as far as the inspection's hours exceed what the dated logs give at its date
(stated hours hold its real flight already). The least the records allow counts it at 0 h,
as every figure did before: an inspection's derived hours take the least, so the interval
after it holds every hour the records can put there. Each hour item such a log moves says so
in its sentence, with the least, and the due list names each such log.
"""

from __future__ import annotations

import calendar
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta

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
    in_utc,
)
from .policy import LifePolicy, LifeRule

NO_RECORD = "no maintenance record entered for this aircraft"
# The end of the sentence of an inspection whose calendar interval has no completion on
# record to count from; life/cue.py gives such an item and such a board a cue of their own.
NO_DATE = "has no date to count from"
# The start of the reason of an inspection whose recorded hours are more than the time in
# service the records give at its date (issue #151); life/cue.py gives it a cue of its own.
DISAGREE = "the records disagree"
H = 3600.0
# How far a recorded inspection's hours may pass the time in service at its date: the
# ledger's own allowance when it accepts one (ledger.validate), a second.
HOURS_SLACK_S = 1.0


@dataclass(frozen=True)
class _Flown:
    log_ref: str
    utc: Maybe[datetime]
    seconds: float  # flight time, the most it can have been: the span when the log cannot say
    cycle: bool  # counts as one flight cycle
    end: Maybe[datetime]  # log_end(): when it counts
    unknown: bool  # its flight time is not known, so `seconds` is the log's span

    @property
    def least_s(self) -> float:
        """The least it can have flown: its flight time, or 0 when the log cannot say."""
        return 0.0 if self.unknown else self.seconds


@dataclass(frozen=True)
class HoursBound:
    """What logs whose flight time is not known add to an hours figure (issue #152): the
    figure with each at 0 h, what they add at the most the records allow, and each log that
    adds, as (aircraft key, log ref, seconds added)."""

    least_s: float
    seconds: float
    logs: tuple[tuple[str, str, float], ...]


def counting_unknown(logs: int, seconds: float) -> str:
    """The words that say a figure counts logs whose flight time is not known at the most."""
    which = "1 log" if logs == 1 else f"{logs} logs"
    return (
        f"counting {which} whose flight time is not known at the most the records allow "
        f"({seconds / H:.1f} h)"
    )


def _bound_clause(bound: HoursBound | None, least: float, used: float) -> str:
    """The end of an hour item's sentence when such logs move it; empty when none does."""
    if bound is None:
        return ""
    return (
        f", {counting_unknown(len(bound.logs), bound.seconds)}: at least {least:.1f} h used, "
        f"at most {used:.1f} h"
    )


def _span_note(key: str, ref: str, seconds: float) -> str:
    return (
        f"flight {ref} of aircraft {key} has no flight time on record, so the whole "
        f"{seconds / H:.1f} h span of its log counts as flown, the most the records allow"
    )


@dataclass(frozen=True)
class _AircraftUsage:
    flown: tuple[_Flown, ...]
    unlogged: tuple[tuple[Maybe[datetime], float], ...]  # (start of the log it followed, s)
    notes: tuple[str, ...]


def log_end(record: FlightRecord) -> Maybe[datetime]:
    """When a log's flight counts: the end of the log.

    A record holds no landing time: its flight time is a total airborne within the log, not
    an interval. The log's end is the last instant its flight can have ended, so that is when
    its cycle and hours count. A log with no UTC start has no end either."""
    if not is_known(record.utc_start):
        return record.utc_start
    return record.utc_start + timedelta(seconds=record.log_span_s)


def counted(record: FlightRecord, until: datetime | None, undated: bool = True) -> bool:
    """A log counts once it has ended by `until`; a log with no UTC start counts whenever,
    or, with `undated` False, never: then only the logs the records can place count."""
    end = log_end(record)
    if not is_known(end):
        return undated
    return until is None or end <= until


def _usage(
    key: str,
    records: Sequence[FlightRecord],
    tolerance_s: float,
    until: datetime | None = None,
    undated: bool = True,
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
        if not counted(r, until, undated):
            continue
        # A log that cannot say how long it flew flew at most its span (issue #152).
        unknown = not is_known(r.flight_time_s)
        seconds = r.flight_time_s if is_known(r.flight_time_s) else max(r.log_span_s, 0.0)
        cycle = not is_known(r.flight_time_s) or r.flight_time_s > 0
        flown.append(_Flown(r.log_ref, r.utc_start, seconds, cycle, log_end(r), unknown))
    # Flight no log covers has no recorded end. It was flown after the log it followed, so it
    # counts with that log: the earliest it can have been flown, never later than it was.
    unlogged = tuple(
        (by_ref[f.log_ref].utc_start, f.seconds)
        for f in result.findings
        if f.kind == "unlogged_flight"
        and f.log_ref in by_ref
        and counted(by_ref[f.log_ref], until, undated)
    )
    return _AircraftUsage(tuple(flown), unlogged, tuple(notes))


def _first_log(records: Sequence[FlightRecord]) -> datetime | None:
    starts = [r.utc_start for r in records if is_known(r.utc_start)]
    return min(starts) if starts else None


def _not_known(
    key: str, before_s: float, first_log: datetime | None, until: datetime | None
) -> Unknown | None:
    """Why the time in service at `until` is not known, or None when it is.

    The hours before the first log are one total, reached by the first log; before it, the
    records hold no date to place them on. With no hours entered, or no dated log, or no
    date asked (now), the total stands."""
    if until is None or before_s <= 0 or first_log is None or until >= first_log:
        return None
    return Unknown(
        f"the time in service of aircraft {key} before its first log on {first_log:%Y-%m-%d} "
        f"is not known: {before_s / H:.1f} h had been flown by then, on dates the records "
        "do not hold"
    )


def time_in_service(
    key: str,
    before_s: float,
    records: Sequence[FlightRecord],
    tolerance_s: float = FLUSH_INTERVAL_S,
    until: datetime | None = None,
    undated: bool = True,
) -> Maybe[float]:
    """Total time in service (14 CFR 91.417(a)(2)(i)): the hours before the first log, plus
    the flight of the logs that had ended by `until`, plus the flight reconcile() found no
    log covers. Not known before the first log when hours before it were entered.

    A log with no UTC start counts at any date, and a log whose flight time is not known
    counts its whole span (issue #152), so the total at `until` is the most the records
    allow. With `undated` False it is the least: only the logs the records place by `until`,
    each whose flight time is not known at 0 h. An inspection's derived hours take the least
    (issue #151), so such a log falls inside the interval after it, whichever was entered
    first."""
    unknown = _not_known(key, before_s, _first_log(records), until)
    if unknown is not None:
        return unknown
    own = _usage(key, records, tolerance_s, until, undated)
    least = _least_s(before_s, own)
    return least + _spans_s(own.flown) if undated else least


def _least_s(before_s: float, own: _AircraftUsage) -> float:
    """The time in service with each log whose flight time is not known at 0 h."""
    return before_s + sum(f.least_s for f in own.flown) + sum(s for _, s in own.unlogged)


def _spans_s(flown: Sequence[_Flown]) -> float:
    """What the logs whose flight time is not known add at the most: their spans."""
    return sum(f.seconds for f in flown if f.unknown)


def most_time_in_service(
    key: str,
    before_s: float,
    records: Sequence[FlightRecord],
    tolerance_s: float = FLUSH_INTERVAL_S,
    until: datetime | None = None,
    spans: bool = True,
) -> float:
    """The most time in service the records allow at `until`, known at any date.

    From the first log on it is time_in_service(). Before the first log the hours before it
    are reached only by the first log, so the aircraft had at most those hours plus what the
    logs with no UTC start add, which may have been flown at any date (issue #151). A log
    whose flight time is not known counts its whole span; with `spans` False, 0 h, the
    figure a reading that must be a least is taken against (issue #152)."""
    own = _usage(key, records, tolerance_s, until)
    least = _least_s(before_s, own)
    return least + _spans_s(own.flown) if spans else least


def _installed_on(component: Component, key: str, as_of: datetime) -> bool:
    return any(
        i.aircraft_key == key and i.from_utc <= as_of and (i.to_utc is None or as_of < i.to_utc)
        for i in component.installations
    )


def _in_window(utc: Maybe[datetime], start: datetime, end: datetime | None) -> bool:
    return is_known(utc) and start <= utc and (end is None or utc < end)


def _component_usage(
    component: Component, usage: dict[str, _AircraftUsage]
) -> tuple[Usage, HoursBound | None]:
    """The usage, its hours the most the records allow, and what logs whose flight time is
    not known add to them (None when none adds): each at its whole span, since the records
    place it in the fitting it started in but cannot say how much of it was flown."""
    hours_s = component.hours_s_before
    cycles = component.cycles_before
    spans: list[tuple[str, str, float]] = []
    for inst in component.installations:
        flown = usage[inst.aircraft_key]
        for f in flown.flown:
            if _in_window(f.utc, inst.from_utc, inst.to_utc):
                hours_s += f.least_s
                cycles += int(f.cycle)
                if f.unknown and f.seconds > 0:
                    spans.append((inst.aircraft_key, f.log_ref, f.seconds))
        for utc, seconds in flown.unlogged:
            if _in_window(utc, inst.from_utc, inst.to_utc):
                hours_s += seconds
    if not spans:
        return Usage(hours_s, cycles), None
    added = sum(s for _, _, s in spans)
    return Usage(hours_s + added, cycles), HoursBound(hours_s, added, tuple(spans))


def component_hours(
    component: Component,
    flights_of: Callable[[str], Sequence[FlightRecord]],
    tolerance_s: float = FLUSH_INTERVAL_S,
    until: datetime | None = None,
) -> tuple[Usage, HoursBound | None]:
    """component_usage(), and what logs whose flight time is not known add to its hours."""
    keys = {i.aircraft_key for i in component.installations}
    usage = {k: _usage(k, flights_of(k), tolerance_s, until) for k in keys}
    return _component_usage(component, usage)


def component_usage(
    component: Component,
    flights_of: Callable[[str], Sequence[FlightRecord]],
    tolerance_s: float = FLUSH_INTERVAL_S,
    until: datetime | None = None,
) -> Usage:
    """The hours and cycles a component has reached by `until`: its usage before this tool's
    first record, plus every flight of every airframe it was fitted to while it was fitted
    (14 CFR 43.10), counted as due_list() counts them: a log from its end, in the
    installation window it started in, a log whose flight time is not known at its span."""
    return component_hours(component, flights_of, tolerance_s, until)[0]


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
    key: str,
    c: Component,
    rule: LifeRule,
    usage: Usage,
    as_of: datetime,
    fraction: float,
    bound: HoursBound | None = None,
) -> list[DueItem]:
    subject = f"{c.kind} {c.id}"
    items: list[DueItem] = []
    unit: Unit
    for basis, limit in rule.limits():
        # Hours and cycles are completed units: at remaining 0 the whole life is used and the
        # limit is reached, which grounds as past does. A calendar life at remaining 0 is on
        # its last day, which is not over: it stays within its life until the day ends.
        reached = False
        if basis == "hours":
            used = round(usage.hours_s / H, 3)
            remaining = round(limit - used, 3)
            unit, tolerance = "h", 0.0
            reached = remaining <= 0
            if remaining > 0:
                message = (
                    f"{subject} on aircraft {key} has {remaining:.1f} h left of its "
                    f"{limit:.0f} h life limit"
                )
            elif remaining == 0:
                message = f"{subject} on aircraft {key} has reached its {limit:.0f} h life limit"
            else:
                message = (
                    f"{subject} on aircraft {key} is {-remaining:.1f} h past its {limit:.0f} h "
                    "life limit"
                )
            if bound is not None:
                message += _bound_clause(bound, round(bound.least_s / H, 3), used)
            scale = limit
        elif basis == "cycles":
            used, remaining = float(usage.cycles), limit - usage.cycles
            unit, tolerance = "cycles", 0.0
            reached = remaining <= 0
            if remaining > 0:
                message = (
                    f"{subject} on aircraft {key} has {_plural(remaining, 'cycle')} left of its "
                    f"{limit:.0f}-cycle life limit"
                )
            elif remaining == 0:
                message = (
                    f"{subject} on aircraft {key} has reached its {limit:.0f}-cycle life limit"
                )
            else:
                message = (
                    f"{subject} on aircraft {key} is {_plural(-remaining, 'cycle')} past its "
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
                "overdue" if reached else _state(remaining, scale, tolerance, fraction),
                rule.source,
                message,
            )
        )
    return items


def _inspection_items(
    key: str,
    rule: LifeRule,
    done: InspectionDone | None,
    time_in_service_s: Maybe[float],
    as_of: datetime,
    fraction: float,
    disagree: Unknown | None = None,
    extra: HoursBound | None = None,
) -> list[DueItem]:
    """The items of one inspection. `time_in_service_s` counts each log whose flight time is
    not known at 0 h; `extra` is what such logs add to the hours interval at the most the
    records allow (_interval_bound), and its sentence then says so."""
    subject = f"the {rule.name}"
    items: list[DueItem] = []
    unit: Unit
    for basis, limit in rule.limits():
        not_known = time_in_service_s if isinstance(time_in_service_s, Unknown) else disagree
        if basis == "hours" and not_known is not None:
            items.append(
                DueItem(
                    key,
                    subject,
                    None,
                    basis,
                    "h",
                    not_known,
                    limit,
                    not_known,
                    rule.tolerance_hours,
                    "unknown",
                    rule.source,
                    f"{subject} of aircraft {key} cannot be measured on {as_of:%Y-%m-%d}: "
                    f"{not_known.reason}",
                )
            )
            continue
        if basis == "hours":
            assert not isinstance(time_in_service_s, Unknown)
            since_s = (done.at_hours_s - done.carried_over_s) if done else 0.0
            least = round((time_in_service_s - since_s) / H, 3)
            added = extra.seconds if extra is not None else 0.0
            used = round((time_in_service_s + added - since_s) / H, 3)
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
            message += _bound_clause(extra, least, used)
        elif basis == "calendar":
            if done is None:
                # No date to count from: the interval cannot be counted, and that is said,
                # never skipped (issue #150). The board is then not known with this reason.
                names = " or ".join((rule.name, *rule.satisfied_by))
                not_known = Unknown(
                    f"{subject} of aircraft {key} cannot be counted on {as_of:%Y-%m-%d}: no "
                    f"completed {names} is recorded, so its {limit:.0f}-calendar-month "
                    f"interval {NO_DATE}"
                )
                items.append(
                    DueItem(
                        key,
                        subject,
                        None,
                        basis,
                        "days",
                        not_known,
                        limit,
                        not_known,
                        0.0,
                        "unknown",
                        rule.source,
                        not_known.reason,
                    )
                )
                continue
            start = in_utc(done.done_utc).date()
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


def _disagreement(
    key: str,
    done: InspectionDone,
    before_s: float,
    records: Sequence[FlightRecord],
    tolerance_s: float,
) -> Unknown | None:
    """Why the hours recorded for `done` disagree with the records (issue #151), or None.

    Its recorded hours are more than the most time in service the records give at its date,
    with the hours before the first log now on record (before the first log: those hours
    plus the logs with no UTC start, most_time_in_service); or, derived, more than the least,
    the logs the records place by then, which is what the tool derives now: the difference
    can only be logs with no UTC start, which the records cannot place before it. None when
    they agree. The ledger refuses such records; a store written before it can hold them."""
    until = done.done_utc
    most = most_time_in_service(key, before_s, records, tolerance_s, until=until)
    on = f"the {done.name} of {in_utc(until):%Y-%m-%d} is recorded at {done.at_hours_s / H:.1f} h"
    if done.at_hours_s > most + HOURS_SLACK_S:
        return Unknown(
            f"{DISAGREE}: {on}, more than the {most / H:.1f} h time in service the records give "
            f"by then, counting {before_s / H:.1f} h before the first log; correct the "
            "inspection or the hours before the first log"
        )
    if not done.derived:
        return None
    least = time_in_service(key, before_s, records, tolerance_s, until=until, undated=False)
    if not is_known(least) or done.at_hours_s <= least + HOURS_SLACK_S:
        return None
    return Unknown(
        f"{DISAGREE}: {on}, derived, more than the {least / H:.1f} h the dated logs give by "
        "then: the difference can only be logs with no UTC start, which the records cannot "
        "place before it; correct the inspection, stating its hours"
    )


def _interval_bound(
    key: str,
    flown: Sequence[_Flown],
    done: InspectionDone | None,
    before_s: float,
    records: Sequence[FlightRecord],
    tolerance_s: float,
) -> HoursBound | None:
    """What logs whose flight time is not known add to the interval since `done`, at the most
    the records allow (issue #152); None when they add nothing. least_s is not used.

    A log that ended after the inspection, or has no UTC start (#151), can lie wholly inside
    the interval: its whole span. A log that ended by the inspection's date was flown before
    it; its real flight is in the inspection's hours only as far as they exceed what the
    dated logs give at that date, each such log at 0 h. A derived figure is that least, so it
    adds nothing; a stated figure adds the difference, up to those logs' spans. Never more:
    the hours the inspection states hold that flight already, and counting it again would put
    every hour of ground time before the inspection inside the interval after it."""
    unknown = [f for f in flown if f.unknown and f.seconds > 0]
    if not unknown:
        return None
    if done is None:
        return HoursBound(
            0.0, _spans_s(unknown), tuple((key, f.log_ref, f.seconds) for f in unknown)
        )
    when = done.done_utc

    def before_it(f: _Flown) -> bool:
        return is_known(f.end) and f.end <= when

    after = [(key, f.log_ref, f.seconds) for f in unknown if not before_it(f)]
    earlier = [f for f in unknown if before_it(f)]
    added = sum(s for _, _, s in after)
    if earlier:
        least = time_in_service(key, before_s, records, tolerance_s, until=when, undated=False)
        room = done.at_hours_s - least if is_known(least) else 0.0
        share = min(max(room, 0.0), _spans_s(earlier))
        if share > 0:
            added += share
            # Together they account for the share; each is named.
            after = [(key, f.log_ref, f.seconds) for f in earlier] + after
    if added <= 0:
        return None
    return HoursBound(0.0, added, tuple(after))


def _disagreeing_items(
    key: str,
    rule: LifeRule,
    done: InspectionDone,
    disagree: Unknown,
    before_s: float,
    records: Sequence[FlightRecord],
    time_in_service_s: Maybe[float],
    as_of: datetime,
    fraction: float,
    tolerance_s: float,
    flown: Sequence[_Flown] = (),
) -> list[DueItem]:
    """The items of an inspection whose recorded hours the records disagree with (#151).

    Its interval counts the hours flown since it, and the records give those without its
    recorded figure. From the first log on, the least time in service at its date is what
    the logs place by then: the item reads as if the inspection were done at that figure,
    so the interval holds every hour the records can put after it (a log with no UTC start
    falls inside it, as for a derived figure), and the sentence says the records disagree.
    Before the first log the records hold no such figure: the dated logs since it are the
    least the interval holds, so the item reads overdue when they alone put it past its
    tolerance, and not known otherwise. Never later than the records allow.

    `time_in_service_s` counts each log whose flight time is not known at 0 h. Read from the
    least at its date, the interval adds what such logs can put inside it (_interval_bound);
    before the first log the "at least" reading stays on the least, those logs at 0 h on both
    sides, so it says only what is certain."""
    until = done.done_utc
    least = time_in_service(key, before_s, records, tolerance_s, until=until, undated=False)
    if is_known(least):
        at, exact = least, True
    else:
        at = most_time_in_service(key, before_s, records, tolerance_s, until=until, spans=False)
        exact = False
    read = replace(done, at_hours_s=at)
    extra = _interval_bound(key, flown, read, before_s, records, tolerance_s) if exact else None
    items: list[DueItem] = []
    for i in _inspection_items(key, rule, read, time_in_service_s, as_of, fraction, extra=extra):
        if i.basis != "hours":
            items.append(i)
        elif exact:
            items.append(
                replace(
                    i,
                    message=f"{i.message}, counted from {at / H:.1f} h, the least time in "
                    f"service the records give at its date; {disagree.reason}",
                )
            )
        elif i.state == "overdue":
            assert is_known(i.remaining)
            beyond = f", beyond its {i.tolerance:.0f} h tolerance" if i.tolerance else ""
            items.append(
                replace(
                    i,
                    message=f"{i.subject} of aircraft {key} is at least {-i.remaining:.1f} h "
                    f"overdue{beyond}, counting only the hours the dated logs give since it; "
                    f"{disagree.reason}",
                )
            )
        else:
            (hours,) = (
                u
                for u in _inspection_items(
                    key, rule, done, time_in_service_s, as_of, fraction, disagree
                )
                if u.basis == "hours"
            )
            items.append(hours)
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
    because its life status travelled with it (14 CFR 43.10). A log counts once it has ended
    by `as_of` (log_end); a flight with no UTC start counts whenever. Dates are taken in UTC:
    `as_of` is converted to UTC first, and so is the due list's `as_of` (issue #149).
    """
    as_of = in_utc(as_of)
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
    not_known = _not_known(aircraft_key, before, _first_log(flights_of(aircraft_key)), as_of)
    # The time in service counts each log whose flight time is not known at its span, the most
    # the records allow (issue #152). The inspection items are counted from the least, each
    # such log at 0 h, and add what such logs can put inside their interval (_interval_bound).
    least_s = _least_s(before, own)
    least_in_service: Maybe[float] = not_known or least_s
    time_in_service_s: Maybe[float] = not_known or (least_s + _spans_s(own.flown))

    items: list[DueItem] = []
    by_id: dict[str, Usage] = {}
    kinds: list[str] = []
    # Each log whose flight time is not known that this due list counts is named: the
    # aircraft's own, and another airframe's that a fitted part carries.
    spans = [_span_note(aircraft_key, f.log_ref, f.seconds) for f in own.flown if f.unknown]
    for c in installed:
        rule = policy.component_kinds.get(c.kind)
        if rule is None:
            kinds.append(
                f"component kind {c.kind!r} of {c.id} is not in fleet.toml; no limits apply"
            )
            continue
        by_id[c.id], bound = _component_usage(c, usage)
        items.extend(
            _component_items(
                aircraft_key, c, rule, by_id[c.id], as_of, policy.due_soon_fraction, bound
            )
        )
        for key, ref, seconds in bound.logs if bound is not None else ():
            note = _span_note(key, ref, seconds)
            if key != aircraft_key and note not in spans:
                spans.append(note)
    notes.extend(spans)
    notes.extend(kinds)
    done_by_name: dict[str, InspectionDone] = {}
    for d in maintenance.inspections if maintenance else ():
        if d.name not in done_by_name or d.done_utc > done_by_name[d.name].done_utc:
            done_by_name[d.name] = d
    for name, rule in policy.inspections.items():
        # The later of this inspection and any that satisfies it starts the interval.
        satisfying = [done_by_name[n] for n in (name, *rule.satisfied_by) if n in done_by_name]
        done = max(satisfying, key=lambda d: d.done_utc) if satisfying else None
        bases = {basis for basis, _ in rule.limits()}
        counted_from_zero = "hours" in bases and is_known(time_in_service_s)
        if done is None and (counted_from_zero or "calendar" in bases):
            # Hours are counted from zero time in service; a calendar interval has no date to
            # count from and cannot be counted.
            if "calendar" not in bases:
                how = "its interval is counted from zero time in service"
            elif not counted_from_zero:
                how = "its interval cannot be counted"
            else:
                how = (
                    "its hours are counted from zero time in service and its calendar "
                    "interval cannot be counted"
                )
            notes.append(
                f"no completed {' or '.join((name, *rule.satisfied_by))} is recorded for "
                f"aircraft {aircraft_key}; {how}"
            )
        disagree = None
        if done is not None and "hours" in bases and is_known(time_in_service_s):
            disagree = _disagreement(
                aircraft_key, done, before, flights_of(aircraft_key), tolerance_s
            )
            # The records cannot place a log with no UTC start on either side of the
            # completion: it counts in the time in service at any date, and is named.
            notes.extend(
                f"flight {f.log_ref} of aircraft {aircraft_key} has no UTC start, so the records "
                f"cannot place it before or after the {done.name} of "
                f"{in_utc(done.done_utc):%Y-%m-%d}; its {f.seconds / H:.1f} h count in the time "
                "in service at any date"
                for f in own.flown
                if not is_known(f.utc)
            )
        if disagree is None or done is None:
            extra = None
            if "hours" in bases and is_known(least_in_service):
                extra = _interval_bound(
                    aircraft_key, own.flown, done, before, flights_of(aircraft_key), tolerance_s
                )
            items.extend(
                _inspection_items(
                    aircraft_key,
                    rule,
                    done,
                    least_in_service,
                    as_of,
                    policy.due_soon_fraction,
                    extra=extra,
                )
            )
        else:
            items.extend(
                _disagreeing_items(
                    aircraft_key,
                    rule,
                    done,
                    disagree,
                    before,
                    flights_of(aircraft_key),
                    least_in_service,
                    as_of,
                    policy.due_soon_fraction,
                    tolerance_s,
                    own.flown,
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
    since = in_utc(w.opened_utc).date().isoformat()
    if w.state == "awaiting_parts":
        return f"aircraft {key} is on the ground awaiting parts since {since}: {w.description}"
    if w.state == "in_work":
        return f"aircraft {key} is in maintenance since {since}: {w.description}"
    return f"aircraft {key} carries a deferred defect since {since}: {w.description}"


def grounding_reasons(due: DueList) -> tuple[str, ...]:
    """The reasons that ground the aircraft, worst first as board() lists them: open work
    orders awaiting parts or in work, and limits reached or past. Empty when nothing does."""
    key = due.aircraft_key
    return (
        *(_work_sentence(key, w) for w in due.work_orders if w.state == "awaiting_parts"),
        *(_work_sentence(key, w) for w in due.work_orders if w.state == "in_work"),
        *(i.message for i in due.items if i.state == "overdue"),
    )


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
    unknown = [i for i in due.items if i.state == "unknown"]
    within = [i for i in due.items if i.state == "overdue_within_tolerance"]
    reasons = (
        *(_work_sentence(key, w) for w in awaiting),
        *(_work_sentence(key, w) for w in in_work),
        *(i.message for i in overdue),
        *(i.message for i in unknown),
        *(i.message for i in within),
        *(_work_sentence(key, w) for w in deferred),
    )
    status: Maybe[str]
    if awaiting:
        status = "AOG"
    elif in_work:
        status = "in maintenance"
    elif overdue:
        status = "unserviceable"
    elif unknown:
        # Serviceability cannot be asserted, with or without deferred defects; what is not
        # known is said, never a state that reads as leave to fly.
        tis = due.time_in_service_s
        status = tis if isinstance(tis, Unknown) else Unknown(unknown[0].message)
    elif within or deferred:
        status = "serviceable with deferred defects"
    else:
        status = "serviceable"
    return Board(status, reasons)
