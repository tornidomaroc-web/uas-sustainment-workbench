"""Reconcile logged flight time against the autopilot's lifetime counter."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .record import FlightRecord, Maybe

FLUSH_INTERVAL_S = 30.0  # ArduPilot AP_Stats writes its counters every 30 s


@dataclass(frozen=True)
class Coverage:
    """One consecutive pair of logs of the same aircraft.

    unlogged_s = counter at the next boot - (counter at this boot + this log's flight time):
    flight the autopilot counted that no log covers. Small negative values are normal, since
    the counter is written only every 30 s (ArduPilot) or at disarm (PX4).
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


def reconcile(
    records: Sequence[FlightRecord],
    *,
    aircraft_key: str | None = None,
    tolerance_s: float = FLUSH_INTERVAL_S,
) -> Reconciliation:
    raise NotImplementedError
