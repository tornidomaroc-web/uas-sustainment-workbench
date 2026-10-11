"""The store file is in SQLite's WAL mode, so a reader on another connection never makes the
store's own commit wait, and `verify_journal` walks the journal on a second connection without
the store's write lock, so a write issued during the walk lands at once (issue #44).

What must still hold, and is checked here: the two caches keyed on `PRAGMA data_version`
(the kept journal result and /health) see every commit from another connection and none of
the store's own commits or rollbacks; a second writer still makes the commit wait and a
refused commit leaves the kept result where it was; a walk describes one state of the file
even when a write lands halfway through; and a closed store is one self-contained file, with
no `-wal` or `-shm` beside it. No assertion names an OS: no error text, no path separator.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from uas_workbench.ledger import Entry
from uas_workbench.service import journal
from uas_workbench.service.cli import main
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
    store._journal = None  # the next call walks
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
