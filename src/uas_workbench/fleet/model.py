"""Aircraft and fleet records."""

from __future__ import annotations

from dataclasses import dataclass

from uas_workbench.flight.record import FlightRecord, Source
from uas_workbench.ledger.model import Entry
from uas_workbench.ledger.project import project
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
    """Aircraft, their flight records, and the maintenance ledger entries logs never hold.

    Nothing about maintenance is stored as a snapshot: the records the engine reads are
    projected from the entries, and the board state is computed from those on every read.
    """

    aircraft: tuple[Aircraft, ...]
    flights: dict[str, tuple[FlightRecord, ...]]  # aircraft key -> its records
    entries: tuple[Entry, ...] = ()

    @property
    def maintenance(self) -> dict[str, MaintenanceRecord]:
        return project(self.entries).maintenance

    @property
    def components(self) -> tuple[Component, ...]:
        return project(self.entries).components
