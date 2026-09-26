"""Refuse impossible entries before anything is written, with a status and a sentence.

The checks run against the projection of what is already recorded, so a back-dated entry
is judged by the windows and work orders it would land among, whatever order it arrives
in. Shape errors are 422, unknown subjects 404, the public showcase aircraft 403, and
transitions the projection cannot accept 409.
"""

from __future__ import annotations

import dataclasses
import re
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from uas_workbench.life.engine import time_in_service
from uas_workbench.life.policy import LifePolicy

from .model import AIRCRAFT_KINDS, COMPONENT_KINDS, KINDS, RETRACTION, Entry, LedgerError
from .project import project

if TYPE_CHECKING:
    from uas_workbench.service.store import Store

CLOCK_SKEW = timedelta(minutes=5)
SUBJECT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
WorkStateIn = Literal["deferred", "in_work", "awaiting_parts"]


class _Details(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WorkOpen(_Details):
    work_id: str | None = Field(default=None, min_length=1, max_length=64)
    state: WorkStateIn


class WorkStateChange(_Details):
    work_id: str = Field(min_length=1, max_length=64)
    state: WorkStateIn


class WorkClose(_Details):
    work_id: str = Field(min_length=1, max_length=64)


class ComponentRegister(_Details):
    kind: str = Field(min_length=1)
    in_service_since: date
    hours_s_before: float = Field(ge=0)
    cycles_before: int = Field(ge=0)


class ComponentMove(_Details):
    aircraft_key: str = Field(min_length=1)


class InspectionDoneIn(_Details):
    name: str = Field(min_length=1)
    at_hours_s: float | None = Field(default=None, ge=0)
    carried_over_s: float = Field(default=0.0, ge=0)
    derived: bool = False


class TimeInService(_Details):
    before_s: float = Field(ge=0)


class Empty(_Details):
    pass


DETAILS: dict[str, type[_Details]] = {
    "work_order.open": WorkOpen,
    "work_order.state": WorkStateChange,
    "work_order.close": WorkClose,
    "component.register": ComponentRegister,
    "component.install": ComponentMove,
    "component.remove": ComponentMove,
    "inspection.done": InspectionDoneIn,
    "time_in_service.set": TimeInService,
    RETRACTION: Empty,
}


def _showcase_keys() -> set[str]:
    from uas_workbench.fleet.showcase import ALFA_KEY, PX4_KEY  # avoids an import cycle

    return {ALFA_KEY, PX4_KEY}


def _stamp(t: datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def _shape(entry: Entry, now: datetime) -> dict[str, Any]:
    if entry.kind not in KINDS:
        raise LedgerError(422, f"unknown kind {entry.kind!r}; the kinds are {', '.join(KINDS)}")
    if not SUBJECT.match(entry.subject):
        raise LedgerError(422, "subject must be an aircraft key or a component id")
    if not entry.entered_by.strip():
        raise LedgerError(422, "entered_by must name the person recording the entry")
    if entry.kind != RETRACTION and not entry.statement.strip():
        raise LedgerError(422, "statement must describe what was done, in the person's words")
    if entry.occurred_utc.tzinfo is None:
        raise LedgerError(422, "occurred_utc must carry a time zone")
    if entry.occurred_utc > now + CLOCK_SKEW:
        raise LedgerError(422, f"occurred_utc {_stamp(entry.occurred_utc)} is in the future")
    if entry.supersedes is not None and not (entry.reason or "").strip():
        raise LedgerError(422, "a correction needs a reason for superseding the earlier entry")
    if entry.kind == RETRACTION and entry.supersedes is None:
        raise LedgerError(422, "a retraction must name the entry it supersedes")
    try:
        model = DETAILS[entry.kind].model_validate(entry.details)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or 'details'}: {err['msg']}"
            for err in exc.errors()
        )
        raise LedgerError(422, f"invalid details for {entry.kind}: {problems}") from exc
    return model.model_dump(mode="json")


