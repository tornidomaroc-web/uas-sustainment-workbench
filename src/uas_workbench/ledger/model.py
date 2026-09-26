"""The maintenance ledger: append-only entries, and what is projected from them.

An entry records what a person stated: what happened, when it happened, when it was
entered, by whom, and in what words (14 CFR 43.9(a): a description of the work, its date,
the name of the person). Entries are never edited or deleted. A correction is a new entry
that supersedes an older one with a reason; a retraction supersedes without replacing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from uas_workbench.life.model import Component, MaintenanceRecord, WorkState

NOTE = (
    "This entry records what the entering person stated. The workbench does not certify "
    "airworthiness or return to service."
)

AIRCRAFT_KINDS: tuple[str, ...] = (
    "work_order.open",  # details: state, optional work_id (assigned when absent)
    "work_order.state",  # details: work_id, state
    "work_order.close",  # details: work_id
    "inspection.done",  # details: name, optional at_hours_s (derived when absent), carried_over_s
    "time_in_service.set",  # details: before_s, the hours before the first log this tool holds
)
COMPONENT_KINDS: tuple[str, ...] = (
    "component.register",  # details: kind, in_service_since, hours_s_before, cycles_before
    "component.install",  # details: aircraft_key; the time is the entry's occurred_utc
    "component.remove",  # details: aircraft_key
)
RETRACTION = "retraction"  # supersedes an entry and replaces it with nothing
KINDS: tuple[str, ...] = (*AIRCRAFT_KINDS, *COMPONENT_KINDS, RETRACTION)


@dataclass(frozen=True)
class Entry:
    id: int | None  # assigned by the store, in order of entry
    subject: str  # an aircraft key or a component id
    kind: str
    occurred_utc: datetime  # when the event happened
    recorded_utc: datetime  # when it was entered
    entered_by: str  # the person's name and role, as typed; not verified
    statement: str  # the description in the person's words
    details: dict[str, Any]
    supersedes: int | None
    reason: str | None  # why the superseded entry was wrong; required with supersedes
    synthetic: bool


@dataclass(frozen=True)
class WorkOrderState:
    work_id: str
    aircraft_key: str
    opened_utc: datetime
    state: WorkState
    description: str
    closed_utc: datetime | None
    synthetic: bool


@dataclass(frozen=True)
class Projection:
    """What the live entries say, in the records the life engine reads."""

    live: tuple[Entry, ...]  # in the order they occurred
    superseded_by: dict[int, int]  # dead entry id -> the live entry that superseded it
    maintenance: dict[str, MaintenanceRecord] = field(default_factory=dict)
    components: tuple[Component, ...] = ()
    work_orders: dict[str, tuple[WorkOrderState, ...]] = field(default_factory=dict)
    registered_at: dict[str, datetime] = field(default_factory=dict)


class LedgerError(Exception):
    """An entry the ledger refuses, with the HTTP status the API answers and a sentence."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail
