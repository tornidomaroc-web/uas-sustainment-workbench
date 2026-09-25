"""Aircraft and fleet records."""

from __future__ import annotations

from dataclasses import dataclass, field

from uas_workbench.flight.record import FlightRecord, Source
from uas_workbench.life import Component, MaintenanceRecord


@dataclass(frozen=True)
class Aircraft:
    key: str
    label: str
    source: Source
    synthetic: bool
    licence: str
    attribution: str


@dataclass(frozen=True)
class Fleet:
    """Aircraft, their flight records, and the maintenance records logs never hold.

    The board state of an aircraft is not stored anywhere: it is computed from its
    maintenance record, its components and its flights (uas_workbench.life).
    """

    aircraft: tuple[Aircraft, ...]
    flights: dict[str, tuple[FlightRecord, ...]]  # aircraft key -> its records
    components: tuple[Component, ...] = ()
    maintenance: dict[str, MaintenanceRecord] = field(default_factory=dict)
