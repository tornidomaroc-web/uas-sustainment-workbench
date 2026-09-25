"""Reconcile logged flight time against the autopilot's lifetime counter.

For consecutive logs of one aircraft: the counter the next log booted with, minus the
counter this log booted with plus the flight time this log shows, is flight the autopilot
counted that no log covers. On ArduPilot the counter is also written into the log every
30 s, which checks the derivation itself; PX4 saves it only at disarm, so only the next
boot's value is available.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise

from .record import FlightRecord, LifetimeCounter, Maybe, Source, Unknown, is_known

FLUSH_INTERVAL_S = 30.0  # ArduPilot AP_Stats writes its counters every 30 s
DUPLICATE_WINDOW_S = 1.0


@dataclass(frozen=True)
class Coverage:
    """One consecutive pair of logs of the same aircraft.

    unlogged_s = counter at the next boot - (counter at this boot + this log's flight time).
    Small negative values are normal: the counter lags the log by up to one flush.
    """

    aircraft_key: str
    log_ref: str
    next_log_ref: str
    unlogged_s: float
    boots_between: Maybe[int]


@dataclass(frozen=True)
class Finding:
    aircraft_key: str
    log_ref: str
    kind: str  # "unlogged_flight" | "counter_mismatch"
    seconds: float
    message: str


@dataclass(frozen=True)
class Reconciliation:
    coverage: tuple[Coverage, ...]
    findings: tuple[Finding, ...]
    unchecked: dict[str, str]  # log_ref -> why this log could not be reconciled


@dataclass(frozen=True)
class _Checkable:
    record: FlightRecord
    key: str
    lifetime: LifetimeCounter
    flight_s: float


def _checkable(record: FlightRecord, key: str | None) -> _Checkable | Unknown:
    aircraft = record.aircraft_key if key is None else key
    if isinstance(aircraft, Unknown):
        return Unknown(f"aircraft unknown: {aircraft.reason}")
    if isinstance(record.lifetime, Unknown):
        return Unknown(f"no lifetime counter: {record.lifetime.reason}")
    if isinstance(record.flight_time_s, Unknown):
        return Unknown(f"no flight time: {record.flight_time_s.reason}")
    return _Checkable(record, aircraft, record.lifetime, record.flight_time_s)


def _ordered(group: list[_Checkable]) -> list[_Checkable] | Unknown:
    if all(is_known(c.record.boot_count) for c in group):
        return sorted(group, key=lambda c: c.record.boot_count)  # type: ignore[arg-type, return-value]
    if all(is_known(c.record.utc_start) for c in group):
        return sorted(group, key=lambda c: c.record.utc_start)  # type: ignore[arg-type, return-value]
    return Unknown("cannot order the logs of this aircraft: boot count and UTC start unknown")


def _drop_duplicates(ordered: list[_Checkable], unchecked: dict[str, str]) -> list[_Checkable]:
    """The same log uploaded twice shows the same UTC start and span; keep the first."""
    kept: list[_Checkable] = []
    for c in ordered:
        for k in kept:
            same_start = (
                is_known(c.record.utc_start)
                and is_known(k.record.utc_start)
                and abs((c.record.utc_start - k.record.utc_start).total_seconds())
                < DUPLICATE_WINDOW_S
            )
            same_span = abs(c.record.log_span_s - k.record.log_span_s) < DUPLICATE_WINDOW_S
            if same_start and same_span:
                unchecked[c.record.log_ref] = f"duplicate of {k.record.log_ref}"
                break
        else:
            kept.append(c)
    return kept


def _boots_between(cur: FlightRecord, nxt: FlightRecord) -> Maybe[int]:
    if is_known(cur.boot_count) and is_known(nxt.boot_count):
        return nxt.boot_count - cur.boot_count - 1
    return Unknown("no boot counter")


def _in_log_mismatch(c: _Checkable, tolerance_s: float) -> Finding | None:
    """ArduPilot writes the counter during the log; compare its advance with the log."""
    if c.lifetime.last_seen_at_s <= 0:
        return None
    advanced = c.lifetime.last_seen_s - c.lifetime.at_boot_s
    gap = advanced - c.flight_s
    if abs(gap) <= tolerance_s:
        return None
    return Finding(
        c.key,
        c.record.log_ref,
        "counter_mismatch",
        gap,
        f"the counter advanced {advanced:.0f} s during log {c.record.log_ref} while the log "
        f"shows {c.flight_s:.0f} s of flight",
    )


def reconcile(
    records: Sequence[FlightRecord],
    *,
    aircraft_key: str | None = None,
    tolerance_s: float = FLUSH_INTERVAL_S,
) -> Reconciliation:
    """aircraft_key, when given, assigns every record to that aircraft (the operator knows
    which airframe a log came from even when the log itself cannot say)."""
    unchecked: dict[str, str] = {}
    groups: dict[tuple[Source, str], list[_Checkable]] = defaultdict(list)
    for record in records:
        checkable = _checkable(record, aircraft_key)
        if isinstance(checkable, Unknown):
            unchecked[record.log_ref] = checkable.reason
        else:
            groups[(record.source, checkable.key)].append(checkable)

    coverage: list[Coverage] = []
    findings: list[Finding] = []
    for (_, key), group in groups.items():
        ordered = _ordered(group)
        if isinstance(ordered, Unknown):
            unchecked.update({c.record.log_ref: ordered.reason for c in group})
            continue
        ordered = _drop_duplicates(ordered, unchecked)
        for cur, nxt in pairwise(ordered):
            unlogged = nxt.lifetime.at_boot_s - (cur.lifetime.at_boot_s + cur.flight_s)
            coverage.append(
                Coverage(
                    key,
                    cur.record.log_ref,
                    nxt.record.log_ref,
                    unlogged,
                    _boots_between(cur.record, nxt.record),
                )
            )
            if unlogged > tolerance_s:
                findings.append(
                    Finding(
                        key,
                        cur.record.log_ref,
                        "unlogged_flight",
                        unlogged,
                        f"{unlogged:.0f} s of flight on aircraft {key} is not covered by any "
                        f"log (after {cur.record.log_ref}, before {nxt.record.log_ref})",
                    )
                )
            elif unlogged < -tolerance_s:
                findings.append(
                    Finding(
                        key,
                        cur.record.log_ref,
                        "counter_mismatch",
                        unlogged,
                        f"the counter advanced {-unlogged:.0f} s less than log "
                        f"{cur.record.log_ref} shows before {nxt.record.log_ref}",
                    )
                )
        for c in ordered:
            if mismatch := _in_log_mismatch(c, tolerance_s):
                findings.append(mismatch)
        unchecked[ordered[-1].record.log_ref] = (
            "last known log of this aircraft; the counter was not seen again"
        )
    return Reconciliation(tuple(coverage), tuple(findings), unchecked)
