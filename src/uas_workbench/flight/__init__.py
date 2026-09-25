"""One normalised FlightRecord per log, and reconciliation against the autopilot's own counters.

Every value a log cannot support is an explicit Unknown with a reason, never a zero or a guess.
"""

from .reconcile import Coverage, Finding, Reconciliation, reconcile
from .record import FaultEvent, FlightRecord, LifetimeCounter, Maybe, Unknown, is_known

__all__ = [
    "Coverage",
    "FaultEvent",
    "Finding",
    "FlightRecord",
    "LifetimeCounter",
    "Maybe",
    "Reconciliation",
    "Unknown",
    "is_known",
    "reconcile",
]
