"""SQLite storage for aircraft, their flight records and the maintenance ledger.

Records are stored as the JSON the codecs produce, with the columns the queries need
alongside. One file, no server, and the same code runs in tests, in CI and in the container.
The ledger is append-only: entries are inserted and never updated or deleted. Nothing about
maintenance is stored as a snapshot; the records the engine reads are projected from the
entries on read, and no board state is stored either.

Every entry and every flight record is linked into the write journal (journal.py) in the
transaction that writes it, so a change made to either directly in the SQLite file is
reported by `verify_journal`. A store written by 0.5.x is journaled when first opened, after
a note saying that what came before is not tamper-evident.

A file store is in SQLite's WAL mode (`PRAGMA journal_mode`, kept in the file): a reader on
another connection keeps a snapshot while the store commits, where the rollback journal made
the commit wait for the reader and fail (issue #44). Connections are never shared between
threads that read at the same time (issue #60): the store holds one writing connection, used
under its write lock only, and every thread that reads gets its own reading connection, opened
on its first read and closed by `close`, on which each read runs inside one read transaction,
under WAL a snapshot that no write waits for. Python's `sqlite3` keeps one statement cache per
connection, so two threads reading through one connection stepped the same prepared statement
and read each other's rows; a read connection has one thread, so that cannot happen. The two
answers that are kept rather than computed on every call, `journal_state` and `readable`,
read their rows and the version they are labelled with on the writing connection under the
write lock, where the store's own commits do not move `PRAGMA data_version` and a commit from
another connection does; the kept projection is computed on one more connection of its own,
under its own lock, so a request never waits for a write and a write never waits for it.
`PRAGMA synchronous` stays FULL: a journal link is on disk before the commit returns, as
before. `PRAGMA busy_timeout` is 5 s: a second writer (a `uasw record` beside a running
service) waits that long for the write lock, then fails as before. While a store is open its
file has `-wal` and `-shm` beside it; `close` closes every reading connection, checkpoints the
WAL into the file and closes the writing one, so when it was the last store on the file SQLite
removes both and a closed store is one file. A store in memory has no file, keeps the mode
SQLite gives it, and has one connection, which every read and write takes the write lock for.

Every record is read through one path (`_entry`, `_flight`), and a record that cannot be
read there (not JSON, nested too deep for the parser, a field missing or of the wrong type,
a time with no UTC offset) raises `UnreadableRecord` naming the table, the row, its aircraft
or component and why, instead of a parser's exception; so does the projection when an
entry's details cannot be used. No read skips such a record and goes on: whatever depended
on it is not known, and the service, the command line and the evidence pack say so.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from datetime import datetime
from typing import Any

from uas_workbench.fleet.model import Aircraft, Fleet
from uas_workbench.flight.codec import record_from_json, record_to_json
from uas_workbench.flight.record import FlightRecord, Source, is_known
from uas_workbench.ledger.codec import entry_from_json, entry_to_json
from uas_workbench.ledger.model import Entry, Projection, UnreadableRecord
from uas_workbench.ledger.project import project
from uas_workbench.life import Component, MaintenanceRecord

from . import journal
from .journal import Verification

SCHEMA = (
    """
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
    + journal.SCHEMA
)
AIRCRAFT_COLUMNS = "key, label, source, synthetic, licence, attribution"
# How long a write waits for another writer's lock before it fails. Writes take milliseconds;
# a second writer is `uasw record` beside a running service, or two services on one file.
BUSY_TIMEOUT_MS = 5000


class DuplicateFlight(Exception):
    pass


