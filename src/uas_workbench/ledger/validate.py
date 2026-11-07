"""Refuse impossible entries before anything is written, with a status and a sentence.

Every entry is judged against the records as they stood when it happened: the fold of the
live entries that precede it, with liveness decided over the whole ledger first, as in
every read. So a back-dated entry lands among the windows and work orders of its own
date, whatever order it arrives in. Then every already recorded later entry of the same
subject is judged again with the new one in place, and the write is refused if one of them
would no longer hold: the ledger never holds an entry its own records cannot accept. A
correction keeps the date of the entry it corrects and takes its place; to move a date,
the entry is retracted and a new one written. Undoing a retraction or a correction (at any
depth of a chain of them) brings back the entry it had ended: that entry is judged as a new
entry of its date would be, and so is every entry after it (issue #161). A component that
has reached any of its life limits when it is fitted, exactly or past it, goes on no
aircraft: a life-limited
part at its limit is replaced, and the usage it has reached travels with it (14 CFR 43.10).
A calendar life is reached when its last UTC day ends; on that day the part may still be fitted.
An entry's time is converted to UTC before it is judged and stored (issue #149).
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
from uas_workbench.life.engine import (
    HOURS_SLACK_S,
    component_hours,
    counting_unknown,
    end_of_month_after,
    most_time_in_service,
    time_in_service,
)
from uas_workbench.life.model import Component, in_utc
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
    counted as the board counts it, a log from its end, and a log whose flight time is not
    known at its whole span, the most it can have flown (issue #152): a part that may have
    reached its limit is not fitted, and the sentence says the figure is that most."""
    usage, bound = component_hours(c, store.flights, tolerance_s, until=at)
    flown, tail = "it has flown", ""
    if bound is not None:
        flown = "it may have flown"
        tail = (
            f", {counting_unknown(len(bound.logs), bound.seconds)}: at least "
            f"{bound.least_s / H:.1f} h flown"
        )
    past: list[str] = []
    for basis, limit in rule.limits():
        if basis == "hours":
            used_h = round(usage.hours_s / H, 3)
            if used_h > limit:
                past.append(f"{flown} {used_h:.1f} h, past its {limit:.0f} h life limit{tail}")
            elif used_h == limit:
                past.append(
                    f"{flown} {used_h:.1f} h, the whole of its {limit:.0f} h life limit{tail}"
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
            if in_utc(at).date() > due:
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
    refusal and to keep work order ids unique over the whole history, a default id as much
    as a typed one; None when a live entry is judged again after a write, whose id is
    unique already. An entry the write brings back is given one, as a new entry is.
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
            wid = details["work_id"] = f"WO-{store.next_entry_id()}"
            # The default id is checked as a typed one is: an order opened by hand as WO-12
            # must not be replaced by the one entry 12 opens without an id.
            if whole is not None and any(
                w.work_id == wid for w in whole.work_orders.get(entry.subject, ())
            ):
                raise LedgerError(
                    409,
                    f"work order {wid} already exists on {entry.subject}, so this work order "
                    "cannot take it as its default id; give it a work id of its own",
                )
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
        # Stated hours are judged against every entry of the hours before the first log that
        # counts an interval from this inspection, by _inspection_hours_hold (issue #151).
        if details.get("at_hours_s") is None:
            record = then.maintenance.get(entry.subject)
            before = record.time_in_service_before_s if record else 0.0
            # Derived hours are the least the records allow: the logs they place by then. A log
            # with no UTC start then falls inside the interval after the inspection, the most
            # it can add, whether it was uploaded before or after this entry (issue #151).
            least = time_in_service(
                entry.subject,
                before,
                store.flights(entry.subject),
                tolerance_s,
                until=at,
                undated=False,
            )
            if isinstance(least, Unknown):
                raise LedgerError(
                    422, f"at_hours_s cannot be derived: {least.reason}; state at_hours_s"
                )
            details["at_hours_s"] = round(least, 1)
            details["derived"] = True
    return details


@dataclasses.dataclass(frozen=True)
class _Change:
    """What a write changes in the live entries besides adding itself (issue #161).

    Superseding an entry ends it; superseding a retraction or a correction brings back the
    entry it had ended, and down a chain of retractions every level flips: undoing the undo
    ends it again. Liveness is decided over the whole ledger, so comparing the live entries
    before and after the write finds every entry that comes back or ends, at any depth.
    Retractions fold nothing and are left out, except from `returned`: a retraction that
    comes back still supersedes an entry, which another live entry may supersede too."""

    revived: tuple[Entry, ...]  # live after the write, dead before; in fold order
    ended: tuple[Entry, ...]  # live before the write, dead after
    old_ids: frozenset[int]  # the live entries before the write
    superseded_by: dict[int, Entry]  # dead entry id -> the entry that ended it, before the write
    returned: tuple[Entry, ...] = ()  # `revived` and the retractions brought back; fold order


def _change(existing: list[Entry], timeline: list[Entry], candidate: Entry) -> _Change:
    old_live, old_by = liveness(existing)
    new_live, _ = liveness(timeline)
    old_ids = frozenset(e.id for e in old_live if e.id is not None)
    new_ids = {e.id for e in new_live}
    by_id = {e.id: e for e in existing}
    returned = tuple(e for e in new_live if e is not candidate and e.id not in old_ids)
    return _Change(
        revived=tuple(e for e in returned if e.kind != RETRACTION),
        ended=tuple(e for e in old_live if e.id not in new_ids and e.kind != RETRACTION),
        old_ids=old_ids,
        superseded_by={dead: by_id[by] for dead, by in old_by.items()},
        returned=returned,
    )


def _brought_back(e: Entry, change: _Change) -> str:
    """The start of a refusal of an entry that the write would bring back, naming the entry
    that had ended it."""
    by = change.superseded_by[e.id or 0]
    ended = "retracted" if by.kind == RETRACTION else "corrected"
    return (
        f"this would bring back entry {e.id} ({e.kind} on {e.subject}, "
        f"{_stamp(e.occurred_utc)}), which entry {by.id} {ended}"
    )


def _twin(
    e: Entry, live: list[Entry], timeline: list[Entry], candidate: Entry, change: _Change
) -> str | None:
    """Why `e`, brought back live, would be one of two where the records allow one, naming
    the other and the way through; None when it is the only one. These are the checks a
    new entry faces in _subjects, _correction and _transitions that look at the whole
    ledger rather than the records at its date: one live registration of a part, one live
    entry superseding any entry, one live entry at each place in the fold, and one work
    order per id on an aircraft."""
    others = [x for x in live if x.id != e.id and x.subject == e.subject]
    if e.kind == "component.register":
        same = [x for x in others if x.kind == "component.register"]
        if same:
            return (
                f"component {e.subject} is already registered, by entry {same[0].id}; retract "
                f"entry {same[0].id} first"
            )
    if e.supersedes is not None:
        # A live entry is superseded once, so a chain of corrections and retractions is a
        # line. Two live entries superseding one would fold both: one part on two aircraft,
        # two closes for one, two figures for one entry of the hours before the first log.
        same = [x for x in others if x.supersedes == e.supersedes]
        if same:
            return (
                f"entry {e.supersedes} is already superseded by entry {same[0].id}; retract "
                f"entry {same[0].id} first"
            )
    if e.kind != RETRACTION:
        # A correction keeps the date of the entry it corrects and takes its place in the
        # fold, through a chain of them. Superseding a correction undoes it, whatever the
        # kind of the entry superseding it, so the entry it had corrected comes back: a
        # correction of a correction would leave the first entry and the new one live at one
        # place, folded in an order nothing decides, and neither judged against the other.
        key = fold_key(timeline)
        same = [x for x in others if x.kind != RETRACTION and key(x) == key(e)]
        if same and same[0] is candidate:
            by = change.superseded_by[e.id or 0].id
            return (
                f"superseding entry {by} undoes it, so it and this entry would both stand in "
                f"one place in the records; to replace entry {by}, retract it first, then "
                f"correct entry {e.id}"
            )
        if same:
            return (
                f"entry {same[0].id} stands in its place already; retract entry {same[0].id} first"
            )
    if e.kind == "work_order.open":
        wid = e.details.get("work_id")
        same = [
            x for x in others if x.kind == "work_order.open" and x.details.get("work_id") == wid
        ]
        if same:
            return (
                f"work order {wid} already exists on {e.subject}, opened by entry {same[0].id}; "
                f"retract entry {same[0].id} first"
            )
    return None


def _revived_entries_hold(
    store: Store,
    timeline: list[Entry],
    candidate: Entry,
    change: _Change,
    tolerance_s: float,
    policy: LifePolicy,
) -> None:
    """An entry the write brings back faces every check a new entry of its kind and date
    faces (issue #161): that it is the only live registration of its part, the only live
    entry superseding the entry it supersedes, the only live entry at its place in the fold
    and the only live work order of its id (_twin); that its subjects are still what its
    kind needs, an aircraft key or a component id, against the store as it is now
    (_subjects); that fleet.toml still names its kind or inspection (_policy); and the
    records as they stood when it happened (_transitions). The first that does not hold
    refuses the write and is named, with the entry that had ended it and the way through.
    What it cannot fail is left out: its shape and its date were checked when it was
    written and do not change, nor does the entry it supersedes (rows are never edited or
    deleted). A retraction that comes back faces _twin only: the entry it retracts must not
    be superseded by another live entry already. A new entry also may not supersede an
    entry that others depend on; what that protects is judged directly here, since every
    later entry is judged again with the write in place (_later_entries_still_hold)."""
    live, _ = liveness(timeline)
    for e in change.returned:
        why = _twin(e, live, timeline, candidate, change)
        if why is not None:
            raise LedgerError(409, f"{_brought_back(e, change)}: {why}")
        if e.kind == RETRACTION:
            continue
        # The rest of the ledger as a new entry in its place would see it: every live entry
        # but this one and the entry it supersedes, which would be live in its stead.
        whole = project(x for x in timeline if x.id not in (e.id, e.supersedes))
        try:
            _subjects(store, e, whole, dict(e.details))
        except LedgerError as exc:
            raise LedgerError(
                409,
                f"{_brought_back(e, change)}: {exc.detail}; correct the records it conflicts "
                "with first, or record a new entry instead",
            ) from exc
        try:
            _policy(e, e.details, policy)
        except LedgerError as exc:
            raise LedgerError(
                409, f"{_brought_back(e, change)}: {exc.detail}; name it in fleet.toml first"
            ) from exc
        try:
            _transitions(
                store, e, dict(e.details), project(timeline, before=e), whole, tolerance_s, policy
            )
        except LedgerError as exc:
            raise LedgerError(
                409,
                f"{_brought_back(e, change)}: {exc.detail}; correct the records it conflicts "
                "with first, or record a new entry instead",
            ) from exc


def _later_entries_still_hold(
    store: Store,
    timeline: list[Entry],
    candidate: Entry,
    cut: Entry,
    tolerance_s: float,
    policy: LifePolicy,
) -> None:
    """Judge again every live entry of the subject that follows `cut` in the fold, with the
    candidate in place; the first that no longer holds refuses the write and is named. `cut`
    is the earliest entry the write adds, ends or brings back (issue #161)."""
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


HOURS_KINDS = ("time_in_service.set", "inspection.done")


def _counting(live: list[Entry], order: Any, e: Entry) -> list[Entry | None]:
    """The entries of the hours before the first log that a dated answer counts the interval
    from inspection `e` with: the one in force at its date (None when none is: 0 h), and
    every one after it, which governs every date after its own."""
    totals = [s for s in live if s.subject == e.subject and s.kind == "time_in_service.set"]
    earlier = [s for s in totals if order(s) < order(e)]
    return [
        max(earlier, key=order) if earlier else None,
        *(s for s in totals if order(s) > order(e)),
    ]


def _inspection_hours_hold(
    store: Store,
    existing: list[Entry],
    timeline: list[Entry],
    candidate: Entry,
    corrected: Entry | None,
    change: _Change,
    tolerance_s: float,
) -> None:
    """An inspection's hours are at most the time in service the aircraft can have had at its
    date, counting each entry of the hours before the first log that a dated answer counts
    its interval with (_counting); before the first log, at most those hours plus the logs
    with no UTC start (most_time_in_service). Judged when an inspection or such an entry is
    written, corrected or retracted, in either order (issue #151), and when a write brings
    one back or ends one by undoing a retraction or a correction, at any depth (issue #161).
    A store written by 0.6.6 or earlier can already hold an inspection past that bound: a
    write is refused only when it puts an inspection further past it than the records
    already did, so an entry that removes the disagreement or leaves it as it was is
    accepted. The refusal names the entries, in one sentence, and an entry that can still be
    corrected."""
    hours = [e for e in (candidate, *change.revived, *change.ended) if e.kind in HOURS_KINDS]
    if not hours:
        return
    # Only the inspections the write makes live need judging, unless an entry of the hours
    # before the first log changes, which moves the bound of every inspection.
    totals = any(e.kind == "time_in_service.set" for e in hours)
    newly = {candidate.id, *(e.id for e in change.revived)}
    flights = store.flights(candidate.subject)
    new_live, _ = liveness(timeline)
    old_live, _ = liveness(existing)
    new_order, old_order = fold_key(timeline), fold_key(existing)

    def over(e: Entry, s: Entry | None) -> tuple[float, float, float, bool]:
        before = float(s.details["before_s"]) if s is not None else 0.0
        until = e.occurred_utc
        most = most_time_in_service(e.subject, before, flights, tolerance_s, until=until)
        known = is_known(time_in_service(e.subject, before, flights, tolerance_s, until=until))
        return float(e.details["at_hours_s"]) - most, most, before, known

    def worst(live: list[Entry], order: Any, e: Entry) -> tuple[Entry | None, float]:
        excess = [(s, over(e, s)[0]) for s in _counting(live, order, e)]
        return max(excess, key=lambda p: p[1])

    for e in new_live:
        if e.subject != candidate.subject or e.kind != "inspection.done":
            continue
        if not totals and e.id not in newly:
            continue
        s, excess = worst(new_live, new_order, e)
        if excess <= HOURS_SLACK_S:
            continue
        # The same inspection as the records held it before this write: itself, or the one
        # the candidate corrects; none when it is new or comes back live with this write.
        was = e.id if e is not candidate else (corrected.id if corrected is not None else None)
        held = [x for x in old_live if x.id == was and x.kind == "inspection.done"]
        if held and excess <= max(0.0, worst(old_live, old_order, held[0])[1]) + HOURS_SLACK_S:
            continue
        _, most, before, known = over(e, s)
        at, stamp = float(e.details["at_hours_s"]), _stamp(e.occurred_utc)
        if known:
            why = (
                f"at_hours_s {at / H:.1f} h is more than the time in service of {e.subject} at "
                f"{stamp}, {most / H:.1f} h"
            )
        else:
            first = min(f.utc_start for f in flights if is_known(f.utc_start))
            why = (
                f"at_hours_s {at / H:.1f} h is more than the {most / H:.1f} h time in service "
                f"{e.subject} can have reached by {stamp}, before its first log on "
                f"{first:%Y-%m-%d}"
            )
        counted_by = (
            f", counting the {before / H:.1f} h before the first log that entry {s.id} records"
            if s is not None
            else ""
        )
        if e is not candidate and e.id not in change.old_ids:
            # Brought back by this write: it is dead now, so it cannot be retracted or
            # corrected; the way through is the entry it is counted with, or a new record.
            routes: list[str] = []
            if s is None:
                routes.append("enter the hours before the first log first")
            elif s.id in change.old_ids:
                routes.append(f"correct entry {s.id} first")
            routes.append(f"record the inspection again stating at most {most / H:.1f} h")
            raise LedgerError(
                409, f"{_brought_back(e, change)}: {why}{counted_by}; {', or '.join(routes)}"
            )
        if e is not candidate:
            raise LedgerError(
                409,
                f"this would make entry {e.id} impossible ({e.kind} on {e.subject}, {stamp}): "
                f"{why}; retract or correct entry {e.id} first",
            )
        if s is None or new_order(s) < new_order(e):
            raise LedgerError(422, why if known else f"{why}{counted_by}")
        raise LedgerError(
            409, f"{why}{counted_by}; state fewer hours or correct entry {s.id} first"
        )


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
    # One instant, one date: the entry is judged and stored in UTC, whatever offset it was
    # given with (issue #149).
    entry = dataclasses.replace(entry, occurred_utc=in_utc(entry.occurred_utc))
    current = store.projection()
    _subjects(store, entry, current, details)
    corrected = _correction(store, entry, current)
    _policy(entry, details, policy)
    existing = list(store.entries())
    # The candidate takes the id the store will give it, so it sorts after every entry at
    # its instant, and a correction takes the place of the entry it corrects.
    candidate = dataclasses.replace(entry, id=store.next_entry_id(), details=details)
    timeline = [*existing, candidate]
    others = project([e for e in existing if corrected is None or e.id != corrected.id])
    details = _transitions(
        store, candidate, details, project(timeline, before=candidate), others, tolerance_s, policy
    )
    candidate = dataclasses.replace(candidate, details=details)
    timeline[-1] = candidate
    # Superseding a retraction or a correction brings back what it had ended, and through a
    # chain of them may end or bring back entries further down: each is judged (issue #161).
    change = _change(existing, timeline, candidate)
    _revived_entries_hold(store, timeline, candidate, change, tolerance_s, policy)
    _inspection_hours_hold(store, existing, timeline, candidate, corrected, change, tolerance_s)
    cut = min((corrected or candidate, *change.revived, *change.ended), key=fold_key(timeline))
    _later_entries_still_hold(store, timeline, candidate, cut, tolerance_s, policy)
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
    """Validate, then write. Nothing is written when validation refuses. The check and the
    write hold the store's write lock together, and on a store file SQLite's write
    transaction too, so two appends made at once, through one store or from two processes,
    are checked as if made one after the other: two corrections of one entry cannot both be
    accepted."""
    with store.writing():
        return store.append_entry(
            validate(store, entry, policy=policy, now=now, tolerance_s=tolerance_s)
        )
