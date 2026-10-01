"""Refuse impossible entries before anything is written, with a status and a sentence.

Every entry is judged against the records as they stood when it happened: the fold of the
live entries that precede it, with liveness decided over the whole ledger first, as in
every read. So a back-dated entry lands among the windows and work orders of its own
date, whatever order it arrives in. Then every already recorded later entry of the same
subject is judged again with the new one in place, and the write is refused if one of them
would no longer hold: the ledger never holds an entry its own records cannot accept. A
correction keeps the date of the entry it corrects and takes its place; to move a date,
the entry is retracted and a new one written. A component that has reached any of its life
limits when it is fitted, exactly or past it, goes on no aircraft: a life-limited
part at its limit is replaced, and the usage it has reached travels with it (14 CFR 43.10).
A calendar life is reached when its last day ends; on that day the part may still be fitted.
Shape errors are 422, unknown
subjects 404, the public showcase aircraft 403, and transitions the records cannot accept
409.
"""

from __future__ import annotations

import dataclasses
import re
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from uas_workbench.flight.record import Unknown, is_known
from uas_workbench.life.engine import component_usage, end_of_month_after, time_in_service
from uas_workbench.life.model import Component
from uas_workbench.life.policy import LifePolicy, LifeRule

from .model import AIRCRAFT_KINDS, COMPONENT_KINDS, KINDS, RETRACTION, Entry, LedgerError
from .project import fold_key, liveness, project

if TYPE_CHECKING:
    from uas_workbench.service.store import Store

CLOCK_SKEW = timedelta(minutes=5)
SUBJECT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
H = 3600.0
# 14 CFR 43.10(c): the control method "must deter the installation of the part after it has
# reached its life limit". Reached, not only exceeded: a part with nothing left is refused.
NOT_FITTED_AGAIN = (
    "a life-limited part that has reached its life limit is replaced, not fitted again"
)
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


