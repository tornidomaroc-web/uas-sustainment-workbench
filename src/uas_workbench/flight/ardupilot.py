"""FlightRecord from an ArduPilot DataFlash log (.bin)."""

from __future__ import annotations

from pathlib import Path

from .record import FlightRecord


def read_dataflash(path: Path, *, licence: str, attribution: str) -> FlightRecord:
    raise NotImplementedError
