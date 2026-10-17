"""The store's own checkpoints are not changes from outside (issue #79).

Since PR #76 the store looks at its file (one `stat`) before every read and write and, when
the file is not as last seen, checks it (`PRAGMA quick_check`) while every other read and write
waits, then moves the connection generation: every connection is opened afresh and every kept
answer (the journal walk, what `readable` holds, the projection) is computed again. Under WAL
the store file changes only when the WAL is checkpointed into it, and SQLite checkpoints
automatically on the commit that takes the WAL past about 1,000 pages, which is the store's own
commit. So the store's own writing was taken for a change from outside: on main 096f335, 3,000
writes through one store gave 13 such moves, each followed by a walk of the whole journal.

The store's own commit is now made under the lock its look at the file takes: the file is
looked at right before the commit and right after it, and a change between the two is the
commit's own checkpoint. It is still checked, once, by the writer (a change from outside that
lands during the commit would be in that file too), and when sound the file is recorded as
seen with no generation moved and nothing computed again. Every other change, another
process's commit and checkpoint included, is checked as from outside, as before.

Assertions are about counts, the store's own answers and fresh walks, never an operating
system's text.
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from uas_workbench.fleet import load_config
from uas_workbench.fleet.synthetic import generate
from uas_workbench.ledger import Entry
from uas_workbench.ledger.project import project
from uas_workbench.service.store import Store

STORE = importlib.import_module("uas_workbench.service.store")
JOURNAL = importlib.import_module("uas_workbench.service.journal")
# A statement long enough that some tens of writes take the WAL past the thousand pages SQLite
# checkpoints at by itself (main), and a store that folds its WAL every 20 own commits (the
# test's own number, so a test sees several of the store's own checkpoints in a few seconds).
LONG = "hours read off the aircraft. " * 1000
MOST_WRITES = 600
CHECKPOINTS = 3
EVERY = 20


class Counted:
    """Generation moves, journal walks and file checks made by one store."""

    def __init__(self, store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
        self.store = store
        self.generation = store._generation
        self.walks = 0
        self.checks = 0
        self.checkpoints = 0
        walk, check, checkpoint = JOURNAL.verify, store._damage_in, store._checkpoint

        def counted_walk(*args: Any, **kwargs: Any) -> Any:
            self.walks += 1
            return walk(*args, **kwargs)

        def counted_check(now: Any) -> Any:
            self.checks += 1
            return check(now)

        def counted_checkpoint() -> None:
            self.checkpoints += 1
            checkpoint()

        monkeypatch.setattr(JOURNAL, "verify", counted_walk)
        monkeypatch.setattr(store, "_damage_in", counted_check)
        monkeypatch.setattr(store, "_checkpoint", counted_checkpoint)

    @property
    def moves(self) -> int:
        return self.store._generation - self.generation


def apart(db: Path) -> None:
    """Set the file's time of last change two seconds past what it was: a change from outside
    within the clock tick of the change last seen, at the same size, is not seen until the next
    change (LIMITS.md), and these cases are about what is seen."""
    t = os.stat(db).st_mtime_ns + 2_000_000_000
    os.utime(db, ns=(t, t))


def file_state(db: Path) -> tuple[int, int]:
    st = os.stat(db)
    return st.st_size, st.st_mtime_ns


def seeded(tmp_path: Path) -> tuple[Path, Store]:
    db = tmp_path / "fleet.sqlite"
    seeding = Store(str(db))
    seeding.add_fleet(generate(load_config()))
    seeding.close()
    store = Store(str(db))
    warm(store)
    return db, store


def warm(store: Store) -> None:
    """Every kept answer computed, so a later walk or fold is one the store chose to make."""
    assert store.journal_state().ok
    assert store.readable() is None
    store.projection()


def assert_kept_equal_fresh(store: Store) -> None:
    assert store.journal_state() == store.verify_journal()
    assert store.readable() is None
    assert store.projection() == project(store.entries())
    fresh = Store(store._path)
    try:
        assert store.journal_state() == fresh.verify_journal()
        assert store.projection() == fresh.projection()
    finally:
        fresh.close()


def write_until_checkpointed(store: Store, db: Path, statement: str = LONG) -> int:
    """Own writes, each followed by every kept answer, until the store file has changed
    `CHECKPOINTS` times; how many times it changed. Under WAL only a checkpoint changes it."""
    first = store.entries()[0]
    changes, seen = 0, file_state(db)
    for i in range(MOST_WRITES):
        store.append_entry(Entry(**{**first.__dict__, "id": None, "statement": f"{i} {statement}"}))
        warm(store)
        now = file_state(db)
        changes += now != seen
        seen = now
        if changes >= CHECKPOINTS:
            break
    return changes


@pytest.fixture
def store_and_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Path, Store]]:
    # How often the store folds its WAL (absent on main, where SQLite decides).
    monkeypatch.setattr(STORE, "CHECKPOINT_COMMITS", EVERY, raising=False)
    db, store = seeded(tmp_path)
    try:
        yield db, store
    finally:
        store.close()


def test_the_stores_own_checkpoints_move_no_generation_and_walk_nothing(
    store_and_file: tuple[Path, Store], monkeypatch: pytest.MonkeyPatch
) -> None:
    db, store = store_and_file
    counted = Counted(store, monkeypatch)
    changes = write_until_checkpointed(store, db)
    assert changes >= CHECKPOINTS, "the WAL was never checkpointed into the file"
    # Each own checkpoint is checked once, by the writer; none is taken for a change from
    # outside: no generation moved, so no connection reopened and nothing computed again.
    assert counted.moves == 0, (
        f"{counted.moves} generation moves and {counted.walks} journal walks for "
        f"{changes} of the store's own checkpoints"
    )
    assert counted.walks == 0, f"{counted.walks} journal walks after own writes"
    # One check per own checkpoint, whether or not its write moved the file's size or time
    # of last change (within one clock tick of the change before it, it moves neither).
    assert counted.checkpoints >= changes
    assert counted.checks == counted.checkpoints, (counted.checks, counted.checkpoints)
    assert_kept_equal_fresh(store)


def test_a_commit_never_changes_the_store_file_and_the_stores_own_checkpoint_does(
    store_and_file: tuple[Path, Store], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The store file changes at the store's own checkpoints and at nothing else it does: a
    commit goes to the `-wal` file only, so every change the store makes to its file is one it
    knows it made."""
    monkeypatch.setattr(STORE, "CHECKPOINT_COMMITS", 7, raising=False)
    db, store = store_and_file
    first = store.entries()[0]
    seen, changed_at = file_state(db), []
    for i in range(1, 22):
        store.append_entry(Entry(**{**first.__dict__, "id": None, "statement": f"{i} {LONG}"}))
        now = file_state(db)
        if now != seen:
            changed_at.append(i)
        seen = now
    assert changed_at == [7, 14, 21]


