"""SQLite storage for aircraft, their flight records and their maintenance records.

Records are stored as the JSON the codecs produce, with the columns the queries need
alongside. One file, no server, and the same code runs in tests, in CI and in the container.
No board state is stored: the API computes it from these records on every request.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Sequence

from uas_workbench.fleet.model import Aircraft, Fleet
from uas_workbench.flight.codec import record_from_json, record_to_json
from uas_workbench.flight.record import FlightRecord, Source, is_known
from uas_workbench.life import Component, MaintenanceRecord
from uas_workbench.life.codec import (
    component_from_json,
    component_to_json,
    maintenance_from_json,
    maintenance_to_json,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS aircraft (
    key TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    source TEXT NOT NULL,
    synthetic INTEGER NOT NULL,
    licence TEXT NOT NULL,
    attribution TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS flights (
    id INTEGER PRIMARY KEY,
    aircraft_key TEXT NOT NULL REFERENCES aircraft(key),
    log_ref TEXT NOT NULL,
    synthetic INTEGER NOT NULL,
    utc_start TEXT,
    record TEXT NOT NULL,
    UNIQUE (aircraft_key, log_ref)
);
CREATE TABLE IF NOT EXISTS components (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    synthetic INTEGER NOT NULL,
    record TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS maintenance (
    aircraft_key TEXT PRIMARY KEY REFERENCES aircraft(key),
    synthetic INTEGER NOT NULL,
    record TEXT NOT NULL
);
"""
AIRCRAFT_COLUMNS = "key, label, source, synthetic, licence, attribution"


class DuplicateFlight(Exception):
    pass


class Store:
    def __init__(self, path: str = ":memory:") -> None:
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute("PRAGMA foreign_keys = ON")
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(SCHEMA)

    def close(self) -> None:
        self._db.close()

    def add_fleet(self, fleet: Fleet) -> None:
        for aircraft in fleet.aircraft:
            self.add_aircraft(aircraft)
            for record in fleet.flights.get(aircraft.key, ()):
                self.add_flight(aircraft.key, record)
        for component in fleet.components:
            self.add_component(component)
        for maintenance in fleet.maintenance.values():
            self.add_maintenance(maintenance)

    def add_aircraft(self, aircraft: Aircraft) -> None:
        with self._lock, self._db:
            self._db.execute(
                f"INSERT OR REPLACE INTO aircraft ({AIRCRAFT_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    aircraft.key,
                    aircraft.label,
                    aircraft.source,
                    int(aircraft.synthetic),
                    aircraft.licence,
                    aircraft.attribution,
                ),
            )

    def ensure_aircraft(self, key: str, record: FlightRecord) -> Aircraft:
        """An aircraft row for an ingested log whose aircraft is not yet known."""
        existing = self.get_aircraft(key)
        if existing is not None:
            return existing
        aircraft = Aircraft(
            key=key,
            label=key,
            source=record.source,
            synthetic=record.synthetic,
            licence=record.licence,
            attribution=record.attribution,
        )
        self.add_aircraft(aircraft)
        return aircraft

    def add_flight(self, aircraft_key: str, record: FlightRecord) -> None:
        utc = record.utc_start.isoformat() if is_known(record.utc_start) else None
        try:
            with self._lock, self._db:
                self._db.execute(
                    "INSERT INTO flights (aircraft_key, log_ref, synthetic, utc_start, record) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        aircraft_key,
                        record.log_ref,
                        int(record.synthetic),
                        utc,
                        json.dumps(record_to_json(record)),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise DuplicateFlight(f"{aircraft_key}/{record.log_ref} is already stored") from exc

    def add_component(self, component: Component) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO components (id, kind, synthetic, record) "
                "VALUES (?, ?, ?, ?)",
                (
                    component.id,
                    component.kind,
                    int(component.synthetic),
                    json.dumps(component_to_json(component)),
                ),
            )

    def add_maintenance(self, record: MaintenanceRecord) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO maintenance (aircraft_key, synthetic, record) "
                "VALUES (?, ?, ?)",
                (
                    record.aircraft_key,
                    int(record.synthetic),
                    json.dumps(maintenance_to_json(record)),
                ),
            )

    def _aircraft(self, row: tuple[object, ...]) -> Aircraft:
        key, label, source, synthetic, licence, attribution = row
        return Aircraft(
            key=str(key),
            label=str(label),
            source=_source(str(source)),
            synthetic=bool(synthetic),
            licence=str(licence),
            attribution=str(attribution),
        )

    def aircraft(self) -> Sequence[Aircraft]:
        rows = self._db.execute(
            f"SELECT {AIRCRAFT_COLUMNS} FROM aircraft ORDER BY synthetic, key"
        ).fetchall()
        return [self._aircraft(r) for r in rows]

    def get_aircraft(self, key: str) -> Aircraft | None:
        row = self._db.execute(
            f"SELECT {AIRCRAFT_COLUMNS} FROM aircraft WHERE key = ?", (key,)
        ).fetchone()
        return self._aircraft(row) if row else None

    def flights(self, aircraft_key: str) -> Sequence[FlightRecord]:
        rows = self._db.execute(
            "SELECT record FROM flights WHERE aircraft_key = ? "
            "ORDER BY utc_start IS NULL, utc_start, log_ref",
            (aircraft_key,),
        ).fetchall()
        return [record_from_json(json.loads(r[0])) for r in rows]

    def flight_count(self) -> int:
        row = self._db.execute("SELECT COUNT(*) FROM flights").fetchone()
        return int(row[0])

    def components(self) -> Sequence[Component]:
        rows = self._db.execute("SELECT record FROM components ORDER BY id").fetchall()
        return [component_from_json(json.loads(r[0])) for r in rows]

    def maintenance(self, aircraft_key: str) -> MaintenanceRecord | None:
        row = self._db.execute(
            "SELECT record FROM maintenance WHERE aircraft_key = ?", (aircraft_key,)
        ).fetchone()
        return maintenance_from_json(json.loads(row[0])) if row else None


def _source(value: str) -> Source:
    if value == "px4":
        return "px4"
    if value == "ardupilot":
        return "ardupilot"
    raise ValueError(f"unknown source {value!r} in store")
