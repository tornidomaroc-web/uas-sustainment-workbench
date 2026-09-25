"""Maintenance records an operator enters, and what the engine derives from them.

Logs never hold any of this (LIMITS.md): which part is on which airframe, when an
inspection was done, what work is open. The record content follows 14 CFR 43.9(a): a
description of the work and its date. Life status travels with a part (14 CFR 43.10), so a
component carries its own usage before this tool's first record and the list of airframes
it has been installed on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal

from uas_workbench.flight.record import Maybe

Basis = Literal["hours", "cycles", "calendar"]
DueState = Literal["ok", "due_soon", "overdue_within_tolerance", "overdue"]
WorkState = Literal["deferred", "in_work", "awaiting_parts"]
Unit = Literal["h", "cycles", "days"]


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
    """

    name: str  # a name in fleet.toml [[life.inspections]]
    done_utc: datetime
    at_hours_s: float
    carried_over_s: float = 0.0


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
    used: float
    limit: float
    remaining: float
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


@dataclass(frozen=True)
class Board:
    """The board state in civil vocabulary and, for every state but serviceable, why."""

    status: Maybe[str]
    reasons: tuple[str, ...]