def _subjects(store: Store, entry: Entry, projection: Any, details: dict[str, Any]) -> None:
    aircraft_keys = {a.key for a in store.aircraft()}
    component_ids = {c.id for c in projection.components}
    showcase = _showcase_keys()
    if entry.kind in AIRCRAFT_KINDS:
        if entry.subject in component_ids:
            raise LedgerError(
                422, f"{entry.subject} is a component; {entry.kind} needs an aircraft"
            )
        if entry.subject not in aircraft_keys:
            raise LedgerError(404, f"aircraft {entry.subject} is not in the store")
        if entry.subject in showcase:
            raise LedgerError(
                403,
                f"{entry.subject} is a public research or validation aircraft; no maintenance "
                "record may be entered for it",
            )
    elif entry.kind in COMPONENT_KINDS:
        if entry.subject in aircraft_keys:
            raise LedgerError(
                422, f"{entry.subject} is an aircraft; {entry.kind} needs a component id"
            )
        if entry.kind == "component.register":
            if entry.subject in component_ids:
                raise LedgerError(409, f"component {entry.subject} is already registered")
        elif entry.subject not in component_ids:
            raise LedgerError(404, f"component {entry.subject} is not registered")
        if entry.kind in ("component.install", "component.remove"):
            key = str(details["aircraft_key"])
            if key not in aircraft_keys:
                raise LedgerError(404, f"aircraft {key} is not in the store")
            if key in showcase:
                raise LedgerError(
                    403,
                    f"{key} is a public research or validation aircraft; no component may be "
                    "recorded on it",
                )


def _dependents(target: Entry, live: tuple[Entry, ...]) -> list[int]:
    same = [e for e in live if e.subject == target.subject and e.id != target.id]
    if target.kind == "component.register":
        return [e.id for e in same if e.kind in ("component.install", "component.remove") and e.id]
    if target.kind == "work_order.open":
        wid = target.details.get("work_id")
        return [
            e.id
            for e in same
            if e.kind in ("work_order.state", "work_order.close")
            and e.details.get("work_id") == wid
            and e.id
        ]
    if target.kind == "component.install":
        later = [
            e
            for e in same
            if e.kind == "component.remove"
            and e.occurred_utc >= target.occurred_utc
            and e.details.get("aircraft_key") == target.details.get("aircraft_key")
        ]
        return [later[0].id] if later and later[0].id else []
    return []


def _correction(store: Store, entry: Entry, projection: Any) -> None:
    if entry.supersedes is None:
        return
    target = store.entry(entry.supersedes)
    if target is None:
        raise LedgerError(404, f"entry {entry.supersedes} does not exist")
    if target.subject != entry.subject:
        raise LedgerError(
            409, f"entry {entry.supersedes} is about {target.subject}, not {entry.subject}"
        )
    if target.id in projection.superseded_by:
        raise LedgerError(
            409,
            f"entry {entry.supersedes} is already superseded by entry "
            f"{projection.superseded_by[target.id]}",
        )
    dependents = _dependents(target, projection.live)
    if dependents:
        raise LedgerError(
            409,
            f"entry {entry.supersedes} cannot be superseded while entries "
            f"{', '.join(str(d) for d in dependents)} depend on it; correct those first",
        )


