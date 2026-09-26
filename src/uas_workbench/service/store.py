"""SQLite storage for aircraft, their flight records and the maintenance ledger.

Records are stored as the JSON the codecs produce, with the columns the queries need
alongside. One file, no server, and the same code runs in tests, in CI and in the container.
The ledger is append-only: entries are inserted and never updated or deleted. Nothing about
maintenance is stored as a snapshot; the records the engine reads are projected from the
entries on read, and no board state is stored either.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Sequence

from uas_workbench.fleet.model import Aircraft, Fleet
from uas_workbench.flight.codec import record_from_json, record_to_json
from uas_workbench.flight.record import FlightRecord, Source, is_known
from uas_workbench.ledger.codec import entry_from_json, entry_to_json
from uas_workbench.ledger.model import Entry, Projection
from uas_workbench.ledger.project import project
from uas_workbench.life import Component, MaintenanceRecord

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
CREATE TABLE IF NOT EXISTS entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject TEXT NOT NULL,
    kind TEXT NOT NULL,
    occurred_utc TEXT NOT NULL,
    recorded_utc TEXT NOT NULL,
    synthetic INTEGER NOT NULL,
    supersedes INTEGER REFERENCES entries(id),
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
        self._projection: Projection | None = None

    def close(self) -> None:
        self._db.close()

    def add_fleet(self, fleet: Fleet) -> None:
        for aircraft in fleet.aircraft:
            self.add_aircraft(aircraft)
            for record in fleet.flights.get(aircraft.key, ()):
                self.add_flight(aircraft.key, record)
        for entry in fleet.entries:
            self.append_entry(entry)

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

    # ---- the ledger ------------------------------------------------------------------

    def append_entry(self, entry: Entry) -> Entry:
        """Insert one entry and return it with its id. Validation is the ledger's job."""
        with self._lock, self._db:
            cursor = self._db.execute(
                "INSERT INTO entries (subject, kind, occurred_utc, recorded_utc, synthetic, "
                "supersedes, record) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    entry.subject,
                    entry.kind,
                    entry.occurred_utc.isoformat(),
                    entry.recorded_utc.isoformat(),
                    int(entry.synthetic),
                    entry.supersedes,
                    "",
                ),
            )
            assert cursor.lastrowid is not None
            stored = Entry(**{**entry.__dict__, "id": int(cursor.lastrowid)})
            self._db.execute(
                "UPDATE entries SET record = ? WHERE id = ?",
                (json.dumps(entry_to_json(stored), ensure_ascii=False), stored.id),
            )
            self._projection = None
        return stored

    def next_entry_id(self) -> int:
        row = self._db.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM entries").fetchone()
        return int(row[0])

    def entries(self, subject: str | None = None) -> Sequence[Entry]:
        """Every entry, superseded ones included, in the order they were entered."""
        if subject is None:
            rows = self._db.execute("SELECT record FROM entries ORDER BY id").fetchall()
        else:
            rows = self._db.execute(
                "SELECT record FROM entries WHERE subject = ? ORDER BY id", (subject,)
            ).fetchall()
        return [entry_from_json(json.loads(r[0])) for r in rows]

    def entry(self, entry_id: int) -> Entry | None:
        row = self._db.execute("SELECT record FROM entries WHERE id = ?", (entry_id,)).fetchone()
        return entry_from_json(json.loads(row[0])) if row else None

    def entry_count(self) -> int:
        row = self._db.execute("SELECT COUNT(*) FROM entries").fetchone()
        return int(row[0])

    def projection(self) -> Projection:
        """The live entries folded into records; recomputed after every append."""
        if self._projection is None:
            self._projection = project(self.entries())
        return self._projection

    def components(self) -> Sequence[Component]:
        return self.projection().components

    def maintenance(self, aircraft_key: str) -> MaintenanceRecord | None:
        return self.projection().maintenance.get(aircraft_key)

    # ---- reads -----------------------------------------------------------------------

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


def _source(value: str) -> Source:
    if value == "px4":
        return "px4"
    if value == "ardupilot":
        return "ardupilot"
    raise ValueError(f"unknown source {value!r} in store")