def _policy(entry: Entry, details: dict[str, Any], policy: LifePolicy) -> None:
    if entry.kind == "component.register" and details["kind"] not in policy.component_kinds:
        raise LedgerError(
            422,
            f"component kind {details['kind']!r} is not in fleet.toml; the kinds are "
            f"{', '.join(policy.component_kinds)}",
        )
    if entry.kind == "inspection.done" and details["name"] not in policy.inspections:
        raise LedgerError(
            422,
            f"inspection {details['name']!r} is not in fleet.toml; the inspections are "
            f"{', '.join(policy.inspections)}",
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


def _correction(store: Store, entry: Entry, projection: Any) -> Entry | None:
    """The live entry this one supersedes, or None when it supersedes nothing."""
    if entry.supersedes is None:
        return None
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
    if entry.kind != RETRACTION and entry.occurred_utc != target.occurred_utc:
        raise LedgerError(
            409,
            f"a correction keeps the date of the entry it corrects: entry {target.id} happened "
            f"on {_stamp(target.occurred_utc)} and this one says {_stamp(entry.occurred_utc)}; "
            f"to move the date, retract entry {target.id} and record a new entry",
        )
    dependents = _dependents(target, projection.live)
    if dependents:
        raise LedgerError(
            409,
            f"entry {entry.supersedes} cannot be superseded while entries "
            f"{', '.join(str(d) for d in dependents)} depend on it; correct those first",
        )
    return target


def _limits_past(
    store: Store, c: Component, rule: LifeRule, at: datetime, tolerance_s: float
) -> list[str]:
    """Each life limit of `c` that is reached at `at`, past or exactly, in the words of the
    refusal; empty when none is. A limit is reached when nothing of it remains: 300 of 300
    cycles, 300.0 of 300 h, the end of the last day of the calendar life. On that last day
    something remains, so the part is within its life and may be fitted. This is the board's
    boundary too (life.engine): what grounds an aircraft when fitted is what cannot be fitted,
    and a part with nothing left has no flight in it to give (14 CFR 43.10(c)). The usage is
    counted as the board counts it, a log from its end."""
    usage = component_usage(c, store.flights, tolerance_s, until=at)
    past: list[str] = []
    for basis, limit in rule.limits():
        if basis == "hours":
            used_h = round(usage.hours_s / H, 3)
            if used_h > limit:
                past.append(f"it has flown {used_h:.1f} h, past its {limit:.0f} h life limit")
            elif used_h == limit:
                past.append(
                    f"it has flown {used_h:.1f} h, the whole of its {limit:.0f} h life limit"
                )
        elif basis == "cycles":
            if usage.cycles > limit:
                past.append(
                    f"it has flown {usage.cycles} cycles, past its {limit:.0f}-cycle life limit"
                )
            elif usage.cycles == limit:
                past.append(
                    f"it has flown {usage.cycles} cycles, the whole of its {limit:.0f}-cycle "
                    "life limit"
                )
        else:
            due = end_of_month_after(c.in_service_since, int(limit))
            if at.date() > due:
                past.append(f"its {limit:.0f}-calendar-month life limit ended on {due}")
    return past


def _transitions(
    store: Store,
    entry: Entry,
    details: dict[str, Any],
    then: Any,
    whole: Any,
    tolerance_s: float,
    policy: LifePolicy,
) -> dict[str, Any]:
    """Judge `entry` against `then`, the records as they stood when it happened.

    `whole` is the projection of every other entry, used to name a later boundary in a
    refusal and to keep work order ids unique over the whole history; None when an already
    recorded entry is judged again, whose id is unique already.
    """
    at = entry.occurred_utc
    if entry.kind == "work_order.open":
        wid = details.get("work_id")
        if (
            wid
            and whole is not None
            and any(w.work_id == wid for w in whole.work_orders.get(entry.subject, ()))
        ):
            raise LedgerError(409, f"work order {wid} already exists on {entry.subject}")
        if not wid:
            details["work_id"] = f"WO-{store.next_entry_id()}"
    elif entry.kind in ("work_order.state", "work_order.close"):
        wid = str(details["work_id"])
        orders = [w for w in then.work_orders.get(entry.subject, ()) if w.work_id == wid]
        if not orders:
            later = (
                [w for w in whole.work_orders.get(entry.subject, ()) if w.work_id == wid]
                if whole is not None
                else []
            )
            if later:
                raise LedgerError(
                    409,
                    f"work order {wid} was opened on {_stamp(later[0].opened_utc)}, "
                    f"after {_stamp(at)}",
                )
            raise LedgerError(409, f"no work order {wid} is open on {entry.subject}")
        w = orders[0]
        if w.closed_utc is not None:
            raise LedgerError(
                409, f"work order {wid} on {entry.subject} was closed on {_stamp(w.closed_utc)}"
            )
    elif entry.kind in ("component.install", "component.remove"):
        found = [c for c in then.components if c.id == entry.subject]
        if not found:
            # The 0.1.0 sentence, word for word; tests/test_ledger_refusals.py holds it.
            registered = whole.registered_at.get(entry.subject) if whole is not None else None
            why = (
                f"it was registered on {_stamp(registered)}"
                if registered
                else "it was not registered by then"
            )
            raise LedgerError(
                409, f"{entry.subject} cannot be on an aircraft at {_stamp(at)}: {why}"
            )
        (c,) = found
        key = str(details["aircraft_key"])
        on = [i for i in c.installations if i.to_utc is None]
        if entry.kind == "component.install":
            if on:
                since = _stamp(on[0].from_utc)
                raise LedgerError(409, f"{c.id} is installed on {on[0].aircraft_key} since {since}")
            # A part that has reached a life limit when it is fitted goes on no aircraft
            # (14 CFR 43.10(c)); the usage is what the records held by then, on every
            # airframe it had been fitted to.
            rule = policy.component_kinds.get(c.kind)
            past = _limits_past(store, c, rule, at, tolerance_s) if rule else []
            if past:
                raise LedgerError(
                    409,
                    f"{c.id} cannot be fitted to {key} at {_stamp(at)}: {' and '.join(past)}; "
                    f"{NOT_FITTED_AGAIN}",
                )
        elif not on or on[0].aircraft_key != key:
            # The 0.1.0 sentence, word for word; tests/test_ledger_refusals.py holds it.
            where = f"installed on {on[0].aircraft_key}" if on else "not installed on any aircraft"
            raise LedgerError(409, f"{c.id} is {where} at {_stamp(at)}, not installed on {key}")
    elif entry.kind == "inspection.done":
        record = then.maintenance.get(entry.subject)
        before = record.time_in_service_before_s if record else 0.0
        tis = time_in_service(
            entry.subject, before, store.flights(entry.subject), tolerance_s, until=at
        )
        if details.get("at_hours_s") is None:
            if isinstance(tis, Unknown):
                raise LedgerError(
                    422, f"at_hours_s cannot be derived: {tis.reason}; state at_hours_s"
                )
            details["at_hours_s"] = round(tis, 1)
            details["derived"] = True
        elif is_known(tis) and details["at_hours_s"] > tis + 1.0:
            raise LedgerError(
                422,
                f"at_hours_s {details['at_hours_s'] / 3600:.1f} h is more than the time in "
                f"service of {entry.subject} at {_stamp(at)}, {tis / 3600:.1f} h",
            )
    return details


def _later_entries_still_hold(
    store: Store,
    timeline: list[Entry],
    candidate: Entry,
    cut: Entry,
    tolerance_s: float,
    policy: LifePolicy,
) -> None:
    """Judge again every live entry of the subject that follows `cut` in the fold, with the
    candidate in place; the first that no longer holds refuses the write and is named."""
    live, _ = liveness(timeline)
    key = fold_key(timeline)
    for e in live:
        if e is candidate or e.subject != candidate.subject or e.kind == RETRACTION:
            continue
        if key(e) <= key(cut):
            continue
        try:
            _transitions(
                store, e, dict(e.details), project(timeline, before=e), None, tolerance_s, policy
            )
        except LedgerError as exc:
            raise LedgerError(
                409,
                f"this would make entry {e.id} impossible ({e.kind} on {e.subject}, "
                f"{_stamp(e.occurred_utc)}): {exc.detail}; retract or correct entry {e.id} first",
            ) from exc


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
    current = store.projection()
    _subjects(store, entry, current, details)
    target = _correction(store, entry, current)
    _policy(entry, details, policy)
    existing = list(store.entries())
    # The candidate takes the id the store will give it, so it sorts after every entry at
    # its instant, and a correction takes the place of the entry it corrects.
    candidate = dataclasses.replace(entry, id=store.next_entry_id(), details=details)
    timeline = [*existing, candidate]
    others = project([e for e in existing if target is None or e.id != target.id])
    details = _transitions(
        store, candidate, details, project(timeline, before=candidate), others, tolerance_s, policy
    )
    candidate = dataclasses.replace(candidate, details=details)
    timeline[-1] = candidate
    _later_entries_still_hold(store, timeline, candidate, target or candidate, tolerance_s, policy)
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
