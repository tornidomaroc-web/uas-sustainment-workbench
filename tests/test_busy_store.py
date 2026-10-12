"""A store another connection holds is waited for, then refused cleanly (issues #69 and #70).

Every case here has a real second connection on the store file holding SQLite's write lock
(`BEGIN IMMEDIATE`), as another process writing would. A write that waits past the busy
timeout must be refused with nothing written: 503 from the service, one `refused (503)` line
and exit 1 from the command line, never a traceback and never a 500 after a commit.

#70: the first open of a store written by 0.5.x switches the file to WAL, which SQLite
answers busy at once, without the busy timeout, while another connection holds the lock; the
open failed in a fraction of a second with a traceback.

#69: an ingest wrote the aircraft row of a new aircraft and the flight in two commits; a
writer taking the store between them left a 500 with the aircraft row written and no flight.

Assertions are about statuses, rows and the store's own answers, never an operating system's
error text, a path or a line ending.
"""

from __future__ import annotations

import importlib
import random
import sqlite3
import threading
import time
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from uas_workbench.fleet import load_config
from uas_workbench.fleet.synthetic import generate
from uas_workbench.ledger.project import project
from uas_workbench.service.app import create_app
from uas_workbench.service.cli import main
from uas_workbench.service.store import Store

CONFIG = load_config()
STORE = importlib.import_module("uas_workbench.service.store")
LOG = next((Path(__file__).parent / "fixtures" / "px4").glob("*.ulg"))
BUSY = (
    "another write held the store for longer than this one waits; nothing was written, "
    "so send it again"
)
BUSY_OPEN = (
    "another connection held the store's write lock for longer than opening the store "
    "waits; nothing was written, so run it again"
)
HOLD_S = 1.0  # a lock let go within the busy timeout


class Holder:
    """A second connection on the store file that holds its write lock until let go."""

    def __init__(self, path: Path) -> None:
        self.connection = sqlite3.connect(
            str(path), isolation_level=None, timeout=0, check_same_thread=False
        )
        self.connection.execute("BEGIN IMMEDIATE")

    def release(self) -> None:
        if self.connection.in_transaction:
            self.connection.execute("ROLLBACK")
        self.connection.close()


def seeded(path: Path) -> None:
    store = Store(str(path))
    store.add_fleet(generate(CONFIG))
    store.close()


def make_0_5_store(path: Path) -> None:
    """A store as 0.5.x left it: rollback-journal mode, no journal table, schema version 0."""
    seeded(path)
    c = sqlite3.connect(str(path), isolation_level=None)
    c.execute("PRAGMA journal_mode = DELETE")
    c.execute("DROP TABLE journal")
    c.execute("PRAGMA user_version = 0")
    c.close()


def file_state(path: Path) -> tuple[str, int, int]:
    """Journal mode, schema version and whether a journal table exists, read on a connection
    of its own that changes nothing."""
    c = sqlite3.connect(str(path), isolation_level=None)
    try:
        mode = str(c.execute("PRAGMA journal_mode").fetchone()[0])
        version = int(c.execute("PRAGMA user_version").fetchone()[0])
        journal = int(
            c.execute("SELECT COUNT(*) FROM sqlite_master WHERE name = 'journal'").fetchone()[0]
        )
    finally:
        c.close()
    return mode, version, journal


def rows(path: Path) -> tuple[int, int]:
    c = sqlite3.connect(str(path), isolation_level=None)
    try:
        aircraft = int(c.execute("SELECT COUNT(*) FROM aircraft").fetchone()[0])
        flights = int(c.execute("SELECT COUNT(*) FROM flights").fetchone()[0])
    finally:
        c.close()
    return aircraft, flights


def assert_kept_equal_fresh(store: Store) -> None:
    assert store.journal_state() == store.verify_journal()
    assert store.journal_state().ok
    assert store.readable() is None
    assert store.projection() == project(store.entries())


