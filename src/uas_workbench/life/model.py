"""Maintenance records an operator enters, and what the engine derives from them.

Logs never hold any of this (LIMITS.md): which part is on which airframe, when an
inspection was done, what work is open. The record content follows 14 CFR 43.9(a): a
description of the work and its date. Life status travels with a part (14 CFR 43.10), so a
component carries its own usage before this tool's first record and the list of airframes
it has been installed on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Literal

from uas_workbench.flight.record import Maybe

Basis = Literal["hours", "cycles", "calendar"]
# "unknown": the item depends on a time in service the records cannot give at that date, or
# is a calendar interval with no completion on record to count from.
DueState = Literal["ok", "due_soon", "overdue_within_tolerance", "overdue", "unknown"]
WorkState = Literal["deferred", "in_work", "awaiting_parts"]
Unit = Literal["h", "cycles", "days"]


def in_utc(t: datetime) -> datetime:
    """The instant `t` written in UTC (issue #149): 2025-09-01T02:00+05:00 is 2025-08-31 21:00
    in UTC. Times are shown in UTC. A time with no offset is UTC already, as everywhere a time
    comes in, and is returned as it is."""
    return t if t.tzinfo is None else t.astimezone(UTC)


# A time given with an offset has two dates: its date as given and its UTC date. Which one an
# authority counts a calendar month in cannot be known here, and each alone reads late one way
# (0.6.6 took the date as given, #149; #159 the UTC date, #174). A calendar interval is
# counted at the bound that is never late: from the earlier of its start's two dates, to the
# later of its end's. The cost is a reading up to one day early, and at a month's end a due
# date one month early; the item's sentence says so where it applies.


def counted_from(t: datetime) -> date:
    """The date a calendar interval starting at `t` is counted from: the earlier of its date
    as given and its UTC date (issue #174). 2025-08-31T22:00-05:00 counts from 2025-08-31 and
    2025-09-01T02:00+05:00 from 2025-08-31."""
    return min(t.date(), in_utc(t).date())


def counted_to(t: datetime) -> date:
    """The date a calendar interval is counted to at `t`: the later of its date as given and
    its UTC date (issue #174). 2026-09-01T02:00+05:00 counts to 2026-09-01 and
    2026-08-31T23:30-05:00 to 2026-09-01."""
    return max(t.date(), in_utc(t).date())


@dataclass(frozen=True)
class Installation:
    """One period during which a component was fitted to one airframe. to_utc None = still on."""

    aircraft_key: str
    from_utc: datetime
    to_utc: datetime | None


@dataclass(frozen=True)
class Component:
    id: str
    kind: str  # a kind named in fleet.toml [[life.component_kinds]]
    in_service_since: date  # calendar life counts from here
    hours_s_before: float  # usage before the first flight record this tool holds
    cycles_before: int
    installations: tuple[Installation, ...]
    synthetic: bool


@dataclass(frozen=True)
class InspectionDone:
    """A completed inspection: when, and at what aircraft time in service.

    carried_over_s: hours the previous interval was overflown by, which 14 CFR 91.409(b) counts
    toward this one, so the next due point is at_hours_s - carried_over_s + interval.
    derived: at_hours_s is the tool's own figure, filled in when the person stated none.
    """

    name: str  # a name in fleet.toml [[life.inspections]]
    done_utc: datetime
    at_hours_s: float
    carried_over_s: float = 0.0
    derived: bool = False


@dataclass(frozen=True)
class WorkOrder:
    opened_utc: datetime
    description: str  # 14 CFR 43.9(a)(1): a description of the work
    state: WorkState
    synthetic: bool


@dataclass(frozen=True)
class MaintenanceRecord:
    """Everything the operator entered for one airframe."""

    aircraft_key: str
    time_in_service_before_s: float  # 14 CFR 91.417(a)(2)(i) total time, before the first log
    inspections: tuple[InspectionDone, ...]
    work_orders: tuple[WorkOrder, ...]
    synthetic: bool


# ---- derived --------------------------------------------------------------------------


@dataclass(frozen=True)
class Usage:
    hours_s: float
    cycles: int


@dataclass(frozen=True)
class DueItem:
    """One limit on one basis, for one component or one inspection of the aircraft."""

    aircraft_key: str
    subject: str  # "battery pack BAT-04A" or "the 100-hour inspection"
    component_id: str | None
    basis: Basis
    unit: Unit
    used: Maybe[float]  # not known when the state is "unknown"
    limit: float
    remaining: Maybe[float]
    tolerance: float  # hours the limit may be exceeded by; 0 for a life limit
    state: DueState
    source: str  # the public civil source cited in fleet.toml
    message: str


@dataclass(frozen=True)
class DueList:
    aircraft_key: str
    as_of: datetime
    has_record: bool
    time_in_service_s: Maybe[float]
    items: tuple[DueItem, ...]
    usage: dict[str, Usage] = field(default_factory=dict)  # component id -> usage
    work_orders: tuple[WorkOrder, ...] = ()
    notes: tuple[str, ...] = ()
    # The date its calendar items are counted to (counted_to of the as_of given, issue #174);
    # as_of itself is the instant, in UTC. None: the UTC date of as_of.
    day: date | None = None


@dataclass(frozen=True)
class Board:
    """The board state in civil vocabulary and, for every state but serviceable, why."""

    status: Maybe[str]
    reasons: tuple[str, ...]