class Store:
    def __init__(self, path: str = ":memory:") -> None:
        self._path = path
        # The writing connection, used under the write lock only. The lock is re-entrant: a
        # store in memory has this one connection, so its reads take the lock too, and a read
        # made while the lock is held (a write that reads first) must not wait for itself.
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.execute("PRAGMA synchronous = FULL")
        self._lock = threading.RLock()
        # One reading connection per thread, opened on the thread's first read and kept in
        # `_readers` so `close` can close every one of them; a thread finds its own through
        # `_local`. A store in memory has no file to open another connection on, so it has
        # none, and the kept projection has no connection of its own there either.
        self._local = threading.local()
        self._readers: list[sqlite3.Connection] = []
        self._readers_lock = threading.Lock()
        self._cache: sqlite3.Connection | None = None
        self._closed = False
        if path != ":memory:":
            self._db.execute("PRAGMA journal_mode = WAL")
            self._cache = self._open_reader()
        with self._lock:
            self._db.executescript(SCHEMA)
            with self._db:
                journal.migrate(self._db)
        self._projection: Projection | None = None
        self._projection_version = -1
        self._projection_lock = threading.Lock()
        self._unreadable: UnreadableRecord | None = None
        self._readable_version = -1
        # The last walk of the journal (verify_journal with no head), kept for the reads that
        # need no token and so must not walk the whole journal on every call; see journal_state.
        self._journal: Verification | None = None
        self._journal_version = -1

    def close(self) -> None:
        """Close every connection: each thread's reading connection and the kept projection's
        first, then the writing one, after the WAL is checkpointed into the file, so that when
        this was the last connection the file alone holds every committed write and SQLite
        removes `-wal` and `-shm`; another connection still open keeps them, and SQLite does
        the same when that one closes. Called once the threads that read are done (a server
        after it has stopped, a command when it ends); a read after it raises
        `sqlite3.ProgrammingError`, as on a closed connection. Closing a closed store does
        nothing."""
        self._closed = True
        with self._readers_lock:
            readers, self._readers = self._readers, []
        for reader in readers:
            reader.close()
        with contextlib.suppress(sqlite3.Error):
            self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self._db.close()

    # ---- connections -----------------------------------------------------------------

    def _open_reader(self) -> sqlite3.Connection:
        """A connection for reads on the store file: autocommit, so that `BEGIN` and
        `ROLLBACK` below bound exactly one read transaction; query-only, so nothing is ever
        written through it; closable from another thread, which is how `close` closes it."""
        connection = sqlite3.connect(self._path, check_same_thread=False, isolation_level=None)
        connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        connection.execute("PRAGMA query_only = ON")
        with self._readers_lock:
            if self._closed:
                connection.close()
                raise sqlite3.ProgrammingError("Cannot operate on a closed database.")
            self._readers.append(connection)
        return connection

    def _reader(self) -> sqlite3.Connection:
        """This thread's own reading connection, opened on its first read."""
        connection: sqlite3.Connection | None = getattr(self._local, "reader", None)
        if connection is None:
            connection = self._open_reader()
            self._local.reader = connection
        return connection

    @contextlib.contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        """One read transaction on this thread's own connection: under WAL a snapshot, so a
        read of several statements describes one state of the file and no write waits for it.
        A read made inside one already open on this thread joins it. A store in memory has one
        connection and reads it under the write lock."""
        if self._cache is None:
            with self._lock:
                yield self._db
            return
        yield from self._snapshot(self._reader())

    @staticmethod
    def _snapshot(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
        if connection.in_transaction:
            yield connection
            return
        connection.execute("BEGIN")
        try:
            yield connection
        finally:
            # Nothing was written; this ends the snapshot. A connection closed meanwhile (the
            # store was closed under a running read) has nothing left to end.
            with contextlib.suppress(sqlite3.ProgrammingError):
                connection.execute("ROLLBACK")

    def add_fleet(self, fleet: Fleet) -> None:
        for aircraft in fleet.aircraft:
            self.add_aircraft(aircraft)
            for record in fleet.flights.get(aircraft.key, ()):
                self.add_flight(aircraft.key, record)
        # A fleet's entries carry provisional ids; their supersedes pointers are mapped to
        # the ids this store assigns, in order of entry.
        assigned: dict[int, int] = {}
        for entry in fleet.entries:
            target = None
            if entry.supersedes is not None:
                if entry.supersedes not in assigned:
                    raise ValueError(f"entry supersedes {entry.supersedes}, not seeded before it")
                target = assigned[entry.supersedes]
            stored = self.append_entry(
                Entry(**{**entry.__dict__, "id": None, "supersedes": target})
            )
            if entry.id is not None:
                assigned[entry.id] = stored.id or 0

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
            with self._lock:
                with self._db:
                    cursor = self._db.execute(
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
                    assert cursor.lastrowid is not None
                    head = journal.append_link(self._db, "flight", int(cursor.lastrowid))
                # Only once the commit has gone through: a commit refused is rolled back.
                self._journal_after_write(head)
        except sqlite3.IntegrityError as exc:
            raise DuplicateFlight(f"{aircraft_key}/{record.log_ref} is already stored") from exc

    # ---- the ledger ------------------------------------------------------------------

    def append_entry(self, entry: Entry) -> Entry:
        """Insert one entry and return it with its id. Validation is the ledger's job."""
        with self._lock:
            with self._db:
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
                head = journal.append_link(self._db, "entry", int(stored.id or 0))
                self._projection = None
            # Only once the commit has gone through: a commit refused is rolled back.
            self._journal_after_write(head)
        return stored

    @contextlib.contextmanager
    def writing(self) -> Iterator[None]:
        """Hold the write lock across a check and the write it decides, so that no other write
        made through this store lands between them: two requests served at once are then
        checked as if made one after the other (`ledger.append`). Reads go on meanwhile on
        their own connections. A write from another process is not held back by it."""
        with self._lock:
            yield

    def next_entry_id(self) -> int:
        with self._read() as db:
            row = db.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM entries").fetchone()
        return int(row[0])

    def entries(self, subject: str | None = None) -> Sequence[Entry]:
        """Every entry, superseded ones included, in the order they were entered."""
        with self._read() as db:
            return self._entries(db, subject)

    def _entries(self, db: sqlite3.Connection, subject: str | None = None) -> list[Entry]:
        if subject is None:
            rows = db.execute("SELECT id, subject, record FROM entries ORDER BY id").fetchall()
        else:
            rows = db.execute(
                "SELECT id, subject, record FROM entries WHERE subject = ? ORDER BY id", (subject,)
            ).fetchall()
        return [self._entry(r) for r in rows]

    def entry(self, entry_id: int) -> Entry | None:
        with self._read() as db:
            row = db.execute(
                "SELECT id, subject, record FROM entries WHERE id = ?", (entry_id,)
            ).fetchone()
            return self._entry(row) if row else None

    def entry_count(self) -> int:
        with self._read() as db:
            row = db.execute("SELECT COUNT(*) FROM entries").fetchone()
        return int(row[0])

    # ---- the write journal -----------------------------------------------------------

    def verify_journal(self, head: str | None = None) -> Verification:
        """Walk the journal and recompute every hash from the rows as they are now; with
        `head`, also whether that head is in the chain, which a truncated or recomputed
        chain fails.

        The walk is on this thread's reading connection, inside one read transaction: under
        WAL that is a snapshot, so the result describes one state of the file, and the store's
        writes go on meanwhile (issue #44); a write that lands during the walk is in the next
        one. A store in memory has one connection and walks it under the write lock, as before.
        """
        with self._read() as db:
            return journal.verify(db, head)

    def journal_state(self) -> Verification:
        """The result of the last walk of the journal, as `verify_journal()` with no head
        computes it, for the reads anyone may make (the records summary of an aircraft).

        The journal is walked once, and again only after another connection has committed to
        the file (SQLite's data_version moves), as `readable` does for /health. A write made
        through this store appends a link the store computed itself: when the chain verified
        before it, the chain is intact with one more link, so the head and the length move
        with no row read again; when it did not verify, the next call walks it again. So
        asking again, which anyone who can reach the service may do, never walks the journal:
        only a write, which needs the token, or a commit from outside the store makes the next
        call walk it, and once.
        """
        with self._lock:
            # On the writing connection, under the write lock, so that the store's own commits
            # are not what moves the version (they do not move a connection's own
            # data_version) and no own write lands halfway through. One read transaction for
            # the version and the walk: the walk is several reads, and a commit from another
            # connection between two of them would otherwise give a result no single state of
            # the file has, labelled with the older version.
            self._db.execute("BEGIN")
            try:
                version = int(self._db.execute("PRAGMA data_version").fetchone()[0])
                if self._journal is None or version != self._journal_version:
                    self._journal = journal.verify(self._db)
                    self._journal_version = version
            finally:
                self._db.execute("ROLLBACK")  # nothing was written; ends the snapshot
            return self._journal

    def _journal_after_write(self, head: str) -> None:
        """Called under the write lock, after this store linked one row and the commit went
        through: the kept result is moved on by that link when it was intact, and dropped
        otherwise."""
        kept = self._journal
        if kept is None:
            return
        if not kept.ok:
            self._journal = None
            return
        length = kept.length + 1
        self._journal = Verification(
            ok=True,
            head=head,
            length=length,
            broken=None,
            given=None,
            given_at=None,
            migration=kept.migration,
            line=journal.line(True, head, length, None, None, None),
        )

    def readable(self) -> UnreadableRecord | None:
        """The first stored record that cannot be read, or None when every entry reads and
        projects and every flight record reads; /health reports what it returns.

        The whole store is read once, and again only after another connection has committed
        to the file (SQLite's data_version moves; an edit made with sqlite3, or a repair). A
        record this store writes is one it wrote readable and leaves the answer as it was, so
        asking again, which anyone who can reach /health may do, never reads the store again.
        """
        with self._lock:
            # As `journal_state`: the writing connection under the write lock, in one read
            # transaction, so the version names exactly the state the rows were read in and
            # the store's own commits leave it still. A write waits for the one read of the
            # whole store that follows a commit from outside, never for a call.
            self._db.execute("BEGIN")
            try:
                version = int(self._db.execute("PRAGMA data_version").fetchone()[0])
                if version != self._readable_version:
                    self._unreadable = self._first_unreadable(self._db)
                    self._readable_version = version
            finally:
                self._db.execute("ROLLBACK")  # nothing was written; ends the snapshot
            return self._unreadable

    def _first_unreadable(self, db: sqlite3.Connection) -> UnreadableRecord | None:
        try:
            project(self._entries(db))
            rows = db.execute(
                "SELECT id, aircraft_key, log_ref, record FROM flights ORDER BY id"
            ).fetchall()
            for row in rows:
                self._flight(row)
        except UnreadableRecord as exc:
            return exc
        return None

    def projection(self, as_of: datetime | None = None) -> Projection:
        """The live entries folded into records, at `as_of` or as of every entry.

        The whole-ledger projection is cached and recomputed after every append; a dated
        one is folded on request from the same entries.
        """
        if as_of is not None:
            return project(self.entries(), as_of)
        # Computed on a connection of its own, in one read transaction under its own lock, so
        # the version it is labelled with names exactly the state its entries were read in:
        # a commit from any other connection, the store's own writes included (an own append
        # drops it anyway) or an edit made with sqlite3 while the service runs, moves that
        # connection's data_version and the kept projection is not served past it, so a
        # record that can no longer be read is met on the next read, never hidden by a copy.
        # A store in memory has one connection and computes it there under the write lock.
        with self._projection_read() as db:
            version = int(db.execute("PRAGMA data_version").fetchone()[0])
            if self._projection is None or version != self._projection_version:
                self._projection = project(self._entries(db))
                self._projection_version = version
            return self._projection

    @contextlib.contextmanager
    def _projection_read(self) -> Iterator[sqlite3.Connection]:
        # The write lock before the projection's own, always: a write held across its check
        # (`writing`) reads the projection under the write lock.
        if self._cache is None:
            with self._lock, self._projection_lock:
                yield self._db
            return
        with self._projection_lock:
            yield from self._snapshot(self._cache)

    def components(self, as_of: datetime | None = None) -> Sequence[Component]:
        return self.projection(as_of).components

    def maintenance(
        self, aircraft_key: str, as_of: datetime | None = None
    ) -> MaintenanceRecord | None:
        return self.projection(as_of).maintenance.get(aircraft_key)

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
        with self._read() as db:
            rows = db.execute(
                f"SELECT {AIRCRAFT_COLUMNS} FROM aircraft ORDER BY synthetic, key"
            ).fetchall()
        return [self._aircraft(r) for r in rows]

    def get_aircraft(self, key: str) -> Aircraft | None:
        with self._read() as db:
            row = db.execute(
                f"SELECT {AIRCRAFT_COLUMNS} FROM aircraft WHERE key = ?", (key,)
            ).fetchone()
        return self._aircraft(row) if row else None

    def flights(self, aircraft_key: str) -> Sequence[FlightRecord]:
        with self._read() as db:
            rows = db.execute(
                "SELECT id, aircraft_key, log_ref, record FROM flights WHERE aircraft_key = ? "
                "ORDER BY utc_start IS NULL, utc_start, log_ref",
                (aircraft_key,),
            ).fetchall()
            return [self._flight(r) for r in rows]

    def flight_count(self) -> int:
        with self._read() as db:
            row = db.execute("SELECT COUNT(*) FROM flights").fetchone()
        return int(row[0])

    # ---- the one read path for stored records ----------------------------------------

    def _entry(self, row: tuple[object, ...]) -> Entry:
        row_id, subject, text = row
        try:
            entry = entry_from_json(_loads(text))
            if entry.occurred_utc.tzinfo is None or entry.recorded_utc.tzinfo is None:
                raise ValueError("its time carries no UTC offset")
            return entry
        except _CANNOT_READ as exc:
            raise UnreadableRecord("entries", int(str(row_id)), str(subject), _why(exc)) from exc

    def _flight(self, row: tuple[object, ...]) -> FlightRecord:
        row_id, aircraft_key, log_ref, text = row
        try:
            record = record_from_json(_loads(text))
            if is_known(record.utc_start) and record.utc_start.tzinfo is None:
                raise ValueError("its UTC start carries no UTC offset")
            return record
        except _CANNOT_READ as exc:
            raise UnreadableRecord(
                "flights", int(str(row_id)), str(aircraft_key), _why(exc), str(log_ref)
            ) from exc


# What reading a record can raise: the parser on text that is not JSON or is nested too deep
# (RecursionError), the codecs on a field missing (KeyError) or of the wrong type (TypeError,
# ValueError; AttributeError when the record is not an object at all), and the shape checks
# above. Each is the record being unreadable, never a traceback for the caller.
_CANNOT_READ = (RecursionError, KeyError, ValueError, TypeError, AttributeError)


def _loads(text: object) -> Any:
    if not isinstance(text, str | bytes):
        raise TypeError(f"the record column holds {type(text).__name__}, not JSON text")
    return json.loads(text)


def _why(exc: BaseException) -> str:
    if isinstance(exc, RecursionError):
        return "nested too deep to parse"
    if isinstance(exc, json.JSONDecodeError):
        return f"not JSON ({exc})"
    if isinstance(exc, KeyError):
        return f"field {exc} is missing"
    return str(exc) or type(exc).__name__


def _source(value: str) -> Source:
    if value == "px4":
        return "px4"
    if value == "ardupilot":
        return "ardupilot"
    raise ValueError(f"unknown source {value!r} in store")
