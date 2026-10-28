"""Two writers on one store file, in two processes, are checked one after the other (issue #65).

`ledger.append` checks an entry against the records and then writes it, holding the store's
write lock across both (issue #60, PR #61). That lock is a `threading.RLock`, the process's
own: a `uasw record` run beside a running service, or two `uasw` commands at once, each held
their own, both checked the entry against the records as they were before either wrote, and
both were written. Two corrections of one entry made at the same instant from two processes
were both accepted in 20 of 20 rounds. On a store file the check now runs inside the write
transaction (`BEGIN IMMEDIATE`), so the second writer waits for the first to commit and is
checked against the records with the first in them; a writer that cannot get the write
transaction within the busy timeout is refused with nothing written.

Every assertion here is about statuses, entry ids and the store's own answers, never an
operating system's error text, a path or a line ending.
"""

from __future__ import annotations

import importlib
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from uas_workbench.fleet import load_config
from uas_workbench.fleet.synthetic import generate
from uas_workbench.ledger import Entry, LedgerError, append
from uas_workbench.ledger.project import project
from uas_workbench.service.app import create_app
from uas_workbench.service.cli import main
from uas_workbench.service.store import Store

CONFIG = load_config()
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
T = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
JOIN_S = 60.0
ROUNDS = 3
# How long each writer sleeps between its check and its write, so the two overlap whenever
# both start within that long of each other; well under the 5 s busy timeout.
HOLD_S = 0.4

VALIDATE = importlib.import_module("uas_workbench.ledger.validate")
STORE = importlib.import_module("uas_workbench.service.store")

# One writer process: widens its own gap between check and write, says it is ready, waits for
# the shared start signal, then runs one `uasw` command and exits with that command's code.
WRITER = """
import importlib, pathlib, sys, time
ready, go, hold = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), float(sys.argv[3])
validate = importlib.import_module("uas_workbench.ledger.validate")
original = validate.validate
def slow(*args, **kwargs):
    checked = original(*args, **kwargs)
    time.sleep(hold)
    return checked
validate.validate = slow
from uas_workbench.service.cli import main
ready.touch()
deadline = time.monotonic() + 60
while not go.exists():
    if time.monotonic() > deadline:
        sys.exit(3)
    time.sleep(0.001)
main(sys.argv[4:])
"""


def hours(before_s: float, supersedes: int | None = None) -> Entry:
    return Entry(
        id=None,
        subject="SYN-03",
        kind="time_in_service.set",
        occurred_utc=T,
        recorded_utc=NOW,
        entered_by="A. Tester, maintenance",
        statement="hours read off the aircraft",
        details={"before_s": before_s},
        supersedes=supersedes,
        reason="typo in the hours" if supersedes is not None else None,
        synthetic=False,
    )


def correction(db: Path, corrected: int, hours_: int) -> list[str]:
    """The `uasw` arguments of one correction of entry `corrected`."""
    return [
        *("--db", str(db), "record", "time-in-service", "SYN-03", str(hours_)),
        *("--by", "A. Tester, maintenance", "--statement", "hours read off the aircraft"),
        *("--supersedes", str(corrected), "--reason", "typo in the hours"),
    ]


def add(store: Store, e: Entry) -> Entry:
    return append(store, e, policy=CONFIG.life, now=NOW, tolerance_s=CONFIG.tolerance_s)


def seeded(path: Path) -> Store:
    store = Store(str(path))
    store.add_fleet(generate(CONFIG))
    return store


def assert_kept_equal_fresh(store: Store) -> None:
    """Every answer the store keeps equals the same answer computed afresh from the file."""
    assert store.journal_state() == store.verify_journal()
    assert store.journal_state().ok
    assert store.readable() is None
    assert store.projection() == project(store.entries())
    with _fresh(store._path) as fresh:
        assert store.journal_state() == fresh.verify_journal()
        assert store.projection() == fresh.projection()


class _fresh:
    """A store opened on the same file for one comparison and closed after it."""

    def __init__(self, path: str) -> None:
        self.store = Store(path)

    def __enter__(self) -> Store:
        return self.store

    def __exit__(self, *exc: object) -> None:
        self.store.close()


@dataclass
class Outcome:
    code: int
    out: str
    err: str


def race(db: Path, work: Path, corrected: int, hold_s: float = HOLD_S) -> list[Outcome]:
    """Two `uasw record` processes correct entry `corrected` at one instant; their outcomes."""
    work.mkdir(parents=True, exist_ok=True)
    go = work / "go"
    go.unlink(missing_ok=True)
    readies = [work / f"ready-{i}" for i in range(2)]
    for r in readies:
        r.unlink(missing_ok=True)
    procs = [
        subprocess.Popen(
            [
                *(sys.executable, "-c", WRITER, str(ready), str(go), str(hold_s)),
                *correction(db, corrected, 101 + i),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for i, ready in enumerate(readies)
    ]
    try:
        deadline = time.monotonic() + JOIN_S
        while not all(r.exists() for r in readies):
            assert time.monotonic() < deadline, "a writer process never got ready"
            assert all(p.poll() is None for p in procs), "a writer process ended early"
            time.sleep(0.01)
        go.touch()
        outcomes = []
        for p in procs:
            out, err = p.communicate(timeout=JOIN_S)
            outcomes.append(Outcome(p.returncode, out.strip(), err.strip()))
        return outcomes
    finally:
        for p in procs:
            if p.poll() is None:
                p.terminate()
                p.communicate()


def check_one_accepted_one_refused(outcomes: list[Outcome], corrected: int, store: Store) -> None:
    assert sorted(o.code for o in outcomes) == [0, 1], outcomes
    accepted = next(o for o in outcomes if o.code == 0)
    refused = next(o for o in outcomes if o.code == 1)
    assert accepted.out.startswith("entry "), outcomes
    winner = int(accepted.out.split()[1])
    assert refused.err.splitlines()[-1] == (
        f"refused (409): entry {corrected} is already superseded by entry {winner}"
    ), outcomes
    corrections = [e.id for e in store.entries("SYN-03") if e.supersedes == corrected]
    assert corrections == [winner]


def test_two_processes_correcting_one_entry_at_once_accept_one_and_refuse_the_other(
    tmp_path: Path,
) -> None:
    """The reproduction of issue #65, through the command line in two processes. A store
    kept open in this process across the rounds sees every round as a commit from outside,
    and its kept answers still equal answers computed afresh."""
    db = tmp_path / "fleet.sqlite"
    store = seeded(db)
    try:
        for n in range(ROUNDS):
            corrected = add(store, hours(100.0 + n)).id
            assert corrected is not None
            assert_kept_equal_fresh(store)
            outcomes = race(db, tmp_path / f"round-{n}", corrected)
            check_one_accepted_one_refused(outcomes, corrected, store)
            assert_kept_equal_fresh(store)
    finally:
        store.close()


def test_two_stores_on_one_file_check_one_after_the_other(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two `Store` objects on one file hold two locks of their own and two connections each,
    as two processes do; both appending a correction of one entry give one entry and one 409."""
    db = tmp_path / "fleet.sqlite"
    first_store = seeded(db)
    second_store = Store(str(db))
    try:
        corrected = add(first_store, hours(100.0)).id
        original: Callable[..., Entry] = VALIDATE.validate

        def slow(*args: Any, **kwargs: Any) -> Entry:
            checked = original(*args, **kwargs)
            time.sleep(HOLD_S)
            return checked

        monkeypatch.setattr(VALIDATE, "validate", slow)
        gate = threading.Barrier(2)
        accepted: list[Entry] = []
        refused: list[LedgerError] = []

        def correct(store: Store, before_s: float) -> None:
            gate.wait()
            try:
                accepted.append(add(store, hours(before_s, supersedes=corrected)))
            except LedgerError as exc:
                refused.append(exc)

        writers = [
            threading.Thread(None, correct, args=(s, b))
            for s, b in ((first_store, 200.0), (second_store, 300.0))
        ]
        for w in writers:
            w.start()
        for w in writers:
            w.join(JOIN_S)
        assert not any(w.is_alive() for w in writers), "a write never finished"
        assert len(accepted) == 1, [e.id for e in accepted]
        assert [(e.status, e.detail) for e in refused] == [
            (409, f"entry {corrected} is already superseded by entry {accepted[0].id}")
        ]
        for store in (first_store, second_store):
            corrections = [e.id for e in store.entries("SYN-03") if e.supersedes == corrected]
            assert corrections == [accepted[0].id]
            assert_kept_equal_fresh(store)
    finally:
        second_store.close()
        first_store.close()


# ---- a writer that cannot get the write transaction --------------------------------------

BUSY = (
    "another connection held the store's write lock for longer than this write waits, a write "
    "or a checkpoint waiting for a read of an older state to end; nothing was written, so send "
    "it again"
)


@pytest.fixture
def held(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """A seeded store file whose write transaction another connection holds, with the
    store's wait for it cut to 0.2 s; yields the file and the entry to correct."""
    monkeypatch.setattr(STORE, "BUSY_TIMEOUT_MS", 200)
    db = tmp_path / "fleet.sqlite"
    store = seeded(db)
    corrected = add(store, hours(100.0)).id
    store.close()
    holder = STORE.sqlite3.connect(str(db), isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        yield db, corrected
    finally:
        holder.execute("ROLLBACK")
        holder.close()


def test_a_write_that_cannot_get_the_write_transaction_is_refused_and_writes_nothing(
    held: tuple[Path, int],
) -> None:
    db, corrected = held
    store = Store(str(db))
    try:
        before = store.entry_count()
        kept = store.journal_state()
        with pytest.raises(LedgerError) as caught:
            add(store, hours(200.0, supersedes=corrected))
        assert (caught.value.status, caught.value.detail) == (503, BUSY)
        # Reads go on while another writer holds the store, and nothing moved.
        assert store.entry_count() == before
        assert store.journal_state() == kept == store.verify_journal()
        assert store.readable() is None
        assert store.projection() == project(store.entries())
    finally:
        store.close()


def test_the_service_answers_503_and_writes_nothing_when_the_store_stays_held(
    held: tuple[Path, int],
) -> None:
    db, corrected = held
    store = Store(str(db))
    try:
        before = store.entry_count()
        client = TestClient(create_app(store, write_token="t"))
        answer = client.post(
            "/entries",
            headers={"Authorization": "Bearer t"},
            json={
                "subject": "SYN-03",
                "kind": "time_in_service.set",
                "occurred_utc": "2026-09-01T09:00:00Z",
                "entered_by": "A. Tester, maintenance",
                "statement": "hours read off the aircraft",
                "details": {"before_s": 200.0},
                "supersedes": corrected,
                "reason": "typo in the hours",
            },
        )
        assert (answer.status_code, answer.json()) == (503, {"detail": BUSY})
        assert store.entry_count() == before
        assert store.journal_state() == store.verify_journal()
    finally:
        store.close()


def test_the_command_line_refuses_with_one_line_when_the_store_stays_held(
    held: tuple[Path, int], capsys: pytest.CaptureFixture[str]
) -> None:
    db, corrected = held
    with pytest.raises(SystemExit) as code:
        main(correction(db, corrected, 102))
    assert code.value.code == 1
    assert capsys.readouterr().err.strip().splitlines() == [f"refused (503): {BUSY}"]


# ---- what the write transaction leaves alone ---------------------------------------------


def every_read(store: Store) -> None:
    """Each read the service and the command line make, the kept answers included."""
    store.entries()
    store.entries("SYN-03")
    store.entry(1)
    store.entry_count()
    store.next_entry_id()
    store.aircraft()
    store.flights("SYN-03")
    store.flight_count()
    store.projection()
    store.projection(NOW)
    store.journal_state()
    store.readable()
    store.verify_journal()
    with store.snapshot():
        store.entries()
        store.projection()


def test_reads_go_on_while_a_write_holds_the_transaction_and_another_waits_for_it(
    tmp_path: Path,
) -> None:
    """One store holds the write transaction across a check; a second store on the file
    (another process, to SQLite) waits for it. Meanwhile every read, on either store and on
    a third thread of the first, finishes: no read takes the store's lock or the file's,
    and neither waiting writer holds anything a reader needs."""
    db = tmp_path / "fleet.sqlite"
    holder = seeded(db)
    other = Store(str(db))
    entered, release = threading.Event(), threading.Event()
    results: list[object] = []

    def hold() -> None:
        with holder.writing():
            entered.set()
            release.wait(JOIN_S)

    def wait_to_write() -> None:
        try:
            results.append(add(other, hours(150.0)))
        except LedgerError as exc:
            results.append(exc)

    holding = threading.Thread(None, hold)
    holding.start()
    try:
        assert entered.wait(JOIN_S)
        waiting = threading.Thread(None, wait_to_write)
        waiting.start()
        for store in (holder, other):
            reading = threading.Thread(None, every_read, args=(store,))
            reading.start()
            reading.join(JOIN_S / 4)
            assert not reading.is_alive(), "a read waited for a write"
        assert waiting.is_alive(), "the second writer did not wait for the first"
    finally:
        release.set()
        holding.join(JOIN_S)
    waiting.join(JOIN_S)
    assert not waiting.is_alive()
    assert [type(r) for r in results] == [Entry]
    for store in (holder, other):
        assert_kept_equal_fresh(store)
    other.close()
    holder.close()


@pytest.mark.parametrize("where", ["memory", "file"])
def test_a_write_is_not_checked_inside_an_open_snapshot(tmp_path: Path, where: str) -> None:
    """A snapshot reads the state it began on; a check made inside one would read that state,
    not the one the write lands on, so it is refused before anything is checked."""
    store = Store(":memory:" if where == "memory" else str(tmp_path / "fleet.sqlite"))
    store.add_fleet(generate(CONFIG))
    before = store.entry_count()
    with store.snapshot(), pytest.raises(RuntimeError):
        add(store, hours(100.0))
    assert store.entry_count() == before
    store.close()


def test_a_refused_check_and_a_failed_write_roll_back_and_move_nothing_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A check that refuses inside the write transaction, and a write that fails after its
    rows went in, both end with the transaction rolled back: nothing written, the kept answers
    as they were and equal to a fresh computation, and the next write goes through."""
    store = seeded(tmp_path / "fleet.sqlite")
    first = add(store, hours(100.0))
    add(store, hours(200.0, supersedes=first.id))
    assert_kept_equal_fresh(store)
    count, kept = store.entry_count(), store.journal_state()
    with pytest.raises(LedgerError) as caught:
        add(store, hours(300.0, supersedes=first.id))
    assert caught.value.status == 409
    assert not store._db.in_transaction
    assert (store.entry_count(), store.journal_state()) == (count, kept)
    assert_kept_equal_fresh(store)

    journal = importlib.import_module("uas_workbench.service.journal")

    def broken(*args: Any, **kwargs: Any) -> str:
        raise OSError("the link could not be written")

    with monkeypatch.context() as m:
        m.setattr(journal, "append_link", broken)
        with pytest.raises(OSError):
            add(store, hours(400.0))
    assert not store._db.in_transaction
    assert (store.entry_count(), store.journal_state()) == (count, kept)
    assert_kept_equal_fresh(store)
    add(store, hours(500.0))
    assert store.entry_count() == count + 1
    assert_kept_equal_fresh(store)
    store.close()
