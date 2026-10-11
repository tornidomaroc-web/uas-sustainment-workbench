"""The store file is in SQLite's WAL mode, so a reader on another connection never makes the
store's own commit wait, and `verify_journal` walks the journal on the calling thread's own
reading connection without the store's write lock, so a write issued during the walk lands at
once (issue #44). Every thread that reads has a connection of its own (issue #60, the last
section here); the writing connection is used under the write lock only.

What must still hold, and is checked here: the two caches keyed on `PRAGMA data_version`
(the kept journal result and /health) see every commit from another connection and none of
the store's own commits or rollbacks; a second writer still makes the commit wait and a
refused commit leaves the kept result where it was; a walk describes one state of the file
even when a write lands halfway through; and a closed store is one self-contained file, with
no `-wal` or `-shm` beside it. No assertion names an OS: no error text, no path separator.
"""

from __future__ import annotations

import contextlib
import gc
import sqlite3
import threading
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from uas_workbench.ledger import Entry, UnreadableRecord
from uas_workbench.service import journal
from uas_workbench.service.cli import main
from uas_workbench.service.journal import Verification
from uas_workbench.service.store import Store

FIXTURES = Path(__file__).parent / "fixtures"
AT = datetime(2026, 9, 20, 9, 0, tzinfo=UTC)
WAIT_S = 2.0  # how long a write may take before it counts as waiting for the walk


@pytest.fixture
def db(tmp_path: Path) -> str:
    path = str(tmp_path / "fleet.sqlite")
    main(["--db", path, "seed", "--fixtures", str(FIXTURES)])
    return path


def entry(subject: str, statement: str) -> Entry:
    return Entry(
        id=None,
        subject=subject,
        kind="work_order.open",
        occurred_utc=AT,
        recorded_utc=AT,
        entered_by="A. Tester, maintenance",
        statement=statement,
        details={"state": "deferred", "work_id": "WO-1"},
        supersedes=None,
        reason=None,
        synthetic=False,
    )


def pragma(connection: sqlite3.Connection, name: str) -> object:
    return connection.execute(f"PRAGMA {name}").fetchone()[0]


def walked_afresh(path: str) -> journal.Verification:
    """A walk by a connection that holds nothing kept: what the file says now."""
    connection = sqlite3.connect(path)
    try:
        return journal.verify(connection)
    finally:
        connection.close()


@contextlib.contextmanager
def raw(path: str) -> Iterator[sqlite3.Connection]:
    """A plain connection on the file, as an edit made with sqlite3 would use."""
    connection = sqlite3.connect(path)
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


def sidecars(path: str) -> list[str]:
    """The WAL side files beside a store, by suffix only."""
    file = Path(path)
    return sorted(p.name[len(file.name) :] for p in file.parent.glob(file.name + "-*"))


# ---- the mode and its settings ------------------------------------------------------------


def test_a_file_store_is_in_wal_mode_with_full_sync_and_a_busy_timeout(db: str) -> None:
    """WAL for the readers; synchronous FULL so a committed journal link is on disk before
    the commit returns, as in rollback mode (the default for WAL is also FULL, stated here so
    a change is seen); a busy timeout so a second writer waits a moment rather than failing
    at once."""
    store = Store(db)
    assert pragma(store._db, "journal_mode") == "wal"
    assert pragma(store._db, "synchronous") == 2  # FULL
    assert pragma(store._db, "busy_timeout") == 5000
    assert pragma(store._db, "foreign_keys") == 1  # unchanged
    store.close()


def test_the_mode_is_in_the_file_and_an_in_memory_store_is_untouched(db: str) -> None:
    """The mode is persistent: a plain connection that sets nothing finds it. A store in
    memory has no file and no WAL; it stays what SQLite gives it."""
    store = Store(db)
    store.close()
    plain = sqlite3.connect(db)
    try:
        assert pragma(plain, "journal_mode") == "wal"
    finally:
        plain.close()
    memory = Store(":memory:")
    assert pragma(memory._db, "journal_mode") == "memory"
    assert memory.verify_journal().ok
    memory.close()


