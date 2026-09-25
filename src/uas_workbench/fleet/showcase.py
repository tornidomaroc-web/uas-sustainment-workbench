"""The real, licensed log excerpts committed under tests/fixtures, as showcase aircraft.

They are the only real data in the demo and are labelled as such. Their positions and
device ids were removed before they entered the repository (DATA.md).
"""

from __future__ import annotations

from pathlib import Path

from uas_workbench.flight.ardupilot import read_dataflash
from uas_workbench.flight.px4 import read_ulog
from uas_workbench.flight.record import FlightRecord, Unknown

from .model import Aircraft, Fleet

ALFA_KEY = "alfa-fixed-wing"
PX4_KEY = "px4-validation-quadrotor"
ALFA_ATTRIBUTION = (
    "ALFA dataset, Keipour, Mousaei and Scherer, Carnegie Mellon University, CC BY 4.0; "
    "excerpt with positions and device id removed"
)
PX4_ATTRIBUTION = (
    "PX4 Flight Review public log, board-support validation flight of 2026-06-12, CC BY 4.0; "
    "excerpt with positions and device id removed"
)
NO_RECORD = Unknown("no maintenance record: public research or validation aircraft")


def showcase(fixtures_dir: Path) -> Fleet:
    alfa = [
        read_dataflash(p, licence="CC BY 4.0", attribution=ALFA_ATTRIBUTION)
        for p in sorted((fixtures_dir / "alfa").glob("*.bin"))
    ]
    px4 = [
        read_ulog(p, licence="CC BY 4.0", attribution=PX4_ATTRIBUTION)
        for p in sorted((fixtures_dir / "px4").glob("*.ulg"))
    ]
    aircraft: list[Aircraft] = []
    flights: dict[str, tuple[FlightRecord, ...]] = {}
    if alfa:
        aircraft.append(
            Aircraft(
                ALFA_KEY,
                "ALFA fixed-wing (real flights, 2018)",
                "ardupilot",
                False,
                "CC BY 4.0",
                ALFA_ATTRIBUTION,
                NO_RECORD,
            )
        )
        flights[ALFA_KEY] = tuple(alfa)
    if px4:
        aircraft.append(
            Aircraft(
                PX4_KEY,
                "PX4 board-validation quadrotor (real flight, 2026)",
                "px4",
                False,
                "CC BY 4.0",
                PX4_ATTRIBUTION,
                NO_RECORD,
            )
        )
        flights[PX4_KEY] = tuple(px4)
    return Fleet(tuple(aircraft), flights)