def _transitions(
    store: Store,
    entry: Entry,
    details: dict[str, Any],
    projection: Any,
    policy: LifePolicy,
    tolerance_s: float,
) -> dict[str, Any]:
    at = entry.occurred_utc
    if entry.kind == "work_order.open":
        wid = details.get("work_id")
        if wid and any(w.work_id == wid for w in projection.work_orders.get(entry.subject, ())):
            raise LedgerError(409, f"work order {wid} already exists on {entry.subject}")
        if not wid:
            details["work_id"] = f"WO-{store.next_entry_id()}"
    elif entry.kind in ("work_order.state", "work_order.close"):
        wid = str(details["work_id"])
        orders = [w for w in projection.work_orders.get(entry.subject, ()) if w.work_id == wid]
        if not orders:
            raise LedgerError(409, f"no work order {wid} is open on {entry.subject}")
        w = orders[0]
        if w.opened_utc > at:
            raise LedgerError(
                409, f"work order {wid} was opened on {_stamp(w.opened_utc)}, after {_stamp(at)}"
            )
        if w.closed_utc is not None:
            raise LedgerError(
                409, f"work order {wid} on {entry.subject} was closed on {_stamp(w.closed_utc)}"
            )
    elif entry.kind == "component.register":
        if details["kind"] not in policy.component_kinds:
            raise LedgerError(
                422,
                f"component kind {details['kind']!r} is not in fleet.toml; the kinds are "
                f"{', '.join(policy.component_kinds)}",
            )
    elif entry.kind in ("component.install", "component.remove"):
        (c,) = [c for c in projection.components if c.id == entry.subject]
        key = str(details["aircraft_key"])
        if at < projection.registered_at[c.id]:
            raise LedgerError(
                409,
                f"{c.id} cannot be on an aircraft at {_stamp(at)}: it was registered on "
                f"{_stamp(projection.registered_at[c.id])}",
            )
        on = [
            i for i in c.installations if i.from_utc <= at and (i.to_utc is None or at < i.to_utc)
        ]
        if entry.kind == "component.install":
            if on:
                since = _stamp(on[0].from_utc)
                raise LedgerError(409, f"{c.id} is installed on {on[0].aircraft_key} since {since}")
            nxt = [i for i in c.installations if i.from_utc > at]
            if nxt:
                raise LedgerError(
                    409,
                    f"{c.id} was installed on {nxt[0].aircraft_key} on {_stamp(nxt[0].from_utc)}; "
                    "remove it there first or correct that entry",
                )
        elif not on or on[0].aircraft_key != key:
            where = f"installed on {on[0].aircraft_key}" if on else "not installed on any aircraft"
            raise LedgerError(409, f"{c.id} is {where} at {_stamp(at)}, not installed on {key}")
    elif entry.kind == "inspection.done":
        if details["name"] not in policy.inspections:
            raise LedgerError(
                422,
                f"inspection {details['name']!r} is not in fleet.toml; the inspections are "
                f"{', '.join(policy.inspections)}",
            )
        record = projection.maintenance.get(entry.subject)
        before = record.time_in_service_before_s if record else 0.0
        tis = time_in_service(
            entry.subject, before, store.flights(entry.subject), tolerance_s, until=at
        )
        if details.get("at_hours_s") is None:
            details["at_hours_s"] = round(tis, 1)
            details["derived"] = True
        elif details["at_hours_s"] > tis + 1.0:
            raise LedgerError(
                422,
                f"at_hours_s {details['at_hours_s'] / 3600:.1f} h is more than the time in "
                f"service of {entry.subject} at {_stamp(at)}, {tis / 3600:.1f} h",
            )
    return details


def validate(
    store: Store,
    entry: Entry,
    *,
    policy: LifePolicy,
    now: datetime,
    tolerance_s: float = 30.0,
) -> Entry:
    """The entry as it will be stored, or LedgerError."""
    details = _shape(entry, now)
    projection = store.projection()
    _subjects(store, entry, projection, details)
    _correction(store, entry, projection)
    if entry.supersedes is not None:
        # Judge the transition as if the superseded entry were already gone.
        rest = [e for e in store.entries() if e.id != entry.supersedes]
        projection = project(rest)
    details = _transitions(store, entry, details, projection, policy, tolerance_s)
    statement = entry.statement.strip() or (entry.reason or "").strip()
    return dataclasses.replace(entry, details=details, statement=statement)


def append(
    store: Store,
    entry: Entry,
    *,
    policy: LifePolicy,
    now: datetime,
    tolerance_s: float = 30.0,
) -> Entry:
    """Validate, then write. Nothing is written when validation refuses."""
    return store.append_entry(
        validate(store, entry, policy=policy, now=now, tolerance_s=tolerance_s)
    )
