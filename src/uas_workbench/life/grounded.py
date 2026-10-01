"""Logged flights that start while the records show the aircraft grounded.

A log is evidence: it is never refused, and its flight counts toward usage whatever the
board said (life.engine). What this module adds is the comparison nothing else makes: each
log's start against the board state the records give at that instant. Where that state is a
known unserviceable, in maintenance or AOG, the flight is reported, in one sentence that
names the aircraft, the log's start, the state and what caused it.

The state is judged at the instant the log starts, on everything dated up to and including
that instant: the answer the board gives at `as_of=<the log's start>`, so a reader can check
every finding. The log's own flight is not in that state, because a log counts from its end.

A finding says that the records and the log disagree. It does not say who is right: the
entry may have been dated back, the log may belong to another airframe (a log names the
flight controller only), or the aircraft may have flown as the log says. A state that is not
known is never a finding; such a log is listed as not judged, with why.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime

from uas_workbench.flight.reconcile import FLUSH_INTERVAL_S, reconcile
from uas_workbench.flight.record import FlightRecord, Maybe, Unknown, is_known

from .cue import flight_cue, flight_cue_problems, reason_cues
from .engine import counted, grounding_reasons
from .model import Board, DueList

KIND = "flight_while_grounded"
# The board states in which an aircraft does not fly. "serviceable with deferred defects" is
# not one: a deferred defect or an inspection inside its tolerance leaves the aircraft flying.
GROUNDING: tuple[str, ...] = ("unserviceable", "in maintenance", "AOG")
StateAt = Callable[[datetime], tuple[DueList, Board]]


@dataclass(frozen=True)
class GroundedFlight:
    aircraft_key: str
    log_ref: str
    utc_start: datetime
    status: str  # one of GROUNDING
    reasons: tuple[str, ...]  # what grounded the aircraft at that instant, worst first
    message: str
    cue: str
    cause_cues: tuple[str, ...]  # one per reason, in the same order


@dataclass(frozen=True)
class NotJudged:
    log_ref: str
    utc_start: Maybe[datetime]
    why: str


@dataclass(frozen=True)
class GroundedFlights:
    aircraft_key: str
    logs: int  # the logs that had ended by the date asked
    judged: int  # those whose flight started in a known state
    findings: tuple[GroundedFlight, ...]
    not_judged: tuple[NotJudged, ...]


def judged_at(record: FlightRecord) -> Maybe[datetime]:
    """The instant a log is judged at: its start."""
    return record.utc_start


def grounds(status: object) -> bool:
    """A known state in which the aircraft does not fly. Not known never grounds."""
    return isinstance(status, str) and status in GROUNDING


def _stamp(t: datetime) -> str:
    return f"{t:%Y-%m-%d %H:%M:%S} UTC"


def sentence(key: str, start: datetime, status: object, reasons: Sequence[str]) -> str:
    return (
        f"a flight of aircraft {key} was logged from {_stamp(start)} while the records show "
        f"the aircraft {status} at that time: {'; '.join(reasons)}"
    )


def grounded_flights(
    aircraft_key: str,
    records: Sequence[FlightRecord],
    state_at: StateAt,
    *,
    until: datetime | None = None,
    tolerance_s: float = FLUSH_INTERVAL_S,
    excluded: str | None = None,
) -> GroundedFlights:
    """Judge every log of `aircraft_key` that had ended by `until`.

    `state_at(t)` is the due list and board of the aircraft at `t`. `excluded`, when given,
    is why no log of this aircraft is judged at all (the public showcase aircraft)."""
    duplicates = {
        ref: why.removeprefix("duplicate of ")
        for ref, why in reconcile(
            records, aircraft_key=aircraft_key, tolerance_s=tolerance_s
        ).unchecked.items()
        if why.startswith("duplicate of ")
    }
    ended = [r for r in records if counted(r, until)]
    dated = sorted(
        (r for r in ended if is_known(judged_at(r))), key=lambda r: (judged_at(r), r.log_ref)
    )
    undated = [r for r in ended if not is_known(judged_at(r))]
    findings: list[GroundedFlight] = []
    not_judged: list[NotJudged] = []
    judged = 0
    for r in (*dated, *undated):
        at = judged_at(r)
        if excluded is not None:
            not_judged.append(NotJudged(r.log_ref, r.utc_start, excluded))
        elif isinstance(at, Unknown):
            not_judged.append(
                NotJudged(
                    r.log_ref,
                    r.utc_start,
                    f"log {r.log_ref} has no UTC start ({at.reason}), so the state when it "
                    "began cannot be looked up",
                )
            )
        elif r.log_ref in duplicates:
            not_judged.append(
                NotJudged(
                    r.log_ref,
                    r.utc_start,
                    f"log {r.log_ref} is a duplicate of {duplicates[r.log_ref]} and is not "
                    "judged twice",
                )
            )
        elif isinstance(r.flight_time_s, Unknown):
            not_judged.append(
                NotJudged(
                    r.log_ref,
                    r.utc_start,
                    f"log {r.log_ref} does not say whether the aircraft flew "
                    f"({r.flight_time_s.reason})",
                )
            )
        elif r.flight_time_s <= 0:
            not_judged.append(
                NotJudged(r.log_ref, r.utc_start, f"log {r.log_ref} shows no flight: 0 s airborne")
            )
        else:
            due, state = state_at(at)
            if not isinstance(state.status, str) and not grounds(state.status):
                not_judged.append(
                    NotJudged(
                        r.log_ref,
                        r.utc_start,
                        f"the state of aircraft {aircraft_key} at {_stamp(at)} is not known: "
                        f"{state.status.reason}",
                    )
                )
                continue
            judged += 1
            if grounds(state.status):
                findings.append(_finding(aircraft_key, r.log_ref, at, due, state))
    return GroundedFlights(aircraft_key, len(ended), judged, tuple(findings), tuple(not_judged))


def _finding(key: str, log_ref: str, at: datetime, due: DueList, state: Board) -> GroundedFlight:
    reasons = grounding_reasons(due)
    status = str(state.status)  # a grounding state is a known one, so this is its own word
    return GroundedFlight(
        aircraft_key=key,
        log_ref=log_ref,
        utc_start=at,
        status=status,
        reasons=reasons,
        message=sentence(key, at, status, reasons),
        cue=flight_cue(status, at),
        cause_cues=reason_cues(due, Board(status, reasons)),
    )


def problems(finding: GroundedFlight, state_at: StateAt) -> list[str]:
    """Every way `finding` fails to be what the records say at its log's start: the checker
    the tests and the mutation tests share."""
    found: list[str] = []
    f = finding
    due, state = state_at(f.utc_start)
    if not grounds(state.status):
        label = state.status if isinstance(state.status, str) else "not known"
        found.append(f"the aircraft was not grounded at {_stamp(f.utc_start)}: {label}")
    if state.status != f.status:
        found.append(f"state at {_stamp(f.utc_start)} is {state.status}, not {f.status}")
    if f.status not in GROUNDING:
        found.append(f"state {f.status} is not a grounding state")
    actual = grounding_reasons(due)
    if not f.reasons:
        found.append("no reason given")
    for reason in f.reasons:
        if reason not in actual:
            found.append(f"reason {reason!r} is not a grounding reason at that time")
    for reason in actual:
        if reason not in f.reasons:
            found.append(f"grounding reason {reason!r} is left out")
    if f.message != sentence(f.aircraft_key, f.utc_start, f.status, f.reasons):
        found.append("the sentence is not the one its aircraft, start, state and reasons make")
    found.extend(f"cue: {p}" for p in flight_cue_problems(f.cue, f.status, f.utc_start))
    if len(f.cause_cues) != len(f.reasons):
        found.append("cue: the cause cues do not stand one for one beside the reasons")
    return found
