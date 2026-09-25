"""Typed records and value tests shared by the PX4 and ArduPilot probes."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

import numpy as np
import numpy.typing as npt

Source = Literal["px4", "ardupilot"]
FloatArray = npt.NDArray[np.float64]
FilledTest = Callable[[FloatArray], bool]

# The counters the maintenance engine would need, in the order the report prints them.
COUNTERS: tuple[str, ...] = (
    "aircraft_key",
    "utc_date",
    "flight_time",
    "arm_cycles",
    "landings",
    "battery_energy",
    "esc_data",
    "fault_events",
    "lifetime_hours",
)

# Counters that only make sense for a log in which the aircraft actually flew.
FLIGHT_COUNTERS: frozenset[str] = frozenset(
    {"flight_time", "landings", "battery_energy", "esc_data"}
)


@dataclass(frozen=True)
class FieldSpec:
    """One log field a maintenance counter needs (or a marked extra) and what 'filled' means."""

    key: str
    counter: str
    filled_means: str
    extra: bool = False


@dataclass(frozen=True)
class FieldResult:
    """present: the log carries the field with at least one sample.
    filled: at least one sample carries a real value, as the field's spec defines it."""

    present: bool
    filled: bool


ABSENT = FieldResult(present=False, filled=False)


@dataclass
class LogCensus:
    """Census result for one log. Holds booleans and metadata only: no positions, no ids."""

    source: Source
    group: str
    log_ref: str
    firmware: str
    duration_s: float
    airborne: bool
    fields: dict[str, FieldResult] = field(default_factory=dict)
    counters: dict[str, bool] = field(default_factory=dict)
    error: str | None = None


def _finite(values: FloatArray) -> FloatArray:
    return values[np.isfinite(values)]


def nonzero(values: FloatArray) -> bool:
    return bool(np.any(_finite(values) != 0))


def positive(values: FloatArray) -> bool:
    return bool(np.any(_finite(values) > 0))


def toggles(values: FloatArray) -> bool:
    """True when a state signal takes both a zero and a non-zero value, i.e. it changed."""
    finite = _finite(values)
    return bool(np.any(finite == 0) and np.any(finite != 0))


def equals(target: float) -> FilledTest:
    def test(values: FloatArray) -> bool:
        return bool(np.any(values == target))

    return test
