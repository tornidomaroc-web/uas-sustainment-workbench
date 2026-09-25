"""Aircraft and fleet records."""

from __future__ import annotations

from dataclasses import dataclass

from uas_workbench.flight.record import FlightRecord, Maybe, Source


@dataclass(frozen=True)
class Aircraft:
    key: str
    label: str
    source: Source
    synthetic: bool
    licence: str
    attribution: str
    status: Maybe[str]  # one of the civil board states in fleet.toml, or Unknown with a reason


@dataclass(frozen=True)
class Fleet:
    aircraft: tuple[Aircraft, ...]
    flights: dict[str, tuple[FlightRecord, ...]]  # aircraft key -> its records