# ---- data_version, which both caches key on -----------------------------------------------


def test_data_version_moves_on_another_connections_commit_and_on_nothing_else(db: str) -> None:
    """Under WAL as in rollback mode: the store's own commit and its own rollback leave its
    data_version still; a commit from another connection moves it; a rollback there does not.
    `journal_state` and `readable` depend on exactly this."""
    store = Store(db)
    other = Store(db)
    assert pragma(store._db, "journal_mode") == "wal"
    start = pragma(store._db, "data_version")
    store.append_entry(entry("SYN-02", "own commit"))
    assert pragma(store._db, "data_version") == start, "own commit moved data_version"
    store._db.execute("INSERT INTO aircraft VALUES ('X', 'x', 'px4', 1, 'none', 'none')")
    store._db.rollback()
    assert pragma(store._db, "data_version") == start, "own rollback moved data_version"
    other.append_entry(entry("SYN-03", "other commit"))
    moved = pragma(store._db, "data_version")
    assert moved != start, "another connection's commit did not move data_version"
    other._db.execute("INSERT INTO aircraft VALUES ('Y', 'y', 'px4', 1, 'none', 'none')")
    other._db.rollback()
    assert pragma(store._db, "data_version") == moved, "another connection's rollback moved it"
    assert store.journal_state() == walked_afresh(db)
    other.close()
    store.close()


# ---- readers and writers ------------------------------------------------------------------


def test_an_open_reader_on_another_connection_does_not_make_the_stores_commit_wait(
    db: str,
) -> None:
    """The case PR #57 met in rollback mode: a reader holding a read transaction made the
    store's commit fail with the database locked. Under WAL the reader keeps its snapshot and
    the commit goes through at once; the kept result moves with it and equals a fresh walk."""
    store = Store(db)
    before = store.journal_state()
    store._db.execute("PRAGMA busy_timeout = 100")  # a wait here would be the failure
    reader = sqlite3.connect(db, isolation_level=None)
    reader.execute("BEGIN")
    seen = reader.execute("SELECT COUNT(*) FROM journal").fetchone()[0]
    try:
        store.append_entry(entry("SYN-02", "written while a reader holds its snapshot"))
        assert reader.execute("SELECT COUNT(*) FROM journal").fetchone()[0] == seen
    finally:
        reader.execute("COMMIT")
        reader.close()
    assert store.journal_state() == walked_afresh(db)
    assert store.journal_state().length == before.length + 1
    store.close()


def test_a_second_writer_still_makes_the_commit_wait_and_a_refused_one_moves_nothing(
    db: str,
) -> None:
    """WAL has one writer at a time: another connection holding the write lock makes the
    store's commit wait for busy_timeout and then fail; the write is rolled back and the kept
    result stays where it was, then moves with the next write that is stored."""
    store = Store(db)
    before = store.journal_state()
    store._db.execute("PRAGMA busy_timeout = 100")
    writer = sqlite3.connect(db, isolation_level=None)
    writer.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(sqlite3.OperationalError):
            store.append_entry(entry("SYN-02", "a write that is rolled back"))
    finally:
        writer.execute("ROLLBACK")
        writer.close()
    assert store.journal_state() == walked_afresh(db) == before
    store.append_entry(entry("SYN-02", "a write that is stored"))
    assert store.journal_state() == walked_afresh(db)
    assert store.journal_state().length == before.length + 1
    store.close()


def _finished_within(target: threading.Thread, seconds: float) -> bool:
    target.join(seconds)
    return not target.is_alive()


