"""A short cue beside every sentence the engine emits, for boards that cannot carry a paragraph.

The sentence stays the record's word; the cue is at most CUE_LIMIT characters, built from the
same fields by one grammar, never typed per case. The aircraft key is dropped, because a board
row already names it; the regulation, the tolerance explanation and the counted days of a
calendar item are dropped; the part id or inspection name, the number, its unit and the
direction (left, reached, past, overdue, in tolerance, not known) are never dropped. A cue
therefore says exactly what the state says, never a stronger word for a weaker state or the
reverse, and a not-known item's cue says not known. A limit exactly reached reads "reached",
never "0 left" and never "past": the sentence says "has reached", and so does the cue.

  item_cue(item, as_of)       the cue of a due item
  reason_cues(due, board)     the cues of the board's reasons, one for one in the same order
  status_cue(status)          the board state in a few words; not known says why in short
  problems(cue, item, as_of)  what a cue gets wrong against its item: the checker the tests
                              and the mutation tests share

Grammar, with the longest form first and a shorter one when an id would push it past the
limit; the shortest form still names the part, the number, the unit and the direction:

  BAT-04A 1 cycle past 300-cycle limit     BAT-01A 206 cycles left of 300
  PROP-01 1.2 h past 300 h limit           PROP-01 102.9 h left of 300 h
  BAT-04A reached 300-cycle limit          PROP-01 reached 300 h limit
  BAT-04B past calendar limit 2026-08-31   BAT-04B 17 days left, due 2026-08-31
  100 h insp 3.6 h overdue, in tolerance   100 h insp 12.0 h overdue
  100 h insp due in 82.5 h                 annual insp due by 2027-04-30
  annual insp 12 days overdue              100 h insp: time in service not known
  AOG awaiting parts since 2026-08-14      in maintenance since 2026-08-07
  deferred defect since 2026-08-07         not known: no maintenance record
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from uas_workbench.flight.record import Maybe, Unknown, is_known

from .engine import NO_RECORD, _work_sentence
from .model import Board, DueItem, DueList, WorkOrder

CUE_LIMIT = 40
NOT_KNOWN = "not known"
NUMBER = re.compile(r"(?<![\w.-])\d+(?:\.\d+)?(?![\w.])")
DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
# The words that carry a state. Each state allows exactly one of these families.
STATE_WORDS: dict[str, tuple[str, ...]] = {
    "ok": ("left", "due in", "due by"),
    "due_soon": ("left", "due in", "due by"),
    "overdue_within_tolerance": ("in tolerance",),
    "overdue": ("past", "overdue", "reached"),
    "unknown": (NOT_KNOWN,),
}
# The sentence of a life limit exactly reached; its cue says "reached" and no other does.
HAS_REACHED = "has reached its"
UNIT_WORDS: dict[str, tuple[str, ...]] = {"h": (" h",), "cycles": ("cycle",), "days": ("day",)}


def _fit(*forms: str) -> str:
    """The first form within the limit; the last form must always fit."""
    for f in forms:
        if len(f) <= CUE_LIMIT:
            return f
    return forms[-1][:CUE_LIMIT]  # never reached for any id under 20 characters; kept honest


def _name(subject: str) -> str:
    """'the 100-hour inspection' -> '100 h insp'; 'the annual inspection' -> 'annual insp'."""
    return subject.removeprefix("the ").replace("-hour", " h").replace("inspection", "insp")


def _plural(n: float, unit: str) -> str:
    return f"{n:.0f} {unit}{'' if n == 1 else 's'}"


def _due_date(item: DueItem, as_of: datetime) -> str:
    """The calendar due date, as the sentence names it: remaining days counted from as_of."""
    assert is_known(item.remaining)
    return (as_of.date() + timedelta(days=int(item.remaining))).isoformat()


def item_cue(item: DueItem, as_of: datetime) -> str:
    """The cue of one due item, from its own fields."""
    if item.state == "unknown" or isinstance(item.remaining, Unknown):
        return _fit(
            f"{_name(item.subject)}: time in service {NOT_KNOWN}",
            f"{_name(item.subject)} {NOT_KNOWN}",
        )
    rem: float = item.remaining
    limit = item.limit
    if item.component_id is not None:
        cid = item.component_id
        if item.basis == "hours":
            if rem > 0:
                return _fit(f"{cid} {rem:.1f} h left of {limit:.0f} h", f"{cid} {rem:.1f} h left")
            if rem == 0:
                return _fit(f"{cid} reached {limit:.0f} h limit", f"{cid} reached {limit:.0f} h")
            return _fit(
                f"{cid} {-rem:.1f} h past {limit:.0f} h limit", f"{cid} {-rem:.1f} h past limit"
            )
        if item.basis == "cycles":
            if rem > 0:
                return _fit(
                    f"{cid} {_plural(rem, 'cycle')} left of {limit:.0f}",
                    f"{cid} {_plural(rem, 'cycle')} left",
                )
            if rem == 0:
                return _fit(
                    f"{cid} reached {limit:.0f}-cycle limit", f"{cid} reached {limit:.0f} cycles"
                )
            return _fit(
                f"{cid} {_plural(-rem, 'cycle')} past {limit:.0f}-cycle limit",
                f"{cid} {_plural(-rem, 'cycle')} past limit",
            )
        due = _due_date(item, as_of)
        if rem >= 0:
            return _fit(
                f"{cid} {_plural(rem, 'day')} left, due {due}", f"{cid} {_plural(rem, 'day')} left"
            )
        return _fit(f"{cid} past calendar limit {due}", f"{cid} past calendar limit")
    name = _name(item.subject)
    if item.basis == "hours":
        if rem >= 0:
            return _fit(f"{name} due in {rem:.1f} h")
        if item.state == "overdue_within_tolerance":
            return _fit(
                f"{name} {-rem:.1f} h overdue, in tolerance", f"{name} {-rem:.1f} h in tolerance"
            )
        return _fit(f"{name} {-rem:.1f} h overdue")
    due = _due_date(item, as_of)
    if rem >= 0:
        return _fit(f"{name} due by {due}")
    return _fit(f"{name} {_plural(-rem, 'day')} overdue")


def work_cue(w: WorkOrder) -> str:
    """The cue of an open work order: its state and date; the description is a person's text
    and is never cut."""
    since = w.opened_utc.date().isoformat()
    if w.state == "awaiting_parts":
        return f"AOG awaiting parts since {since}"
    if w.state == "in_work":
        return f"in maintenance since {since}"
    return f"deferred defect since {since}"


def status_cue(status: Maybe[str]) -> str:
    """The board state in a few words. A known state is its own cue."""
    if isinstance(status, str):
        return status
    if status.reason == NO_RECORD:
        return f"{NOT_KNOWN}: no maintenance record"
    return f"{NOT_KNOWN}: time in service"


def reason_cues(due: DueList, state: Board) -> tuple[str, ...]:
    """One cue per reason, in the reasons' order: a due item's cue for its sentence, a work
    order's cue for its sentence. A reason nothing produced is an error, never a blank."""
    by_message = {i.message: i for i in due.items}
    by_sentence = {_work_sentence(due.aircraft_key, w): w for w in due.work_orders}
    cues = []
    for reason in state.reasons:
        if reason in by_message:
            cues.append(item_cue(by_message[reason], due.as_of))
        elif reason in by_sentence:
            cues.append(work_cue(by_sentence[reason]))
        else:
            raise ValueError(f"no cue for the reason {reason!r}")
    return tuple(cues)


