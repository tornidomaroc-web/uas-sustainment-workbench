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
on its first read and closed by `close`. Python's `sqlite3` keeps one statement cache per
connection, so two threads reading through one connection stepped the same prepared statement
and read each other's rows; a read connection has one thread, so that cannot happen.

Every read runs inside a snapshot (`snapshot`): one read transaction on the thread's own
connection, under WAL a snapshot of one state of the file that no write waits for, labelled
with that state. A read made inside an open snapshot on the same thread joins it, so a
response built from several reads, the projection and the kept journal result it shows
included, is one state of the store when its builder opens one snapshot around them (the
records summaries and the evidence pack do). The label is the `PRAGMA data_version` of one
more connection the store keeps for nothing else, read under a lock of its own before and
after the snapshot's first read and taken only when the two reads agree: that version moves
on every commit to the file, the store's own and any other connection's, and on nothing else,
so two snapshots with one label read one state. The three answers the store keeps rather than
computes on every call, the kept journal result (`journal_state`), what `readable` holds and
the whole-ledger projection, are each labelled with the state they were computed from, and a
snapshot uses one only when its label is the snapshot's own; otherwise it computes the answer
on its own connection, inside its own transaction, and keeps it unless a newer state's answer
is kept already. The store's own commit moves the three labels on without computing anything,
when the kept answers described the state right before it, which the writing connection's
`PRAGMA data_version` tells: it moves on another connection's commit and never on the
store's own. So a call never walks the journal or reads the whole store, an own write never
does, and a commit from outside costs the first snapshot after it one walk and one read.
Nothing a snapshot does takes the write lock: a write through the store lands while a
snapshot is open and is in the next one. `PRAGMA synchronous` stays FULL: a journal link is
on disk before the commit returns, as before. `PRAGMA busy_timeout` is 5 s: a second writer
(a `uasw record` beside a running service) waits that long for the write lock, then fails as
before. While a store is open its file has `-wal` and `-shm` beside it; `close` closes every
reading connection, checkpoints the WAL into the file and closes the writing one, so when it
was the last store on the file SQLite removes both and a closed store is one file. A store in
memory has no file, keeps the mode SQLite gives it, and has one connection: a snapshot there
holds the write lock for its length, its label is the count of the store's own commits, and a
write issued meanwhile waits for it.

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
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
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


class _Kept[T]:
    """One answer the store keeps rather than computes on every call, labelled with the state
    it describes; its lock serialises the computations, so two snapshots of one state compute
    it once."""

    def __init__(self) -> None:
        self.value: tuple[int, T] | None = None  # (label, answer): one reference, replaced whole
        self.lock = threading.Lock()


@dataclass
class _Snapshot:
    label: int
    # The kept answers as they were when the snapshot began, so that an own commit landing
    # while it is open, which moves the store's labels on, leaves the snapshot what it began on.
    journal: tuple[int, Verification] | None
    readable: tuple[int, UnreadableRecord | None] | None
    projection: tuple[int, Projection] | None
    # What this snapshot read once and hands out again, one state: every entry, as read, and
    # the dated projections folded from them, by date. A summary of nine aircraft reads the
    # ledger once and folds each date once, where it read and folded per aircraft and per date.
    ledger: list[Entry] | None = None
    dated: dict[datetime, Projection] = field(default_factory=dict)