def test_a_write_issued_during_a_verify_walk_does_not_wait_for_it(
    db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #44: the walk no longer holds the store's write lock, so a write issued while
    `verify_journal` reads every row lands at once instead of waiting for the walk to end.
    The walk is held open here until the write has landed."""
    store = Store(db)
    started, release = threading.Event(), threading.Event()
    walk = journal.verify

    def held(connection: sqlite3.Connection, head: str | None = None) -> journal.Verification:
        started.set()
        release.wait(10)
        return walk(connection, head)

    monkeypatch.setattr(journal, "verify", held)
    results: list[journal.Verification] = []
    walking = threading.Thread(target=lambda: results.append(store.verify_journal()))
    writing = threading.Thread(
        target=lambda: store.append_entry(entry("SYN-02", "issued during the walk"))
    )
    walking.start()
    assert started.wait(10)
    writing.start()
    try:
        assert _finished_within(writing, WAIT_S), "the write waited for the walk"
    finally:
        release.set()
        walking.join(10)
        writing.join(10)
    assert results and results[0].ok
    assert store.journal_state() == walked_afresh(db)
    store.close()


def test_a_walk_describes_one_state_of_the_file_while_a_write_lands(
    db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write that lands halfway through the walk is not in it: the walk reads a snapshot
    and reports the chain as it was when it began, intact at the old length; the next walk
    has the new link; the kept result equals a fresh walk throughout."""
    store = Store(db)
    before = store.verify_journal()
    paused, go = threading.Event(), threading.Event()
    unjournaled = journal._unjournaled

    def held(
        connection: sqlite3.Connection, seen: set[tuple[str, int | None]], length: int
    ) -> journal.Break | None:
        paused.set()  # every link has been read; the last pass over the rows is next
        go.wait(10)
        return unjournaled(connection, seen, length)

    monkeypatch.setattr(journal, "_unjournaled", held)
    results: list[journal.Verification] = []
    walking = threading.Thread(target=lambda: results.append(store.verify_journal()))
    writing = threading.Thread(
        target=lambda: store.append_entry(entry("SYN-02", "landed during the walk"))
    )
    walking.start()
    assert paused.wait(10)
    writing.start()
    try:
        assert _finished_within(writing, WAIT_S), "the write waited for the walk"
    finally:
        go.set()
        walking.join(10)
        writing.join(10)
    monkeypatch.undo()
    assert results and results[0].ok
    assert results[0].length == before.length, "the walk saw a row written after it began"
    assert results[0].head == before.head
    after = store.verify_journal()
    assert after.ok and after.length == before.length + 1
    assert store.journal_state() == walked_afresh(db) == after
    store.close()


# ---- what is on disk ----------------------------------------------------------------------


def test_a_store_in_use_has_two_side_files_and_a_closed_store_has_none(db: str) -> None:
    """While a store is open its WAL and shared-memory index sit beside the file; closing the
    last connection checkpoints the WAL into the file and removes both, so the file alone is
    the whole store, and copying it when no store is open is a complete copy."""
    store = Store(db)
    store.append_entry(entry("SYN-02", "a write that must end up in the main file"))
    assert sidecars(db) == ["-shm", "-wal"]
    store.close()
    assert sidecars(db) == []
    plain = sqlite3.connect(db)
    try:
        assert (
            plain.execute(
                "SELECT COUNT(*) FROM entries WHERE record LIKE '%must end up in the main file%'"
            ).fetchone()[0]
            == 1
        )
    finally:
        plain.close()


def test_a_second_store_on_the_same_file_keeps_the_side_files_until_it_closes(db: str) -> None:
    """The side files belong to the file, not to one store: they stay while any connection is
    open and go when the last one closes. A copy taken while any store is open is not a copy
    of the whole store; README says what to do instead."""
    store = Store(db)
    other = Store(db)
    store.close()
    assert sidecars(db) == ["-shm", "-wal"]
    other.close()
    assert sidecars(db) == []


def test_closing_a_store_twice_is_harmless(db: str) -> None:
    """tests/conftest.py closes every store at the end of a test, including one its test
    closed already; the checkpoint on close must not turn that into an error."""
    store = Store(db)
    store.close()
    store.close()
    assert sidecars(db) == []


@pytest.mark.parametrize("hook", ["_check_content", "_unjournaled"])
def test_the_kept_walk_is_one_state_when_another_connection_commits_halfway(
    db: str, monkeypatch: pytest.MonkeyPatch, hook: str
) -> None:
    """The walk that fills the kept result reads the journal, then each linked row, then every
    row for one not linked. Another connection's write committed between those reads must not
    reach the walk: the kept result is the state its data_version names, here the state
    before that write, and the next call walks again and has the write."""
    store = Store(db)
    before = walked_afresh(db)
    store._journal.value = None  # the next call walks
    original = getattr(journal, hook)
    fired: list[bool] = []

    def commit_then_read(*args: object) -> object:
        if not fired:
            fired.append(True)
            other = Store(db)
            other.append_entry(entry("SYN-03", "committed halfway through the walk"))
            other.close()
        return original(*args)

    monkeypatch.setattr(journal, hook, commit_then_read)
    kept = store.journal_state()
    monkeypatch.undo()
    assert fired
    assert kept == before, "the walk mixed two states of the file"
    after = walked_afresh(db)
    assert after.ok and after.length == before.length + 1
    assert store.journal_state() == after
    store.close()


# ---- one reading connection per thread (issue #60) ---------------------------------------


def _in_threads(count: int, work: Callable[[int], object]) -> list[object]:
    """`work(n)` on `count` threads at once; the results in thread order, exceptions kept."""
    results: list[object] = [None] * count

    def run(n: int) -> None:
        try:
            results[n] = work(n)
        except BaseException as exc:
            results[n] = exc

    threads = [threading.Thread(target=run, args=(n,)) for n in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    return results


def test_each_thread_reads_on_its_own_connection_and_close_closes_every_one(db: str) -> None:
    """Eight threads read at once, each on a connection of its own and none on the writing
    one, so no two threads step one prepared statement; the walk of the journal runs on that
    connection too. `close` closes every one of them from the closing thread: the closed
    store is one file, and a read after it, from a thread that read before or a new one, is
    refused as a read on a closed connection is. Nothing is left open: the suite turns a
    connection collected unclosed into an error."""
    store = Store(db)
    fresh = walked_afresh(db)
    expected = [a.key for a in store.aircraft()]

    def work(n: int) -> int:
        for _ in range(20):
            assert [a.key for a in store.aircraft()] == expected
            assert store.flights("SYN-03")[0].log_ref.startswith("SYN-03")
            assert store.entries("SYN-03")
            assert store.verify_journal() == fresh
        return id(store._reader())

    connections = _in_threads(8, work)
    assert all(isinstance(c, int) for c in connections), connections
    assert len(set(connections)) == 8, "two threads read on one connection"
    assert id(store._db) not in connections, "a thread read on the writing connection"
    assert set(connections) <= {id(c) for c in store._readers}
    store.close()
    gc.collect()
    assert sidecars(db) == []
    with pytest.raises(sqlite3.ProgrammingError):
        store.aircraft()  # this thread never read
    refused = _in_threads(2, lambda n: store.entry_count())
    assert all(isinstance(r, sqlite3.ProgrammingError) for r in refused), refused


def test_a_store_in_memory_is_read_by_threads_under_its_one_connection(db: str) -> None:
    """A store in memory has one connection; threads read it one at a time under the write
    lock, and every read is whole."""
    store = Store(":memory:")
    main(["--db", ":memory:", "seed", "--fixtures", str(FIXTURES)])  # exercises the path only
    seeded = Store(db)
    for aircraft in seeded.aircraft():
        store.add_aircraft(aircraft)
        for record in seeded.flights(aircraft.key):
            store.add_flight(aircraft.key, record)
    seeded.close()
    expected = [(a.key, len(store.flights(a.key))) for a in store.aircraft()]

    def work(n: int) -> bool:
        return all(
            [(a.key, len(store.flights(a.key))) for a in store.aircraft()] == expected
            for _ in range(30)
        )

    assert _in_threads(4, work) == [True] * 4
    store.close()


def test_a_reading_connections_data_version_moves_on_the_stores_own_commit(db: str) -> None:
    """Why the kept answers are not keyed on a reading connection's version: from any
    connection but the writing one, the store's own commit is another connection's commit,
    so it moves that connection's data_version, and a cache keyed on it there would walk the
    journal, or read the whole store, after every write. The kept journal result and /health
    are keyed on the writing connection's version, read under the write lock, which own
    commits leave still (the test above this section) and which this test's own thread's
    reading connection does not share."""
    store = Store(db)
    reader = store._reader()
    reader.execute("SELECT COUNT(*) FROM entries").fetchone()  # a version is per connection
    start = pragma(reader, "data_version")
    writer_start = pragma(store._db, "data_version")
    store.append_entry(entry("SYN-02", "own commit"))
    assert pragma(reader, "data_version") != start, "own commit left the reader's version"
    assert pragma(store._db, "data_version") == writer_start, "own commit moved the writer's"
    assert store.journal_state() == walked_afresh(db)
    store.close()


def test_the_kept_answers_equal_a_fresh_computation_from_every_thread_after_every_change(
    db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """From four threads at once, after each kind of change: the kept journal result is what
    a fresh walk of the file gives, and /health's answer is what a fresh read of the whole
    store gives, with the journal walked and the store read again exactly when another
    connection has committed, never for the store's own commit or rollback, another
    connection's rollback, or a call."""
    store = Store(db)
    other = Store(db)
    store.journal_state()
    store.readable()
    walks: list[int] = []
    reads: list[int] = []
    walk, read = journal.verify, Store._first_unreadable

    def counted_walk(connection: sqlite3.Connection, head: str | None = None) -> Verification:
        walks.append(1)
        return walk(connection, head)

    def counted_read(self: Store, connection: sqlite3.Connection) -> UnreadableRecord | None:
        reads.append(1)
        return read(self, connection)

    monkeypatch.setattr(journal, "verify", counted_walk)
    monkeypatch.setattr(Store, "_first_unreadable", counted_read)

    def kept_equal_fresh(walked: int, read_: int) -> None:
        fresh = walked_afresh(db)
        del walks[-1:]  # that walk, not one of the store's
        with raw(db) as c:  # a fresh read of the whole store by a connection that kept nothing
            unreadable = read(store, c)
        results = _in_threads(
            4, lambda n: [(store.journal_state(), store.readable()) for _ in range(5)]
        )
        for result in results:
            assert isinstance(result, list), result
            for kept, health in result:
                assert kept == fresh
                assert (health is None) == (unreadable is None)
                if health is not None and unreadable is not None:
                    assert health.detail == unreadable.detail
        assert (sum(walks), sum(reads)) == (walked, read_), "walks and reads of the whole store"

    kept_equal_fresh(0, 0)  # 20 calls from 4 threads, nothing changed: no walk, no read
    store.append_entry(entry("SYN-02", "own commit"))
    kept_equal_fresh(0, 0)
    store._db.execute("INSERT INTO aircraft VALUES ('X', 'x', 'px4', 1, 'none', 'none')")
    store._db.rollback()
    kept_equal_fresh(0, 0)
    other.append_entry(entry("SYN-03", "another connection's commit"))
    kept_equal_fresh(1, 1)
    other._db.execute("INSERT INTO aircraft VALUES ('Y', 'y', 'px4', 1, 'none', 'none')")
    other._db.rollback()
    kept_equal_fresh(1, 1)
    with raw(db) as c:
        (text,) = c.execute("SELECT record FROM entries WHERE id = 4").fetchone()
        c.execute("UPDATE entries SET record = 'not json' WHERE id = 4")
    kept_equal_fresh(2, 2)
    assert store.journal_state().broken is not None and store.readable() is not None
    with raw(db) as c:
        c.execute("UPDATE entries SET record = ? WHERE id = 4", (text,))
    kept_equal_fresh(3, 3)
    assert store.journal_state().ok and store.readable() is None
    other.close()
    store.close()