# ---- the checker -----------------------------------------------------------------------


def _numbers(text: str) -> set[str]:
    out: set[str] = set()
    for m in NUMBER.finditer(DATE.sub(" ", text)):
        value = float(m.group(0))
        out |= {m.group(0), f"{value:.1f}", f"{value:.0f}", f"{value:g}"}
    return out


def problems(cue: str, item: DueItem, as_of: datetime) -> list[str]:
    """Every way `cue` fails to be the short form of `item`'s sentence."""
    found: list[str] = []
    if len(cue) > CUE_LIMIT:
        found.append(f"longer than {CUE_LIMIT} characters: {len(cue)}")
    name = item.component_id if item.component_id is not None else _name(item.subject)
    if name not in cue:
        found.append(f"does not name {name}")
    sentence_numbers = _numbers(item.message)
    for n in NUMBER.findall(DATE.sub(" ", cue)):
        if n not in sentence_numbers:
            found.append(f"number {n} is not in the sentence")
    for d in DATE.findall(cue):
        if d not in item.message:
            found.append(f"date {d} is not in the sentence")
    # A calendar item may carry its date instead of a count of days.
    if not any(w in cue for w in UNIT_WORDS[item.unit]) and not (
        item.unit == "days" and DATE.search(cue)
    ):
        found.append(f"unit {item.unit} is missing")
    for other, words in UNIT_WORDS.items():
        if other != item.unit and any(w in cue for w in words):
            found.append(f"unit {other} named for a {item.unit} item")
    expected = STATE_WORDS[item.state]
    if not any(w in cue for w in expected):
        found.append(f"state {item.state} not said ({' or '.join(expected)})")
    for state, words in STATE_WORDS.items():
        if words != expected:
            for w in words:
                if w in cue and not (w == "overdue" and item.state == "overdue_within_tolerance"):
                    found.append(f"state word {w!r} belongs to {state}, not {item.state}")
    # Reached is its own word: a part past its limit is not merely at it, and one exactly at
    # it is neither past nor overdue.
    if "reached" in cue and HAS_REACHED not in item.message:
        found.append("the cue says reached and the sentence does not")
    if HAS_REACHED in item.message and "reached" not in cue:
        found.append("the sentence says the limit is reached and the cue does not say reached")
    if item.state == "unknown" and NOT_KNOWN not in cue:
        found.append("a not-known item's cue must say not known")
    if item.state == "unknown" and "serviceable" in cue:
        found.append("a not-known item's cue must say not known, never serviceable")
    return found
