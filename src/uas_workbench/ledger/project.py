"""Fold the live entries into the records the life engine reads.

An entry is dead when a live newer entry supersedes it. Deciding from the newest entry
backwards makes that well defined: the newest entry is always live, and undoing a
correction (superseding the correction) revives the entry it had corrected.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime
from typing import cast

from uas_workbench.life.model import (
    Component,
    InspectionDone,
    Installation,
    MaintenanceRecord,
    WorkOrder,
    WorkState,
)

from .model import AIRCRAFT_KINDS, Entry, Projection, WorkOrderState


def liveness(entries: Iterable[Entry]) -> tuple[list[Entry], dict[int, int]]:
    """Live entries in the order they occurred, and dead entry id -> superseding entry id."""
    superseded_by: dict[int, int] = {}
    live: list[Entry] = []
    for e in sorted(entries, key=lambda e: e.id or 0, reverse=True):
        if e.id in superseded_by:
            continue
        live.append(e)
        if e.supersedes is not None and e.id is not None:
            superseded_by[e.supersedes] = e.id
    live.sort(key=lambda e: (e.occurred_utc, e.id or 0))
    return live, superseded_by


class _Aircraft:
    def __init__(self) -> None:
        self.before_s = 0.0
        self.inspections: list[InspectionDone] = []
        self.orders: dict[str, WorkOrderState] = {}
        self.entries = 0
        self.synthetic = True


class _Component:
    def __init__(self, e: Entry) -> None:
        p = e.details
        self.kind = str(p["kind"])
        self.in_service_since = date.fromisoformat(str(p["in_service_since"]))
        self.hours_s_before = float(p["hours_s_before"])
        self.cycles_before = int(p["cycles_before"])
        self.installations: list[Installation] = []
        self.synthetic = e.synthetic
        self.registered_at = e.occurred_utc


def project(entries: Iterable[Entry]) -> Projection:
    live, superseded_by = liveness(entries)
    aircraft: dict[str, _Aircraft] = {}
    components: dict[str, _Component] = {}
    for e in live:
        p = e.details
        if e.kind in AIRCRAFT_KINDS:
            a = aircraft.setdefault(e.subject, _Aircraft())
            a.entries += 1
            a.synthetic = a.synthetic and e.synthetic
            if e.kind == "time_in_service.set":
                a.before_s = float(p["before_s"])
            elif e.kind == "inspection.done":
                a.inspections.append(
                    InspectionDone(
                        str(p["name"]),
                        e.occurred_utc,
                        float(p["at_hours_s"]),
                        float(p.get("carried_over_s", 0.0)),
                    )
                )
            elif e.kind == "work_order.open":
                a.orders[str(p["work_id"])] = WorkOrderState(
                    str(p["work_id"]),
                    e.subject,
                    e.occurred_utc,
                    cast(WorkState, str(p["state"])),
                    e.statement,
                    None,
                    e.synthetic,
                )
            elif e.kind == "work_order.state":
                w = a.orders.get(str(p["work_id"]))
                if w is not None:
                    a.orders[w.work_id] = _with(w, state=cast(WorkState, str(p["state"])))
            elif e.kind == "work_order.close":
                w = a.orders.get(str(p["work_id"]))
                if w is not None:
                    a.orders[w.work_id] = _with(w, closed_utc=e.occurred_utc)
        elif e.kind == "component.register":
            components[e.subject] = _Component(e)
        elif e.kind in ("component.install", "component.remove"):
            c = components.get(e.subject)
            if c is None:
                continue
            key = str(p["aircraft_key"])
            if e.kind == "component.install":
                c.installations.append(Installation(key, e.occurred_utc, None))
            else:
                for n, inst in enumerate(c.installations):
                    if inst.aircraft_key == key and inst.to_utc is None:
                        c.installations[n] = Installation(key, inst.from_utc, e.occurred_utc)
                        break
    maintenance: dict[str, MaintenanceRecord] = {}
    work_orders: dict[str, tuple[WorkOrderState, ...]] = {}
    for key, a in aircraft.items():
        if a.entries == 0:
            continue
        orders = tuple(a.orders.values())
        work_orders[key] = orders
        maintenance[key] = MaintenanceRecord(
            aircraft_key=key,
            time_in_service_before_s=a.before_s,
            inspections=tuple(a.inspections),
            work_orders=tuple(
                WorkOrder(w.opened_utc, w.description, w.state, w.synthetic)
                for w in orders
                if w.closed_utc is None
            ),
            synthetic=a.synthetic,
        )
    parts = tuple(
        Component(
            id=cid,
            kind=c.kind,
            in_service_since=c.in_service_since,
            hours_s_before=c.hours_s_before,
            cycles_before=c.cycles_before,
            installations=tuple(c.installations),
            synthetic=c.synthetic,
        )
        for cid, c in sorted(components.items())
    )
    return Projection(
        live=tuple(live),
        superseded_by=superseded_by,
        maintenance=maintenance,
        components=parts,
        work_orders=work_orders,
        registered_at={cid: c.registered_at for cid, c in components.items()},
    )


def _with(
    w: WorkOrderState, *, state: WorkState | None = None, closed_utc: datetime | None = None
) -> WorkOrderState:
    return WorkOrderState(
        w.work_id,
        w.aircraft_key,
        w.opened_utc,
        state or w.state,
        w.description,
        closed_utc or w.closed_utc,
        w.synthetic,
    )
