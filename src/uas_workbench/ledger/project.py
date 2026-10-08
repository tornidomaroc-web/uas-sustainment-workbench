"""Fold the live entries into the records the life engine reads.

An entry is dead when a live newer entry supersedes it. Deciding from the newest entry
backwards makes that well defined: the newest entry is always live, and undoing a
correction (superseding the correction) revives the entry it had corrected.

The records at a time `as_of` are folded from the live entries that had occurred by then.
Liveness is decided over the whole ledger first, as known now, and the date filter comes
second: a retraction entered in September still governs a query about March, and a naive
filter that dropped the retraction would revive what it retracted. This answers "what was
true at `as_of`, as recorded today", not "what did the board show on that day"; the
ledger keeps `recorded_utc` for the second question, which nothing here asks yet.

Live entries fold in the order they occurred; at one instant, in the order they were
entered, except that a correction takes the place of the entry it corrects. The records
as they stood when one entry happened are the fold of the live entries before it in that
order (`before`); write validation judges every entry against those.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import date, datetime
from typing import Any, cast

from uas_workbench.life.model import (
    Component,
    InspectionDone,
    Installation,
    MaintenanceRecord,
    WorkOrder,
    WorkState,
)

from .model import AIRCRAFT_KINDS, Entry, Projection, UnreadableRecord, WorkOrderState

_REQUIRED = object()


def fold_key(entries: Iterable[Entry]) -> Callable[[Entry], tuple[datetime, int]]:
    """The order entries fold in: when they occurred, then the order they were entered, a
    correction taking the place of the entry it corrects (through a chain of corrections)."""
    by_id = {e.id: e for e in entries if e.id is not None}

    def key(e: Entry) -> tuple[datetime, int]:
        seen: set[int] = set()
        root = e
        while root.supersedes is not None and root.supersedes in by_id:
            if root.supersedes in seen:
                break
            seen.add(root.supersedes)
            root = by_id[root.supersedes]
        return (e.occurred_utc, root.id or 0)

    return key


def liveness(entries: Iterable[Entry]) -> tuple[list[Entry], dict[int, int]]:
    """Live entries in fold order, and dead entry id -> superseding entry id."""
    entries = list(entries)
    superseded_by: dict[int, int] = {}
    live: list[Entry] = []
    for e in sorted(entries, key=lambda e: e.id or 0, reverse=True):
        if e.id in superseded_by:
            continue
        live.append(e)
        if e.supersedes is not None and e.id is not None:
            superseded_by[e.supersedes] = e.id
    live.sort(key=fold_key(entries))
    return live, superseded_by


def _detail[T](
    p: dict[str, Any], name: str, convert: Callable[[Any], T], default: Any = _REQUIRED
) -> T:
    """A detail the fold reads, converted; a wrong type raises ValueError naming the detail
    and a missing one KeyError naming it, so the store can say which entry and which field."""
    value = p[name] if default is _REQUIRED else p.get(name, default)
    try:
        return convert(value)
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError(f"detail {name!r}: {exc}") from exc


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
        self.in_service_since = _detail(p, "in_service_since", lambda v: date.fromisoformat(str(v)))
        self.hours_s_before = _detail(p, "hours_s_before", float)
        self.cycles_before = _detail(p, "cycles_before", int)
        self.installations: list[Installation] = []
        self.synthetic = e.synthetic
        self.registered_at = e.occurred_utc


def _fold(e: Entry, aircraft: dict[str, _Aircraft], components: dict[str, _Component]) -> None:
    """Fold one live entry into the records; the details it reads are typed here."""
    p = e.details
    if e.kind in AIRCRAFT_KINDS:
        a = aircraft.setdefault(e.subject, _Aircraft())
        a.entries += 1
        a.synthetic = a.synthetic and e.synthetic
        if e.kind == "time_in_service.set":
            a.before_s = _detail(p, "before_s", float)
        elif e.kind == "inspection.done":
            a.inspections.append(
                InspectionDone(
                    str(p["name"]),
                    e.occurred_utc,
                    _detail(p, "at_hours_s", float),
                    _detail(p, "carried_over_s", float, default=0.0),
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
            return
        key = str(p["aircraft_key"])
        if e.kind == "component.install":
            c.installations.append(Installation(key, e.occurred_utc, None))
        else:
            for n, inst in enumerate(c.installations):
                if inst.aircraft_key == key and inst.to_utc is None:
                    c.installations[n] = Installation(key, inst.from_utc, e.occurred_utc)
                    break


def project(
    entries: Iterable[Entry], as_of: datetime | None = None, *, before: Entry | None = None
) -> Projection:
    """The records at `as_of` (every entry when None), with liveness over the whole ledger.

    With `before`, an entry of `entries`, only the live entries that fold before it count:
    the records as they stood when that entry happened."""
    entries = list(entries)
    live, superseded_by = liveness(entries)
    if as_of is not None:
        live = [e for e in live if e.occurred_utc <= as_of]
    if before is not None:
        order = fold_key(entries)
        cut = order(before)
        live = [e for e in live if order(e) < cut]
    aircraft: dict[str, _Aircraft] = {}
    components: dict[str, _Component] = {}
    for e in live:
        try:
            _fold(e, aircraft, components)
        except (KeyError, ValueError, TypeError, AttributeError) as exc:
            # A detail the fold reads is missing or of the wrong type: the entry was read
            # from the store but cannot be used, and nothing is projected without it.
            why = f"field {exc} is missing" if isinstance(exc, KeyError) else str(exc)
            raise UnreadableRecord(
                "entries",
                e.id or 0,
                e.subject,
                f"its details cannot be used as {e.kind}: {why}",
            ) from exc
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
        as_of=as_of,
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