@pytest.fixture
def short_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cut the busy timeout to 0.3 s for stores opened after it, so a refusal comes quickly."""
    monkeypatch.setattr(STORE, "BUSY_TIMEOUT_MS", 300)


# ---- #70: the first open of a 0.5.x store -------------------------------------------------


def test_the_first_open_of_a_0_5_store_waits_for_a_writer_that_lets_go(tmp_path: Path) -> None:
    db = tmp_path / "fleet.sqlite"
    make_0_5_store(db)
    holder = Holder(db)
    letting_go = threading.Timer(HOLD_S, holder.release)
    letting_go.start()
    try:
        started = time.monotonic()
        store = Store(str(db))
        waited = time.monotonic() - started
    finally:
        letting_go.join()
    try:
        assert waited >= HOLD_S * 0.8, "the open did not wait for the lock"
        result = store.verify_journal()
        assert result.ok and result.migration is not None
        assert file_state(db)[:2] == ("wal", 1)
    finally:
        store.close()


def test_the_first_open_of_a_0_5_store_held_past_the_busy_timeout_is_refused_and_writes_nothing(
    tmp_path: Path, short_wait: None
) -> None:
    db = tmp_path / "fleet.sqlite"
    make_0_5_store(db)
    before = file_state(db)
    holder = Holder(db)
    try:
        with pytest.raises(Exception) as caught:
            Store(str(db))
        assert isinstance(caught.value, STORE.StoreBusy), repr(caught.value)
        assert (caught.value.status, caught.value.detail) == (503, BUSY_OPEN)
    finally:
        holder.release()
    assert file_state(db) == before == ("delete", 0, 0)
    store = Store(str(db))  # nothing held now: the open migrates
    assert store.verify_journal().ok
    store.close()


def test_the_command_line_refuses_a_held_first_open_with_one_line(
    tmp_path: Path, short_wait: None, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "fleet.sqlite"
    make_0_5_store(db)
    holder = Holder(db)
    try:
        with pytest.raises(SystemExit) as code:
            main(["--db", str(db), "verify"])
    finally:
        holder.release()
    assert code.value.code == 1
    assert capsys.readouterr().err.strip().splitlines() == [f"refused (503): {BUSY_OPEN}"]
    assert file_state(db) == ("delete", 0, 0)


# ---- #69: an ingest is one transaction ----------------------------------------------------


def ingest(client: TestClient, key: str) -> Any:
    return client.post(
        "/ingest",
        headers={"Authorization": "Bearer t"},
        data={"aircraft_key": key},
        files={"log": (LOG.name, LOG.read_bytes())},
    )


@pytest.fixture
def served(tmp_path: Path, short_wait: None) -> Iterator[tuple[Path, Store, TestClient]]:
    db = tmp_path / "fleet.sqlite"
    seeded(db)
    store = Store(str(db))
    client = TestClient(create_app(store, write_token="t"), raise_server_exceptions=False)
    yield db, store, client
    store.close()


@pytest.mark.parametrize("key", ["SYN-02", "NEW-01"])
def test_an_ingest_held_past_the_busy_timeout_answers_503_and_writes_nothing(
    served: tuple[Path, Store, TestClient], key: str
) -> None:
    db, store, client = served
    kept = store.journal_state()
    before = rows(db)
    holder = Holder(db)
    try:
        answer = ingest(client, key)
        # Reads go on while the lock is held.
        assert store.flight_count() == before[1]
    finally:
        holder.release()
    assert (answer.status_code, answer.json()) == (503, {"detail": BUSY})
    assert rows(db) == before
    assert store.journal_state() == kept
    assert_kept_equal_fresh(store)
    assert ingest(client, key).status_code == 201  # nothing held now
    assert rows(db) == (before[0] + (key == "NEW-01"), before[1] + 1)
    assert_kept_equal_fresh(store)


def test_a_writer_taking_the_store_between_aircraft_and_flight_leaves_both_or_neither(
    served: tuple[Path, Store, TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The case #69 describes, made certain: the moment the store encodes the flight record,
    a second connection tries to take the write lock and holds it if it can. With the aircraft
    row and the flight in one transaction, either the ingest holds the lock already (the
    second connection cannot take it, and the ingest lands whole) or it takes the lock after
    the second connection and waits it out (refused whole). Before, the aircraft row was
    already committed there, the second connection took the lock, and the flight failed."""
    db, store, client = served
    before = rows(db)
    encode = STORE.record_to_json
    held: list[Holder | None] = []

    def encoding(record: Any) -> Any:
        if not held:
            try:
                held.append(Holder(db))
            except sqlite3.OperationalError:
                held.append(None)
        return encode(record)

    monkeypatch.setattr(STORE, "record_to_json", encoding)
    try:
        answer = ingest(client, "NEW-01")
    finally:
        for h in held:
            if h is not None:
                h.release()
    outcome = (answer.status_code, rows(db)[0] - before[0], rows(db)[1] - before[1])
    assert outcome in {(201, 1, 1), (503, 0, 0)}, (outcome, answer.text)
    assert_kept_equal_fresh(store)


def test_the_command_line_refuses_a_held_ingest_with_one_line_per_log(
    tmp_path: Path, short_wait: None, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "fleet.sqlite"
    seeded(db)
    before = rows(db)
    holder = Holder(db)
    try:
        with pytest.raises(SystemExit) as code:
            main(["--db", str(db), "ingest", "NEW-01", str(LOG)])
    finally:
        holder.release()
    assert code.value.code == 1
    lines = capsys.readouterr().err.strip().splitlines()
    assert len(lines) == 1, lines
    assert lines[0].startswith("refused (503): ") and lines[0].endswith(f": {BUSY}"), lines
    assert rows(db) == before


def test_ingests_beside_a_writer_that_comes_and_goes_land_whole_or_not_at_all(
    served: tuple[Path, Store, TestClient],
) -> None:
    """A second connection takes and lets go of the write lock at random while new aircraft
    are ingested one after another: every answer is 201 with the aircraft and its flight, or
    503 with neither, and the kept answers equal fresh ones at the end."""
    db, store, client = served
    stop = threading.Event()
    rng = random.Random(65)

    def come_and_go() -> None:
        while not stop.is_set():
            try:
                h = Holder(db)
            except sqlite3.OperationalError:
                continue
            time.sleep(rng.uniform(0.0, 0.6))
            h.release()
            time.sleep(rng.uniform(0.0, 0.05))

    writer = threading.Thread(None, come_and_go)
    writer.start()
    seen: Counter[str] = Counter()
    try:
        for n in range(12):
            before = rows(db)
            answer = ingest(client, f"NEW-{n:02d}")
            after = rows(db)
            outcome = (answer.status_code, after[0] - before[0], after[1] - before[1])
            assert outcome in {(201, 1, 1), (503, 0, 0)}, (outcome, answer.text)
            seen[str(answer.status_code)] += 1
    finally:
        stop.set()
        writer.join(30)
    assert not writer.is_alive()
    assert_kept_equal_fresh(store)