@dataclass(frozen=True)
class _BeforeCommit:
    outside: int  # the writing connection's data_version: moves on outside commits only
    label: int  # the state's label right before the write


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
        # `_readers` so `close` can close every one of them; a thread finds its own, and the
        # snapshot it has open, through `_local`. A store in memory has no file to open another
        # connection on, so it has none, and no labelling connection either.
        self._local = threading.local()
        self._readers: list[sqlite3.Connection] = []
        self._readers_lock = threading.Lock()
        self._labeller: sqlite3.Connection | None = None
        self._label_lock = threading.Lock()
        self._own_commits = 0  # the label of a store in memory
        # The writing connection's data_version and the label, as read after the last own
        # commit; a write whose label before it is still that label skips reading the former.
        self._since: tuple[int, int] | None = None
        self._closed = False
        if path != ":memory:":
            self._db.execute("PRAGMA journal_mode = WAL")
            self._labeller = self._open_reader()
        with self._lock:
            self._db.executescript(SCHEMA)
            with self._db:
                journal.migrate(self._db)
        # The answers kept rather than computed on every call: the whole-ledger projection,
        # the first record that cannot be read (`readable`, behind /health) and the last walk
        # of the journal (`journal_state`, behind the records summaries), each labelled.
        self._projection: _Kept[Projection] = _Kept()
        self._unreadable: _Kept[UnreadableRecord | None] = _Kept()
        self._journal: _Kept[Verification] = _Kept()

    def close(self) -> None:
        """Close every connection: each thread's reading connection and the labelling one
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

    # ---- connections and snapshots ---------------------------------------------------

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

    def _label(self) -> int:
        """The label of the state the file holds now: the labelling connection's
        `PRAGMA data_version`, which moves on every commit to the file by any connection and
        on nothing else, read under its own lock so that no two threads step its statement."""
        assert self._labeller is not None
        with self._label_lock:
            return int(self._labeller.execute("PRAGMA data_version").fetchone()[0])

    @contextlib.contextmanager
    def snapshot(self) -> Iterator[None]:
        """One state of the store for every read made inside: the rows, the projection and
        the kept journal result alike. A response built from several reads opens one around
        them (the records summaries and the evidence pack do), and a write landing meanwhile,
        through this store or from any other connection, is in the next snapshot and not in
        half of this one.

        On a file store it is one read transaction on this thread's own connection, under WAL
        a snapshot that holds no write lock and that no write waits for, labelled with the
        state it reads (see the module's docstring); nested use on the same thread joins the
        open snapshot. On a store in memory it holds the write lock for its length, so a
        write issued meanwhile waits for it. Nothing is written; the transaction ends with a
        rollback."""
        depth: int = getattr(self._local, "depth", 0)
        if depth:
            self._local.depth = depth + 1
            try:
                yield
            finally:
                self._local.depth = depth
            return
        if self._labeller is None:
            with self._lock:
                self._local.snapshot = self._capture(self._own_commits)
                self._local.depth = 1
                try:
                    yield
                finally:
                    self._local.depth = 0
                    self._local.snapshot = None
            return
        connection = self._reader()
        label = self._begin(connection)
        self._local.snapshot = self._capture(label)
        self._local.depth = 1
        try:
            yield
        finally:
            self._local.depth = 0
            self._local.snapshot = None
            # Nothing was written; this ends the snapshot. A connection closed meanwhile (the
            # store was closed under a running read) has nothing left to end.
            with contextlib.suppress(sqlite3.ProgrammingError):
                connection.execute("ROLLBACK")

    def _begin(self, connection: sqlite3.Connection) -> int:
        """Begin one read transaction on `connection` and return its label: the state's
        label is read before and after the transaction's first read, and the transaction
        stands only when the two agree, so no commit landed between them and the state the
        transaction pinned is the one the label names."""
        for _ in range(1000):
            before = self._label()
            connection.execute("BEGIN")
            # The first read pins the snapshot; a bare BEGIN pins nothing.
            connection.execute("PRAGMA data_version").fetchone()
            if self._label() == before:
                return before
            connection.execute("ROLLBACK")
        raise RuntimeError("the store never stood still long enough to read one state of it")

    def _capture(self, label: int) -> _Snapshot:
        return _Snapshot(
            label=label,
            journal=self._journal.value,
            readable=self._unreadable.value,
            projection=self._projection.value,
        )

    @contextlib.contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        """The connection to read on, inside this thread's open snapshot or one opened for
        this read alone."""
        with self.snapshot():
            yield self._db if self._labeller is None else self._reader()

    def _kept[T](
        self, kept: _Kept[T], captured: tuple[int, T] | None, compute: Callable[[], T]
    ) -> T:
        """The kept answer for this thread's open snapshot: the store's when its label is
        the snapshot's, the one captured when the snapshot began when that is, and otherwise
        computed now inside the snapshot, once per state (the lock), and kept unless an
        answer for a newer state is kept already."""
        snapshot: _Snapshot = self._local.snapshot
        label = snapshot.label
        current = kept.value
        if current is not None and current[0] == label:
            return current[1]
        if captured is not None and captured[0] == label:
            return captured[1]
        with kept.lock:
            current = kept.value
            if current is not None and current[0] == label:
                return current[1]
            answer = compute()
            latest = kept.value  # an own commit meanwhile may have moved it on past this label
            if latest is None or latest[0] <= label:
                kept.value = (label, answer)
            return answer

    # ---- own commits ------------------------------------------------------------------

    def _before_commit(self) -> _BeforeCommit:
        """Under the write lock, before an own write: what tells, after its commit, whether
        the kept answers described the state right before it. The writing connection's
        version is read only when the label has moved since the last own commit: an unmoved
        label says no connection committed since, so that version is as it was then."""
        if self._labeller is None:
            return _BeforeCommit(0, self._own_commits)
        label = self._label()
        if self._since is not None and self._since[1] == label:
            return _BeforeCommit(self._since[0], label)
        # The writing connection's version first, the label after it: a commit from outside
        # landing between the two is then in the label and missing from the version, so the
        # version read after the write differs and the kept answers are dropped. Read the
        # other way round, it was in the version and not in the label, and the kept answers
        # of the state before it were moved on over it.
        outside = _data_version(self._db)
        return _BeforeCommit(outside, self._label())

    def _after_commit(self, before: _BeforeCommit, head: str | None, entries: bool) -> None:
        """Under the write lock, after an own commit went through: move the kept answers on
        to the state it made, with nothing computed, when they described the state right
        before it, and drop them otherwise, so that the next snapshot computes them.

        On a file store the writing connection's `PRAGMA data_version` moves on another
        connection's commit and never on the store's own: read before the write and again
        after the new label is read, it says whether any other commit landed from before the
        write to after the label; when none did, the label read in between names exactly the
        state this commit made, and the state labelled before the write (which the kept
        answers' label is compared with) was the one right before it; the read before the
        write is spared when the label has not moved since the last own commit, since then
        nothing committed and the version read after that commit still stands. A journal link this
        store appended leaves an intact chain intact with one more link; a broken one it
        leaves to the next walk. A record this store wrote is one it wrote readable. The
        projection is dropped when an entry was written and kept when only a flight or an
        aircraft was."""
        if self._labeller is None:
            self._own_commits += 1
            after, exact = self._own_commits, True
        else:
            after = self._label()
            outside = _data_version(self._db)
            exact = outside == before.outside
            self._since = (outside, after)
        kept = self._journal.value
        if not exact or kept is None or kept[0] != before.label:
            self._journal.value = None
        elif head is None:
            self._journal.value = (after, kept[1])
        elif kept[1].ok:
            length = kept[1].length + 1
            self._journal.value = (
                after,
                Verification(
                    ok=True,
                    head=head,
                    length=length,
                    broken=None,
                    given=None,
                    given_at=None,
                    migration=kept[1].migration,
                    line=journal.line(True, head, length, None, None, None),
                ),
            )
        else:
            self._journal.value = None
        readable = self._unreadable.value
        if exact and readable is not None and readable[0] == before.label:
            self._unreadable.value = (after, readable[1])
        else:
            self._unreadable.value = None
        projection = self._projection.value
        if exact and not entries and projection is not None and projection[0] == before.label:
            self._projection.value = (after, projection[1])
        else:
            self._projection.value = None

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
        with self._lock:
            before = self._before_commit()
            with self._db:
                self._db.execute(
                    f"INSERT OR REPLACE INTO aircraft ({AIRCRAFT_COLUMNS}) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        aircraft.key,
                        aircraft.label,
                        aircraft.source,
                        int(aircraft.synthetic),
                        aircraft.licence,
                        aircraft.attribution,
                    ),
                )
            self._after_commit(before, None, entries=False)

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
                before = self._before_commit()
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
                self._after_commit(before, head, entries=False)
        except sqlite3.IntegrityError as exc:
            raise DuplicateFlight(f"{aircraft_key}/{record.log_ref} is already stored") from exc

    # ---- the ledger ------------------------------------------------------------------

    def append_entry(self, entry: Entry) -> Entry:
        """Insert one entry and return it with its id. Validation is the ledger's job."""
        with self._lock:
            before = self._before_commit()
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
            # Only once the commit has gone through: a commit refused is rolled back.
            self._after_commit(before, head, entries=True)
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
        """Every entry, superseded ones included, in the order they were entered. The whole
        ledger is read once per snapshot, and each call gets its own list of it."""
        with self._read() as db:
            if subject is not None:
                return self._entries(db, subject)
            snapshot: _Snapshot = self._local.snapshot
            if snapshot.ledger is None:
                snapshot.ledger = self._entries(db)
            return list(snapshot.ledger)

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

        The walk is on this thread's reading connection, inside one snapshot: under WAL
        that is one state of the file, and the store's writes go on meanwhile (issue #44); a
        write that lands during the walk is in the next one. A store in memory has one
        connection and walks it under the write lock, as before.
        """
        with self._read() as db:
            return journal.verify(db, head)

    def journal_state(self) -> Verification:
        """The result of the last walk of the journal, as `verify_journal()` with no head
        computes it, for the reads anyone may make (the records summaries), describing
        exactly the state the calling snapshot reads.

        The journal is walked once per state the kept result is asked for: never for a call
        on a state it was walked for, never for a write made through this store (the link the
        store computed itself moves the head and the length on when the chain verified before
        it, and makes the next read walk again when it did not), and once for the first
        snapshot after a commit from outside the store. So asking again, which anyone who can
        reach the service may do, never walks the journal.
        """
        with self._read() as db:
            snapshot: _Snapshot = self._local.snapshot
            return self._kept(self._journal, snapshot.journal, lambda: journal.verify(db))

    def readable(self) -> UnreadableRecord | None:
        """The first stored record that cannot be read, or None when every entry reads and
        projects and every flight record reads; /health reports what it returns.

        The whole store is read once per state it is asked for, as `journal_state` walks the
        journal: never for a call, never for a record this store writes, which it wrote
        readable, and once for the first snapshot after a commit from outside (an edit made
        with sqlite3, or a repair).
        """
        with self._read() as db:
            snapshot: _Snapshot = self._local.snapshot
            return self._kept(
                self._unreadable, snapshot.readable, lambda: self._first_unreadable(db)
            )

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

        The whole-ledger projection is kept, labelled with the state its entries were read
        in, and computed again once per state an entry was written in or a commit from
        outside made; a dated one is folded from the entries of the calling snapshot once per
        date that snapshot asks for, and kept no longer than the snapshot.
        """
        if as_of is not None:
            with self._read():
                dated = self._local.snapshot.dated
                found: Projection | None = dated.get(as_of)
                if found is None:
                    found = dated[as_of] = project(self.entries(), as_of)
                return found
        with self._read() as db:
            snapshot: _Snapshot = self._local.snapshot
            return self._kept(
                self._projection, snapshot.projection, lambda: project(self._entries(db))
            )

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


def _data_version(connection: sqlite3.Connection) -> int:
    return int(connection.execute("PRAGMA data_version").fetchone()[0])


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
