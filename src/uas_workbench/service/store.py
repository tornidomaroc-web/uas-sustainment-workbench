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
(a `uasw record` beside a running service) waits that long for the write lock, then is
refused as `StoreBusy` with its transaction rolled back, so nothing of it is written; a
checked ledger write takes that lock before its check (`writing`), so it is refused with
nothing checked. An open that has to write (the first of a store 0.5.x left) waits and is
refused the same way. While a store is open its file has `-wal` and `-shm` beside
it; `close` closes every reading connection, checkpoints the WAL into the file and closes the
writing one, so when it was the last store on the file SQLite removes both and a closed store is
one file. A store opened for reads only (`reads_only`, the commands that only read) never
checkpoints, and beside a `-wal` holding anything as it opens (one a hard stop left, or a
running service's) none of its connections checkpoints as it closes either, so it leaves that
`-wal` as it found it (issue #107). A store in memory has no file, keeps the mode SQLite gives
it, and has one connection: a snapshot there holds the write lock for its length, its label is
the count of the store's own commits, and a write issued meanwhile waits for it.

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
import os
import sqlite3
import stat
import struct
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from uas_workbench.fleet.model import Aircraft, Fleet
from uas_workbench.flight.codec import record_from_json, record_to_json
from uas_workbench.flight.record import FlightRecord, Source, is_known
from uas_workbench.ledger.codec import entry_from_json, entry_to_json
from uas_workbench.ledger.model import Entry, LedgerError, Projection, UnreadableRecord
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
# How many of the store's own commits the WAL takes before the store folds it into the file
# (`_checkpoint`): about the 1,000 pages SQLite checkpoints at, at a few pages a write.
CHECKPOINT_COMMITS = 200
# A damaged store file is checked again no sooner than this many times as long as its last
# check took after that check ended, and never sooner than a second (`_watch`, issue #113): so
# the checks take at most a tenth of one core, where a second's wait let them take most of one.
RECHECK_TIMES = 9
# Longer than the clock's tick the time of last change moves with (`Looked.still`).
TICK_NS = 100_000_000
# The file as last seen, for a store whose file may have changed since its check at rest: no
# look equals it, so the store's first look checks the file (`Store.__init__`).
SINCE_CHECKED = "not looked at since its check at rest"
# A checked write refused because another writer held the store past that wait (`writing`).
BUSY = (
    "another write held the store for longer than this one waits; nothing was written, "
    "so send it again"
)
# An open that had to write (a store 0.5.x left, switched to WAL and journaled on its first
# open) refused because another connection held the write lock past that wait.
BUSY_OPEN = (
    "another connection held the store's write lock for longer than opening the store "
    "waits; nothing was written, so run it again"
)


class DuplicateFlight(Exception):
    pass


class StoreBusy(Exception):
    """A write, or an open that has to write, that waited the busy timeout for another
    connection's write lock and was refused with nothing written (issues #69, #70): the
    service answers 503 with the sentence, the command line one `refused (503)` line."""

    status = 503

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class StoreDamaged(Exception):
    """The store file was changed while the store had it open, in a way SQLite did not make,
    and does not pass SQLite's check; or it was replaced or removed (part of issue #73). Every
    read and write is refused (the service answers 503 with the sentence) until the file is
    restored in place; a replaced or removed file needs the service restarted."""

    status = 503

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


@dataclass(frozen=True)
class _FileState:
    """What one `stat` says of the store file: which file it is, its size, when it was last
    changed and its permissions. SQLite's own writes to it are checkpoints: the store's own,
    which it makes and looks for itself (`_checkpoint`), and any other connection's."""

    identity: tuple[int, int]
    size: int
    changed_ns: int
    mode: int


def _file_state(path: str) -> _FileState | None:
    """The store file as it is now, or None when nothing is at `path`."""
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return None
    return _FileState((st.st_dev, st.st_ino), st.st_size, st.st_mtime_ns, stat.S_IMODE(st.st_mode))


@dataclass(frozen=True)
class Looked:
    """The store file as one `stat` saw it right before its check at rest began, and when, by
    the system clock (`look`, issue #117)."""

    state: _FileState | None
    at_ns: int

    def still(self, now: _FileState | None) -> bool:
        """Whether the file `now` describes is the one looked at, with nothing a `stat` shows
        changed since: the same `stat`, of a file last changed more than `TICK_NS` before the
        look. A file's time of last change moves with the clock's tick (about 15 ms on
        Windows), so a change landing within the tick of the last one can leave the `stat` as
        it was (issue #95); one last changed longer before the look cannot."""
        if now != self.state:
            return False
        return now is None or now.changed_ns < self.at_ns - TICK_NS


def look(path: str) -> Looked:
    """The file at `path` as it is now, taken before its check at rest (`checked_at_rest`) and
    given to the store opened after it; a file that cannot be looked at is never `still`."""
    at = time.time_ns()
    try:
        return Looked(_file_state(path), at)
    except OSError:
        return Looked(_FileState((-1, -1), -1, -1, -1), at)


def _busy(exc: sqlite3.OperationalError) -> bool:
    """Whether SQLite refused because another connection held the lock (the result code,
    never the message, which differs between builds)."""
    code = getattr(exc, "sqlite_errorcode", None)  # absent when raised by Python code
    return code is not None and code & 0xFF == sqlite3.SQLITE_BUSY


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
    def __init__(
        self, path: str = ":memory:", *, reads_only: bool = False, looked: Looked | None = None
    ) -> None:
        self._path = path
        # Opened for reads only, the store never checkpoints (`close`), and beside a `-wal`
        # holding anything as it opens, every connection it opens is told not to checkpoint as
        # it closes, which SQLite does when the last connection to the file closes: a read
        # folded a `-wal` a hard stop left into a damaged file and removed it (issue #107).
        # Beside none, or an empty one, its reads add nothing to fold, and the last connection
        # to close removes the side files, so a closed store is still one file.
        self._reads_only = reads_only
        self._keep_wal = reads_only and path != ":memory:" and _holds_wal(path)
        # The writing connection, used under the write lock only. The lock is re-entrant: a
        # store in memory has this one connection, so its reads take the lock too, and a read
        # made while the lock is held (a write that reads first) must not wait for itself.
        self._db = self._open_writer()
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
        self._batching = False  # inside `batch`: writes join its one transaction
        self._uncheckpointed = 0  # own commits since the last own checkpoint (`_checkpoint`)
        # The store file as last seen, and what is wrong with it if anything (`_watch`). The
        # generation moves when the file changed outside SQLite and passed the check: every
        # connection opened before it is opened again before its next use, and it is part of
        # every label, so no answer kept from a connection of an older generation is used.
        self._file: _FileState | str | None = None
        self._identity: tuple[int, int] | None = None
        self._damaged: str | None = None
        self._generation = 0
        self._writer_generation = 0
        self._watch_lock = threading.Lock()
        self._checked_at = 0.0  # when the file was last checked (monotonic seconds)
        self._recheck_after = 1.0  # how long a damaged file waits for its next check, seconds
        # An open writes only on a store that needs it (one 0.5.x left: the switch to WAL, the
        # journal table, the migration); each waits for another connection's write lock up to
        # the busy timeout and, past it, the open is refused with nothing written and every
        # connection it opened closed, never left for the collector (issue #70).
        try:
            if path != ":memory:":
                self._wal()
                self._labeller = self._open_reader()
            with self._lock:
                self._db.executescript(SCHEMA)
                with self._db:
                    journal.migrate(self._db)
        except BaseException as exc:
            self.close()
            if isinstance(exc, sqlite3.OperationalError) and _busy(exc):
                raise StoreBusy(BUSY_OPEN) from exc
            raise
        if path != ":memory:":
            seen = _file_state(path)
            # Opened on a file checked at rest, the store takes the file as last seen only when
            # nothing a `stat` shows changed since the look taken before that check (`looked`);
            # otherwise its first look checks the file, so a change landing while that check
            # ran, or before the store was open, is checked as one from outside. A batch that
            # writes nothing no longer checks the file again (issue #117), and that second
            # check was what caught such a change before. An open that wrote (a store 0.5.x
            # left) changed the file itself, and its first look checks it too.
            self._file = seen if looked is None or looked.still(seen) else SINCE_CHECKED
            self._identity = seen.identity if seen is not None else None
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
        the same when that one closes. A store opened for reads only runs no checkpoint here,
        and leaves a `-wal` it found holding anything as it was (`reads_only`). Called once the
        threads that read are done (a server after it has stopped, a command when it ends); a
        read after it raises `sqlite3.ProgrammingError`, as on a closed connection. Closing a
        closed store does nothing."""
        self._closed = True
        with self._readers_lock:
            readers, self._readers = self._readers, []
        for reader in readers:
            reader.close()
        if not self._reads_only:
            with contextlib.suppress(sqlite3.Error):
                self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self._db.close()

    def _wal(self) -> None:
        """Put the file in WAL mode. On a file in the rollback-journal mode (a store 0.5.x
        left) the switch needs the write lock, and SQLite answers busy at once, without the
        busy timeout, when another connection holds it; so the switch is tried again until
        the busy timeout has passed, as every other write waits. On a file in WAL already it
        changes nothing and takes no lock."""
        deadline = time.monotonic() + BUSY_TIMEOUT_MS / 1000
        while True:
            try:
                self._db.execute("PRAGMA journal_mode = WAL")
                return
            except sqlite3.OperationalError as exc:
                if not _busy(exc) or time.monotonic() >= deadline:
                    raise
                time.sleep(0.02)

    # ---- connections and snapshots ---------------------------------------------------

    def _open_writer(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, check_same_thread=False)
        if self._keep_wal:
            connection.setconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE, True)
        connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA synchronous = FULL")
        # The store folds the WAL into the file itself (`_checkpoint`), so that it knows each
        # change it makes to the file (issue #79).
        connection.execute("PRAGMA wal_autocheckpoint = 0")
        return connection

    # ---- the store file, changed outside SQLite ---------------------------------------

    def _watch(self) -> None:
        """Before every read and every write on a file store: one `stat` of the store file.
        SQLite does not see a file changed outside it, and its connections go on serving the
        pages they hold in memory; so when the file is not as last seen, it is checked
        (`_damage_in`). Damaged, replaced or removed, every read and write is refused as
        `StoreDamaged` until the file changes again and passes; sound (a commit and checkpoint
        by another connection, a file restored in place), the generation moves, so every
        connection is opened afresh before its next use and every kept answer is computed
        again. The store's own checkpoints are looked at as it makes them (`_checkpoint`), under
        the same lock. Takes a lock of its own, never the write lock."""
        if self._labeller is None:
            return
        if self._damaged is None and self._seen() == self._file:
            return
        with self._watch_lock:
            now = self._seen()
            # A file's time of last change moves with the clock's tick (about 15 ms on
            # Windows), so a restore landing within the tick of the damage, at the same size,
            # looks unchanged: a damaged file is looked at again regardless, once the wait its
            # last check set has passed (`_judged`).
            again = (
                self._damaged is not None
                and time.monotonic() - self._checked_at >= self._recheck_after
            )
            if now != self._file or again:
                began = time.monotonic()
                damaged = self._damage_in(now)
                if damaged is None:
                    # Every label carries the generation, so no answer kept before is used.
                    self._generation += 1
                # The file as seen only once its verdict is in: until then every other
                # thread's look misses, and it waits here for the verdict rather than reading
                # or writing the changed file meanwhile.
                self._file = now
                self._judged(damaged, began)
            damaged = self._damaged
        if damaged is not None:
            raise StoreDamaged(damaged)

    def _judged(self, damaged: str | None, began: float) -> None:
        """Under the lock every look at the file takes, after a check of the file that began
        at `began` (monotonic seconds): its verdict, when it ended, and how long a damaged file
        then waits for its next check, which grows with the check's own time (issue #113). The
        wait is timed from the end of the check, so the requests that waited for its verdict do
        not run it again, and it is `RECHECK_TIMES` the check's time, never less than a second,
        so a damaged store spends at most a tenth of one core on its checks however long one
        takes, where a wait of one second let a 4.3 s check take 84 to 86 % of one."""
        ended = time.monotonic()
        self._damaged = damaged
        self._checked_at = ended
        self._recheck_after = max(1.0, RECHECK_TIMES * (ended - began))

    def _seen(self) -> _FileState | str | None:
        try:
            return _file_state(self._path)
        except OSError as exc:
            return f"cannot be looked at ({exc.strerror or exc})"

    def _damage_in(self, now: _FileState | str | None) -> str | None:
        """What is wrong with the store file as `now` describes it, or None when it is sound."""
        if now is None:
            return "the store file was removed while the service has it open; restart the service"
        if isinstance(now, str):
            return f"the store file {now}"
        if now.identity != self._identity:
            return (
                "the store file was replaced while the service has it open; restart the "
                "service to use the file now there"
            )
        try:
            check = _check_connection(self._path)
            try:
                problem = _sqlite_check(check, "integrity_check")
            finally:
                check.close()
        except sqlite3.Error as exc:
            problem = str(exc)
        if problem is None:
            return None
        return (
            f"the store file was changed outside the service and fails SQLite's integrity "
            f"check ({' '.join(problem.split())}); restore it from a good copy"
        )

    def check_whole(self) -> None:
        """SQLite's `integrity_check` on the store file whole, on a connection of its own, as
        `uasw verify` runs it before its walk; `GET /journal/verify` runs it before its walk
        (issue #107). The look at the file before every read sees only a file whose `stat`
        changed, and a change within the clock tick of the last one, at the same size, leaves
        it as it was. A file that fails is refused from then on as one damaged under the store
        (`StoreDamaged`), every read and write alike, until it changes again and passes. Takes
        the lock every look at the file takes, never the write lock."""
        if self._labeller is None:
            return
        self._watch()
        with self._watch_lock:
            began = time.monotonic()
            damaged = self._damage_in(self._seen())
            if damaged is not None:
                self._judged(damaged, began)
        if damaged is not None:
            raise StoreDamaged(damaged)

    def _ready_to_write(self) -> None:
        """Under the write lock, before a write that opens its own transaction: the file is
        looked at, and the writing connection opened afresh when the file changed outside."""
        if self._labeller is None or self._db.in_transaction:
            return
        self._watch()
        if self._writer_generation != self._generation:
            stale, self._db = self._db, self._open_writer()
            self._writer_generation = self._generation
            self._since = None
            with contextlib.suppress(sqlite3.Error):
                stale.close()

    def _open_reader(self) -> sqlite3.Connection:
        """A connection for reads on the store file: autocommit, so that `BEGIN` and
        `ROLLBACK` below bound exactly one read transaction; query-only, so nothing is ever
        written through it; closable from another thread, which is how `close` closes it."""
        connection = sqlite3.connect(self._path, check_same_thread=False, isolation_level=None)
        if self._keep_wal:
            connection.setconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE, True)
        connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        connection.execute("PRAGMA query_only = ON")
        with self._readers_lock:
            if self._closed:
                connection.close()
                raise sqlite3.ProgrammingError("Cannot operate on a closed database.")
            self._readers.append(connection)
        return connection

    def _reader(self) -> sqlite3.Connection:
        """This thread's own reading connection, opened on its first read, and again when
        the store file changed outside SQLite since it was opened (`_watch`)."""
        connection: sqlite3.Connection | None = getattr(self._local, "reader", None)
        if connection is not None and self._local.generation != self._generation:
            with self._readers_lock:
                if connection in self._readers:
                    self._readers.remove(connection)
            with contextlib.suppress(sqlite3.Error):
                connection.close()
            connection = None
        if connection is None:
            generation = self._generation
            connection = self._open_reader()
            self._local.reader = connection
            self._local.generation = generation
        return connection

    def _label(self, generation: int) -> int:
        """The label of the state the file holds now, as seen by a connection of
        `generation`: the labelling connection's `PRAGMA data_version`, which moves on every
        commit to the file by any connection and on nothing else, read under its own lock so
        that no two threads step its statement, with the generation above it, so a label of a
        later generation is always the greater and never equal to an earlier one."""
        assert self._labeller is not None
        with self._label_lock:
            version = int(self._labeller.execute("PRAGMA data_version").fetchone()[0])
        return (generation << 40) | version

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
        self._watch()
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
        generation: int = self._local.generation
        for _ in range(1000):
            before = self._label(generation)
            connection.execute("BEGIN")
            # The first read pins the snapshot; a bare BEGIN pins nothing.
            connection.execute("PRAGMA data_version").fetchone()
            if self._label(generation) == before:
                return before
            connection.execute("ROLLBACK")
        # Refused as SQLite's own refusals are, one line in a command and 503 in the service,
        # never a traceback; a `-shm` that cannot be written is the cause met (issue #112).
        raise sqlite3.OperationalError(
            unwritable_side_files(self._path)
            or "the store never stood still long enough to read one state of it"
        )

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
        label = self._label(self._writer_generation)
        if self._since is not None and self._since[1] == label:
            return _BeforeCommit(self._since[0], label)
        # The writing connection's version first, the label after it: a commit from outside
        # landing between the two is then in the label and missing from the version, so the
        # version read after the write differs and the kept answers are dropped. Read the
        # other way round, it was in the version and not in the label, and the kept answers
        # of the state before it were moved on over it.
        outside = _data_version(self._db)
        return _BeforeCommit(outside, self._label(self._writer_generation))

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
        aircraft was. Inside a batch nothing is committed yet, so nothing moves; the batch
        moves everything on when it ends."""
        if self._batching:
            return
        if self._labeller is None:
            self._own_commits += 1
            after, exact = self._own_commits, True
        else:
            after = self._label(self._writer_generation)
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
        self._uncheckpointed += 1
        if self._uncheckpointed >= CHECKPOINT_COMMITS:
            self._checkpoint()

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
            self._ready_to_write()
            before = self._before_commit()
            with self._committing():
                self._db.execute(
                    f"INSERT OR REPLACE INTO aircraft ({AIRCRAFT_COLUMNS}) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    _aircraft_row(aircraft),
                )
            self._after_commit(before, None, entries=False)

    def add_flight(self, aircraft_key: str, record: FlightRecord) -> None:
        self._add_flight(aircraft_key, record, None)

    def add_log(self, aircraft_key: str, record: FlightRecord) -> None:
        """Store one ingested flight, with its aircraft's row when the store does not hold
        one yet, in one transaction: both are written or neither is, so a write refused
        midway leaves no aircraft without its flight (issue #69)."""
        self._add_flight(aircraft_key, record, _aircraft_of(aircraft_key, record))

    def _add_flight(
        self, aircraft_key: str, record: FlightRecord, aircraft: Aircraft | None
    ) -> None:
        utc = record.utc_start.isoformat() if is_known(record.utc_start) else None
        text = json.dumps(record_to_json(record))
        try:
            with self._lock:
                self._ready_to_write()
                before = self._before_commit()
                with self._committing():
                    if aircraft is not None:
                        self._db.execute(
                            f"INSERT OR IGNORE INTO aircraft ({AIRCRAFT_COLUMNS}) "
                            "VALUES (?, ?, ?, ?, ?, ?)",
                            _aircraft_row(aircraft),
                        )
                    cursor = self._db.execute(
                        "INSERT INTO flights (aircraft_key, log_ref, synthetic, utc_start, record) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (aircraft_key, record.log_ref, int(record.synthetic), utc, text),
                    )
                    assert cursor.lastrowid is not None
                    head = journal.append_link(self._db, "flight", int(cursor.lastrowid))
                # Only once the commit has gone through: a commit refused is rolled back.
                self._after_commit(before, head, entries=False)
        except sqlite3.IntegrityError as exc:
            raise DuplicateFlight(f"{aircraft_key}/{record.log_ref} is already stored") from exc

    @contextlib.contextmanager
    def batch(self) -> Iterator[None]:
        """Every write made inside, through this store on this thread, in one transaction:
        committed together when the block ends and rolled back together when it raises, so a
        batch that fails or is refused partway writes nothing (a seed, issue #72).

        The store's lock and then, on a store file, SQLite's write transaction
        (`BEGIN IMMEDIATE`) are held for its length, in that order, as `writing` takes them;
        another connection holding the write lock past the busy timeout gets the batch
        refused as `StoreBusy` before anything is written. Reads on other threads go on beside
        it on their own connections and do not see it until it commits. The kept answers are
        dropped when it ends, committed or not, and computed again on the next read. A batch
        that wrote something is folded into the file as it ends (`_checkpoint`), which checks
        the file; one that wrote nothing (a seed that finds the store full) changed nothing in
        the file and runs neither, where it checked the file a second time as `serve` and
        `seed` started (issue #117). A batch is not nested and does not run inside a snapshot
        this thread holds open."""
        if getattr(self._local, "depth", 0):
            raise RuntimeError("a batch of writes cannot run inside an open snapshot")
        with self._lock:
            if self._batching or self._db.in_transaction:
                raise RuntimeError("a batch of writes cannot be nested")
            self._ready_to_write()
            try:
                self._db.execute("BEGIN" if self._labeller is None else "BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                if _busy(exc):
                    raise StoreBusy(BUSY) from exc
                raise
            self._batching = True
            changes, wrote = self._db.total_changes, False
            try:
                yield
                # Whether the batch wrote anything, by SQLite's own count of the rows this
                # connection inserted, updated or deleted, those its triggers and foreign keys
                # changed included. Only this connection writes inside the batch's write
                # transaction, and the store writes no schema in one, so an unmoved count is a
                # commit that adds no frame to the WAL and leaves the file as it was.
                wrote = self._db.total_changes != changes
                self._db.commit()
                if wrote:
                    # One own commit for the checkpoint below to fold, however many writes.
                    self._uncheckpointed += 1
            except BaseException:
                self._db.rollback()
                raise
            finally:
                self._batching = False
                # Nothing kept from inside the batch outlives it: a store in memory read the
                # batch's own writes there, under a label its rollback does not move.
                self._journal.value = None
                self._unreadable.value = None
                self._projection.value = None
            if wrote:
                self._checkpoint()

    def holds(self) -> tuple[int, int, int]:
        """How many aircraft, flights and entries the store holds, read in one snapshot."""
        with self.snapshot():
            return len(self.aircraft()), self.flight_count(), self.entry_count()

    @contextlib.contextmanager
    def _committing(self) -> Iterator[None]:
        """One transaction on the writing connection, committed at the end and rolled back on
        any error; a write that waited the busy timeout for another connection's lock is
        refused as `StoreBusy`, with the transaction, and so everything in it, rolled back.
        Inside a batch the write joins the batch's transaction, which commits or rolls back."""
        if self._batching:
            yield
            return
        try:
            with self._db:
                yield
        except sqlite3.OperationalError as exc:
            if _busy(exc):
                raise StoreBusy(BUSY) from exc
            raise

    def _checkpoint(self) -> None:
        """Under the write lock, after an own commit and once the kept answers have moved on:
        every `CHECKPOINT_COMMITS` own commits, and after a batch that wrote, fold the WAL into
        the store file (`PRAGMA wal_checkpoint(RESTART)`). It waits, up to the busy timeout, for the
        snapshots begun before the last commit to end, which are reads already under way;
        snapshots begun meanwhile read the folded file and are not waited for. So the whole WAL
        is folded and the next commit starts it over. A checkpoint that waits for no reader
        (`PASSIVE`) is folded whole only when no snapshot is open, which threads reading
        without pause rarely leave: the `-wal` file then grew with every write, without
        bound. The writing connection never checkpoints on its own
        (`wal_autocheckpoint` is 0), so this is the only change the store makes to its file
        while it is open (issue #79): SQLite checkpointed on the commit that took the WAL past
        about 1,000 pages, which was the store's own and looked like a change from outside,
        and on nearly every commit while readers kept the WAL from starting over.

        The file is looked at right before and right after, under the lock every look at it
        takes, so no other thread meets the change as one from outside; a reader that meets it
        waits for this verdict, as for any other. The file is checked once, here
        (`_damage_in`), when the two looks differ and whenever the checkpoint had own commits
        to fold, whether or not the looks differ: the checkpoint's write stamps the file with
        the system clock, whose tick is about 15 ms on Windows, and one that writes no page
        past the end of the file within the tick of the change last seen leaves both looks
        equal (issue #95). A change from outside landing during the checkpoint is in that file
        too, so damage is refused from then on, as before. Sound, the file is recorded as seen
        and nothing else happens: no generation moves, no connection is opened afresh and no
        kept answer is computed again, since SQLite made the change and every connection of
        the store sees it through SQLite. A file that changed before the first look, which no
        look has met yet, or that is refused, is not written to: the checkpoint waits for the
        next commit, whose look checks that change as from outside. A checkpoint SQLite
        refuses is left for the next one, and with nothing to fold (no own commit since the
        last one) a file that looks as last seen is not checked."""
        if self._labeller is None:
            self._uncheckpointed = 0
            return
        if self._db.in_transaction:
            return
        with self._watch_lock:
            before = self._seen()
            if self._damaged is not None or before != self._file:
                return
            folded, self._uncheckpointed = self._uncheckpointed, 0
            with contextlib.suppress(sqlite3.Error):
                self._db.execute("PRAGMA wal_checkpoint(RESTART)").fetchall()
            after = self._seen()
            if after == before and not folded:
                return
            began = time.monotonic()
            damaged = self._damage_in(after)
            self._file = after
            self._judged(damaged, began)

    # ---- the ledger ------------------------------------------------------------------

    def append_entry(self, entry: Entry) -> Entry:
        """Insert one entry and return it with its id. Validation is the ledger's job."""
        with self._lock:
            self._ready_to_write()
            before = self._before_commit()
            with self._committing():
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
        lands between them: two requests served at once, or two processes writing to one file
        at once, are then checked as if made one after the other (`ledger.append`).

        The store's own lock keeps out the other threads of this process. On a file store the
        check also runs inside the write transaction (`BEGIN IMMEDIATE` on the writing
        connection), SQLite's lock on the file, which keeps out every other connection, those
        of another process included (issue #65): a second writer waits for the first to commit,
        up to the busy timeout, and is checked with the first in the records. The locks are
        always taken in that order, the store's then the file's, and nothing that holds the
        file's waits for the store's. The write commits the transaction; a check that refuses
        rolls it back with nothing written. A writer that cannot get the transaction within the
        busy timeout is refused (503) before anything is checked or written. Reads go on
        meanwhile on their own connections, under WAL on a snapshot that waits for no writer.

        The check reads the store as it is now, so it cannot run inside a snapshot this thread
        holds open, which reads the state the snapshot began on."""
        if getattr(self._local, "depth", 0):
            raise RuntimeError("a write cannot be checked inside an open snapshot")
        with self._lock:
            if self._labeller is None or self._db.in_transaction:
                yield
                return
            self._ready_to_write()
            try:
                self._db.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                if not _busy(exc):
                    raise
                raise LedgerError(503, BUSY) from exc
            try:
                yield
            finally:
                if self._db.in_transaction:
                    self._db.rollback()

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


def _aircraft_of(key: str, record: FlightRecord) -> Aircraft:
    """The aircraft row an ingested log gives an aircraft the store does not hold yet."""
    return Aircraft(
        key=key,
        label=key,
        source=record.source,
        synthetic=record.synthetic,
        licence=record.licence,
        attribution=record.attribution,
    )


def _aircraft_row(aircraft: Aircraft) -> tuple[object, ...]:
    return (
        aircraft.key,
        aircraft.label,
        aircraft.source,
        int(aircraft.synthetic),
        aircraft.licence,
        aircraft.attribution,
    )


def _holds_wal(path: str) -> bool:
    """Whether a `-wal` holding anything, its header alone included, is beside the file."""
    try:
        return os.path.getsize(path + "-wal") > 0
    except OSError:
        return False


def _cannot_write(path: str) -> str | None:
    """Why the file at `path` cannot be opened for writing, or None when it can or is not
    there. It is opened and closed with nothing written, so its bytes and its time of last
    change stay as they were."""
    try:
        handle = os.open(path, os.O_RDWR | getattr(os, "O_BINARY", 0))
    except FileNotFoundError:
        return None
    except OSError as exc:
        return exc.strerror or str(exc)
    os.close(handle)
    return None


def unwritable_side_files(path: str) -> str | None:
    """Why no read of the store at `path` can stand, when its `-shm` cannot be written, or None
    (issue #112). The `-shm` is SQLite's index of the `-wal`; a connection that cannot write it
    keeps the index in its own memory instead, and then every read on any connection is a new
    state of the store (`PRAGMA data_version` moves on each one), so no snapshot stands
    (`Store._begin`). A store file marked read-only leaves both side files beside it after any
    read, since SQLite cannot remove them, and marked read-only too they stopped every command.
    A `-wal` that cannot be written is named beside the `-shm`, and alone stops no read."""
    shm = _cannot_write(path + "-shm")
    if shm is None:
        return None
    wal = _cannot_write(path + "-wal")
    name = Path(path).name  # the side files sit beside the store file the command was given
    also = f", nor can {name}-wal ({wal})" if wal is not None else ""
    return (
        f"the store's side file {name}-shm cannot be written ({shm}){also}; a read must write "
        "the -shm to hold one state of the store, so nothing was read from it or written to it; "
        "make the side files writable, then run it again"
    )


def _check_connection(path: str) -> sqlite3.Connection:
    """A connection of its own to the file at `path`, for SQLite's check; it writes nothing."""
    uri = Path(path).resolve().as_uri() + "?mode=rw"
    check = sqlite3.connect(uri, uri=True)
    check.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    return check


def _sqlite_check(check: sqlite3.Connection, pragma: str) -> str | None:
    """SQLite's own check (`quick_check` or `integrity_check`) on `check`: the first problem
    it reports, or None when it answers ok. Raises what SQLite raises on a file it cannot read."""
    rows = check.execute(f"PRAGMA {pragma}(1)").fetchall()
    if rows == [("ok",)]:
        return None
    return str(rows[0][0]) if rows else "no answer"


@dataclass(frozen=True)
class AtRest:
    """What `checked_at_rest` found wrong with a store: SQLite's finding on one line; whether
    a `-wal` holding anything was beside the file and is kept as it was; and, when one was,
    whether the file alone, without it, passes `integrity_check`, so the damage is only in the
    `-wal` (issue #108); and, when it fails, whether the file holds part of what the `-wal`
    holds, as a fold cut short or held back leaves it, which can leave the file alone failing
    with nothing damaged (`_partly_folded`, #111)."""

    problem: str
    wal_kept: bool
    file_alone_passes: bool
    partly_folded: bool = False


def _partly_folded(path: str) -> bool:
    """Whether the file at `path` holds part of what its `-wal` holds and not the rest, as a
    fold of the `-wal` into the file leaves it when a hard stop cuts it short or a reader of an
    earlier state holds it back (issue #111): some page the `-wal` holds in committed frames
    is in the file exactly as one of its copies there, and some is not in the file as its
    latest copy. A checkpoint writes, in ascending page order, the latest copy of each page
    last written by the commit it folds up to: cut short, the file alone is part old and part
    new; held back by a reader, it folds only the pages that reader's state last wrote, and a
    page written again since keeps its older content in the file. Either way the file alone can
    fail SQLite's check with nothing damaged, while the store with its `-wal` reads every page
    the `-wal` holds from the `-wal`. Every page in the file as its latest copy is a fold
    finished whose frames stayed in the `-wal`, and none in the file as any copy is a fold not
    begun since the `-wal` started over (SQLite starts it over only once all of it is folded):
    either way the file alone is a store of its own.

    Frames count from the header up to the last commit frame, while their salts are the
    header's, as SQLite counts them; their checksums are not computed, which on a large `-wal`
    took seconds, so a frame SQLite would stop at for its checksum can only add a copy to
    compare. The file and the `-wal` are read a page at a time, the latest copy first and an
    older one only while no page has been met in the file as a copy, and nothing is written."""
    try:
        with open(path + "-wal", "rb") as wal:
            header = wal.read(32)
            if len(header) < 32:
                return False
            magic, _, size, _, salt1, salt2 = struct.unpack(">IIIIII", header[:24])
            if magic not in (0x377F0682, 0x377F0683) or size < 512 or size & (size - 1):
                return False
            pending: list[tuple[int, int]] = []
            pages: dict[int, list[int]] = {}  # each page's copies, oldest first
            at = 32
            while True:
                frame = wal.read(24)
                if len(frame) < 24:
                    break
                page, commit, s1, s2 = struct.unpack(">IIII", frame[:16])
                if (s1, s2) != (salt1, salt2):
                    break
                pending.append((page, at + 24))
                if commit:
                    for written, offset in pending:
                        pages.setdefault(written, []).append(offset)
                    pending = []
                at += 24 + size
                wal.seek(at)
            copied = waiting = False
            with open(path, "rb") as file:
                for page, offsets in pages.items():
                    file.seek((page - 1) * size)
                    held = file.read(size)
                    for offset in reversed(offsets if not copied else offsets[-1:]):
                        wal.seek(offset)
                        if len(held) == size and held == wal.read(size):
                            copied = True
                            waiting = waiting or offset != offsets[-1]
                            break
                        waiting = waiting or offset == offsets[-1]
                    if copied and waiting:
                        return True
    except OSError:
        return False
    return False


def _file_alone_check(path: str) -> str | None:
    """SQLite's `integrity_check` on the file's own pages, without the `-wal` beside it: the
    file is opened `immutable`, which SQLite reads read-only with no locks and without the
    `-wal` and `-shm`, so nothing beside the file is opened and nothing is written. A copy of
    the file in memory took twice its size and, at 2 GiB or more, raised MemoryError, past what
    SQLite allocates at once. Any failure to open or check the file is a problem."""
    uri = Path(path).resolve().as_uri() + "?mode=ro&immutable=1"
    try:
        alone = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        return str(exc)
    try:
        return _sqlite_check(alone, "integrity_check")
    except sqlite3.Error as exc:
        return str(exc)
    finally:
        alone.close()


@contextlib.contextmanager
def checked_at_rest(path: str) -> Iterator[AtRest | None]:
    """What is wrong with the existing store file at `path` by SQLite's
    `PRAGMA integrity_check`, run before a store is opened on it, or None when it passes (issues
    #104, #105). Every command runs it when it opens an existing store, those that only read
    included (issue #107), and `uasw verify` before the walk: the journal chain covers only the
    journaled rows, and a write's own reads only the pages they touch, so damage made at rest
    elsewhere in the file was written beside and reported intact. `integrity_check` rather than
    SQLite's quicker `quick_check`: it also finds an index whose keys no longer match their
    rows, which `quick_check` passes, for somewhat more time on a large store (LIMITS.md gives
    both). It writes nothing.

    The check's connection stays open while the block runs, so a store opened inside it
    shares the `-shm` file it made. On a file this process may not write, SQLite makes `-shm`
    with the file's own permissions and cannot remove it when the last connection closes; a
    connection opened after that cannot write it and saw every read as a new state of the store
    ("never stood still"; met on Linux CI, where a read-only store is mode 0400).

    A `-wal` holding anything beside the file (a hard stop leaves one, with its `-shm`), its
    header alone included, is left as it was: SQLite folds the WAL into the file and removes
    the side files when its last connection closes, and on a refusal that was the check's,
    which wrote to the damaged file and took away the `-wal`. An empty `-wal`, or none, holds
    nothing to fold in, and the side files are removed when the check closes, as before. The
    `-shm` is SQLite's index of the `-wal`, and the check's read rebuilds it. When such a `-wal`
    is kept and the check fails, the file alone is checked too, so the refusal can say whether
    the damage is only in the `-wal`: the `-wal` belongs to the damaged store, and a good copy
    put beside it would take its pages in (issue #108); SQLite records nothing in the file that
    ties a `-wal` to it, so a `-wal` left beside another file cannot be told apart. When the file
    alone fails too, whether it holds part of what the `-wal` holds is looked at
    (`_partly_folded`): then the file alone can fail with nothing damaged, and where the damage
    is cannot be told (issue #111).

    A file SQLite finds corrupt or not a database is damaged, with SQLite's sentence; any other
    error (the path is a folder, or cannot be opened) is not judged here and is left to the
    open, which refuses it as before. SQLite's report can span lines; it is given on one."""
    left_beside = _holds_wal(path)
    try:
        check = _check_connection(path)
    except sqlite3.Error:
        yield None
        return
    try:
        if left_beside:
            check.setconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE, True)
        try:
            problem = _sqlite_check(check, "integrity_check")
        except sqlite3.DatabaseError as exc:
            code = getattr(exc, "sqlite_errorcode", None)  # absent when raised by Python code
            damaged = code is not None and code & 0xFF in (
                sqlite3.SQLITE_CORRUPT,
                sqlite3.SQLITE_NOTADB,
            )
            problem = str(exc) if damaged else None
        if problem is None:
            yield None
        else:
            alone_passes = left_beside and _file_alone_check(path) is None
            partly = left_beside and not alone_passes and _partly_folded(path)
            yield AtRest(" ".join(problem.split()), left_beside, alone_passes, partly)
    finally:
        check.close()


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
