"""SQLite storage for aircraft and their flight records."""

from __future__ import annotations

from collections.abc import Sequence

from uas_workbench.fleet.model import Aircraft, Fleet
from uas_workbench.flight.record import FlightRecord


class Store:
    def __init__(self, path: str = ":memory:") -> None:
        raise NotImplementedError

    def add_fleet(self, fleet: Fleet) -> None:
        raise NotImplementedError

    def add_aircraft(self, aircraft: Aircraft) -> None:
        raise NotImplementedError

    def add_flight(self, aircraft_key: str, record: FlightRecord) -> None:
        raise NotImplementedError

    def aircraft(self) -> Sequence[Aircraft]:
        raise NotImplementedError

    def get_aircraft(self, key: str) -> Aircraft | None:
        raise NotImplementedError

    def flights(self, aircraft_key: str) -> Sequence[FlightRecord]:
        raise NotImplementedError

    def flight_count(self) -> int:
        raise NotImplementedError