class _Planting:
    """The writing connection, with a change from outside planted at one moment of the
    store's own work on it: inside its commit, or inside its own checkpoint; and, given
    `after`, that run right after the checkpoint statement, before the store looks again."""

    def __init__(self, connection: Any, moment: str, plant: Any, after: Any = None) -> None:
        self._connection, self._moment, self._plant = connection, moment, plant
        self._after = after
        self.planted = False

    def _now(self) -> None:
        if not self.planted:
            self.planted = True
            self._plant()

    def execute(self, sql: str, *args: Any) -> Any:
        if self._moment == "checkpoint" and sql.startswith("PRAGMA wal_checkpoint"):
            self._now()
            cursor = self._connection.execute(sql, *args)
            if self._after is not None:
                self._after()
            return cursor
        return self._connection.execute(sql, *args)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)

    def __enter__(self) -> Any:
        return self._connection.__enter__()

    def __exit__(self, *exc: Any) -> Any:
        if exc[0] is None and self._moment == "commit":
            self._now()
        return self._connection.__exit__(*exc)


@pytest.mark.parametrize("moment", ["commit", "checkpoint"])
def test_damage_landing_during_the_stores_own_work_on_its_file_is_still_refused(
    store_and_file: tuple[Path, Store], monkeypatch: pytest.MonkeyPatch, moment: str
) -> None:
    """A change from outside that lands while the store commits, or while it folds its WAL
    into the file, is seen: the commit leaves the file to the next look, and the checkpoint is
    followed by the writer's own check of the file, which the damage is in. Damaged, every
    read and write is refused from then on; restored in place, it is served afresh."""
    monkeypatch.setattr(STORE, "CHECKPOINT_COMMITS", 1, raising=False)
    db, store = store_and_file
    original = db.read_bytes()
    first = store.entries()[0]

    def damage() -> None:
        with db.open("r+b") as f:
            for page in range(2, len(original) // 4096):
                f.seek(page * 4096)
                f.write(bytes(range(256)) * 16)
        apart(db)

    writer = store._db
    planting = _Planting(writer, moment, damage)
    store._db = planting  # type: ignore[assignment]
    try:
        store.append_entry(Entry(**{**first.__dict__, "id": None, "statement": "during"}))
    except Exception:
        pass  # the write itself may meet the damage; either way the next look must
    finally:
        store._db = writer
    assert planting.planted, f"no {moment} of the store's own ran to plant the damage in"
    with pytest.raises(STORE.StoreDamaged):
        store.entry_count()
    with pytest.raises(STORE.StoreDamaged):
        store.append_entry(Entry(**{**first.__dict__, "id": None, "statement": "after"}))
    # Restored in place, it serves the restored file, and every kept answer is fresh.
    db.write_bytes(original)
    os.utime(db)
    assert store.entry_count() == len(store.entries())
    assert_kept_equal_fresh(store)


def test_damage_landing_in_the_clock_tick_of_the_stores_own_checkpoint_is_refused(
    store_and_file: tuple[Path, Store], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The store's own checkpoint can leave its file looking as last seen (issue #95): its
    write stamps the file with the system clock, whose tick is about 15 ms on Windows, and a
    checkpoint that writes no page past the end of the file within the tick of the change
    last seen (here the open; in a running service the checkpoint before it) moves neither
    the size nor the time of last change. On main 382767a the two looks were 12 to 66 ms
    apart, at the same size, in 60 of 60 runs here. Damage planted during such a checkpoint
    is in a file whose two looks are equal, and the store must check it all the same: it
    knows it folded its own commits into the file. The tick is injected, not waited for: once
    the checkpoint statement has run, the file's time of last change is set back to the one
    the store saw before it, which is what the clock does within one tick."""
    monkeypatch.setattr(STORE, "CHECKPOINT_COMMITS", 1, raising=False)
    db, store = store_and_file
    original = db.read_bytes()
    first = store.entries()[0]
    seen = store._file
    assert isinstance(seen, STORE._FileState)

    def damage() -> None:
        with db.open("r+b") as f:
            for page in range(2, len(original) // 4096):
                f.seek(page * 4096)
                f.write(bytes(range(256)) * 16)

    def within_the_tick() -> None:
        os.utime(db, ns=(seen.changed_ns, seen.changed_ns))

    writer = store._db
    planting = _Planting(writer, "checkpoint", damage, within_the_tick)
    store._db = planting  # type: ignore[assignment]
    try:
        store.append_entry(Entry(**{**first.__dict__, "id": None, "statement": "during"}))
    except Exception:
        pass  # the write itself may meet the damage; either way the next look must
    finally:
        store._db = writer
    assert planting.planted, "no checkpoint of the store's own ran to plant the damage in"
    assert file_state(db) == (seen.size, seen.changed_ns), (
        "the checkpoint grew the store file, so its size alone tells the two looks apart; "
        "this case needs a write that fits the pages the file has"
    )
    with pytest.raises(STORE.StoreDamaged):
        store.entry_count()
    with pytest.raises(STORE.StoreDamaged):
        store.append_entry(Entry(**{**first.__dict__, "id": None, "statement": "after"}))
    # Restored in place, it serves the restored file, and every kept answer is fresh.
    db.write_bytes(original)
    os.utime(db)
    assert store.entry_count() == len(store.entries())
    assert_kept_equal_fresh(store)


def test_after_own_checkpoints_damage_and_restore_are_seen_as_before(
    store_and_file: tuple[Path, Store],
) -> None:
    """Own checkpoints recorded as seen do not hide a later change from outside: damaged it is
    refused; restored in place it is served afresh, the kept answers equal to fresh ones."""
    db, store = store_and_file
    assert write_until_checkpointed(store, db) >= CHECKPOINTS
    store.close()  # every write in the file alone, so the flight pages are in the file only
    store = Store(str(db))
    try:
        warm(store)
        assert write_until_checkpointed(store, db, "more") >= 1
        held = store.entry_count()
        # The file as it is now, which the `-wal` file's pages fit: a copy from before the
        # writes still in the `-wal` would be overlaid by them (restore with the service
        # stopped, LIMITS.md).
        good = db.read_bytes()
        # Every page after the first: a page the `-wal` file holds a newer copy of is read from
        # there, so damage to it alone passes SQLite's check (LIMITS.md); the flight pages,
        # which no write since the reopen touched, are in the file only.
        with db.open("r+b") as f:
            for page in range(1, os.path.getsize(db) // 4096):
                f.seek(page * 4096)
                f.write(bytes(range(256)) * 16)
        apart(db)
        with pytest.raises(STORE.StoreDamaged):
            store.readable()
        generation = store._generation
        db.write_bytes(good)
        os.utime(db)
        assert store.entry_count() == held
        assert store._generation == generation + 1
        assert_kept_equal_fresh(store)
    finally:
        store.close()


def test_another_process_writing_beside_the_store_is_still_a_change_from_outside(
    store_and_file: tuple[Path, Store], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `uasw record` run beside the store commits and, closing, checkpoints into the file:
    that is another connection's change, checked as from outside, the generation moved once,
    and every kept answer equals a fresh one with the new entry in it."""
    db, store = store_and_file
    counted = Counted(store, monkeypatch)
    held = store.entry_count()
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "uas_workbench.service.cli",
            *("--db", str(db), "record", "work-order", "open", "SYN-03", "--state", "deferred"),
            *("--by", "A. Tester, maintenance", "--statement", "opened from another process"),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr
    assert store.entry_count() == held + 1
    assert counted.checks == 1 and counted.moves == 1, (counted.checks, counted.moves)
    assert_kept_equal_fresh(store)


def test_the_wal_starts_over_while_threads_keep_reading(
    store_and_file: tuple[Path, Store],
) -> None:
    """Two threads reading without pause keep some snapshot open nearly all the time. The
    store's own checkpoint waits for the snapshots older than its last commit to end, so the
    WAL is folded whole and the next commit starts it over: the `-wal` file stays within a few
    checkpoints' worth of writes, where a checkpoint that waits for no reader left it growing
    with every write (84 MiB after 6,000 writes, against 6 MiB on 0.6.1)."""
    db, store = store_and_file
    wal = Path(f"{db}-wal")
    first = store.entries()[0]
    stop = threading.Event()
    failures: list[BaseException] = []

    def read() -> None:
        try:
            while not stop.is_set():
                with store.snapshot():
                    store.entry_count()
                    store.projection()
                    time.sleep(0.002)
        except BaseException as exc:
            failures.append(exc)

    readers = [threading.Thread(None, read) for _ in range(2)]
    for reader in readers:
        reader.start()
    sizes = []
    try:
        for i in range(EVERY * 20):
            store.append_entry(Entry(**{**first.__dict__, "id": None, "statement": f"{i} {LONG}"}))
            sizes.append(wal.stat().st_size)
    finally:
        stop.set()
        for reader in readers:
            reader.join()
    assert not failures, failures
    # The WAL as the first own checkpoint found it: one checkpoint's worth of writes.
    one = sizes[EVERY - 1]
    assert max(sizes) <= 4 * one, (
        f"the WAL grew to {max(sizes)} bytes, {max(sizes) / one:.1f} checkpoints' worth"
    )
    assert_kept_equal_fresh(store)
