"""One response, one state of the store.

The records summary of one aircraft reads its entries, the projection, its flights, the board
and the kept journal result, and the fleet summary reads all of that for every aircraft. Each
read was whole, and a write landing between two of them gave a response that no state of the
store ever had: a correction committed between the entries and the projection of one summary
gave the entries of the state before it, the superseded count and the journal length of the
state after it. Now every read a summary makes, the projection and the journal result it shows
included, is inside one snapshot (`Store.snapshot`): a read transaction on the request thread's
own connection, labelled with the state it reads, which holds no write lock, so a write landing
meanwhile lands at once and is in the next summary, never in half of this one.

Every assertion holds on Windows and Linux alike: no path, no OS text, no timing finer than a
thread that must not wait ten seconds.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from uas_workbench.evidence import build_pack, build_records
from uas_workbench.fleet import load_config
from uas_workbench.ledger import Entry
from uas_workbench.service import journal
from uas_workbench.service.cli import main, seed
from uas_workbench.service.store import Store

FIXTURES = Path(__file__).parent / "fixtures"
CONFIG = load_config()
AS_OF = datetime(2026, 10, 1, tzinfo=UTC)
NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
AT_WRITE = datetime(2026, 9, 20, 9, 0, tzinfo=UTC)
KEY = "SYN-01"  # four entries in the seeded store, the first a time_in_service.set
WAIT_S = 10.0
# Each read of the store a summary makes after its first one: a write landed before any of
# them must be in the summary whole or not at all.
READS = ("projection", "flights", "journal_state")


@pytest.fixture
def db(tmp_path: Path) -> str:
    path = str(tmp_path / "fleet.sqlite")
    main(["--db", path, "seed", "--fixtures", str(FIXTURES)])
    return path


def records(store: Store, key: str = KEY) -> dict[str, Any]:
    return build_records(store, key, CONFIG, as_of=AS_OF, commit="abc1234", generated_utc=NOW)


def pack(store: Store, key: str = KEY) -> dict[str, Any]:
    return build_pack(store, key, CONFIG, as_of=AS_OF, commit="abc1234", generated_utc=NOW)


def triple(summary: dict[str, Any]) -> tuple[Any, Any, Any]:
    """What the straddle mixes: entries held, entries superseded, journal links."""
    return (
        summary["maintenance"]["entries"],
        summary["maintenance"]["superseded"],
        summary["integrity"]["length"],
    )


def entry(subject: str, statement: str = "a record", at: datetime = AT_WRITE) -> Entry:
    return Entry(
        id=None,
        subject=subject,
        kind="work_order.open",
        occurred_utc=at,
        recorded_utc=at,
        entered_by="A. Tester, maintenance",
        statement=statement,
        details={"state": "deferred", "work_id": "WO-9"},
        supersedes=None,
        reason=None,
        synthetic=False,
    )


def correction_of(store: Store, key: str) -> Entry:
    """A correction of the aircraft's last inspection entry, which nothing supersedes and which
    supersedes nothing: one more entry held, one more superseded."""
    inspected = [e for e in store.entries(key) if e.kind == "inspection.done"][-1]
    assert inspected.id is not None and inspected.supersedes is None
    return Entry(
        id=None,
        subject=key,
        kind=inspected.kind,
        occurred_utc=inspected.occurred_utc,
        recorded_utc=AT_WRITE,
        entered_by="A. Tester, maintenance",
        statement="the hours at the inspection, read again",
        details={**inspected.details, "at_hours_s": float(inspected.details["at_hours_s"]) + 60.0},
        supersedes=inspected.id,
        reason="a typo in the hours",
        synthetic=False,
    )


WALK = journal.verify  # the walk itself, kept here so a counted walk never counts this file's


def walked_afresh(path: str) -> journal.Verification:
    connection = sqlite3.connect(path)
    try:
        return WALK(connection)
    finally:
        connection.close()


def count_walks(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    seen: list[int] = []
    walk = journal.verify

    def counted(db: sqlite3.Connection, head: str | None = None) -> journal.Verification:
        seen.append(1)
        return walk(db, head)

    monkeypatch.setattr(journal, "verify", counted)
    return seen


class Lander:
    """Lands one entry while a summary is being built: committed by another connection, which
    is in the file at once, or written through the store from another thread, which is in the
    file as soon as the store lets it (at once on a file store, after the summary on a store in
    memory, which has one connection)."""

    def __init__(self, store: Store, path: str | None, through: str) -> None:
        self.store, self.path, self.through = store, path, through
        self.thread: threading.Thread | None = None

    def land(self, e: Entry) -> None:
        if self.through == "another connection":
            assert self.path is not None
            other = Store(self.path)
            other.append_entry(e)
            other.close()
        else:
            self.thread = threading.Thread(None, lambda: self.store.append_entry(e))
            self.thread.start()

    def done(self) -> None:
        if self.thread is not None:
            self.thread.join(WAIT_S)
            assert not self.thread.is_alive(), "the write through the store never landed"


def inject(monkeypatch: pytest.MonkeyPatch, read: str, before_it: Callable[[], None]) -> list[bool]:
    """Run `before_it` once, just before the store's `read` is first called."""
    fired: list[bool] = []
    original = getattr(Store, read)

    def then_read(self: Store, *args: Any, **kwargs: Any) -> Any:
        if not fired:
            fired.append(True)
            before_it()
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Store, read, then_read)
    return fired


# ---- the straddle --------------------------------------------------------------------------


@pytest.mark.parametrize("through", ["another connection", "the store"])
@pytest.mark.parametrize("read", READS)
def test_a_correction_landing_between_the_reads_of_one_summary_never_gives_a_mixed_state(
    db: str, monkeypatch: pytest.MonkeyPatch, read: str, through: str
) -> None:
    """A correction lands just before one of the reads the summary makes after its first. The
    summary must equal the one built before the correction or the one built after it, never a
    third thing: not the entries of before with the superseded count or the journal length of
    after."""
    store = Store(db)
    store.journal_state()
    before = records(store)
    lander = Lander(store, db, through)
    fired = inject(monkeypatch, read, lambda: lander.land(correction_of(store, KEY)))
    during = records(store)
    lander.done()
    monkeypatch.undo()
    after = records(store)
    assert fired, f"the summary never read {read}"
    assert triple(after) == (triple(before)[0] + 1, triple(before)[1] + 1, triple(before)[2] + 1)
    assert during in (before, after), (
        f"a state the store never held: {triple(during)} (entries, superseded, journal links), "
        f"where the store held {triple(before)} before the correction and {triple(after)} after"
    )
    store.close()


def test_a_correction_landing_mid_summary_on_a_store_in_memory_lands_after_it() -> None:
    """A store in memory has one connection, so a write through it waits for the summary and
    lands when it ends: the summary is the state before, the next one the state after."""
    store = Store(":memory:")
    seed(store, FIXTURES)
    before = records(store)
    lander = Lander(store, None, "the store")
    with pytest.MonkeyPatch.context() as patch:
        fired = inject(patch, "projection", lambda: lander.land(correction_of(store, KEY)))
        during = records(store)
    lander.done()
    assert fired and during == before
    assert triple(records(store)) == (
        triple(before)[0] + 1,
        triple(before)[1] + 1,
        triple(before)[2] + 1,
    )
    store.close()


def test_a_correction_landing_between_the_reads_of_the_oso_pack_never_gives_a_mixed_state(
    db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The draft evidence pack for OSO #03 lists the entries, hashes them and reads the
    projection and the flights: the same promise."""
    store = Store(db)
    before = pack(store)
    lander = Lander(store, db, "another connection")
    fired = inject(monkeypatch, "projection", lambda: lander.land(correction_of(store, KEY)))
    during = pack(store)
    monkeypatch.undo()
    after = pack(store)
    assert fired and before["ledger_hash"] != after["ledger_hash"]
    assert during in (before, after), "the pack mixed two states of the store"
    store.close()


# ---- no write lock ----------------------------------------------------------------------------


def test_a_write_issued_while_a_summary_is_built_lands_at_once_and_is_in_the_next_one(
    db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The summary is held open at its flights read; a write through the store issued meanwhile
    finishes while it is still held, so the summary held no write lock; the summary is the
    state before the write and the next one has it."""
    store = Store(db)
    before = records(store)
    started, release = threading.Event(), threading.Event()
    flights = store.flights

    def held(key: str) -> Any:
        started.set()
        release.wait(WAIT_S)
        return flights(key)

    monkeypatch.setattr(store, "flights", held)
    results: list[dict[str, Any]] = []
    building = threading.Thread(None, lambda: results.append(records(store)))
    writing = threading.Thread(None, lambda: store.append_entry(entry(KEY, "lands at once")))
    building.start()
    assert started.wait(WAIT_S)
    writing.start()
    writing.join(WAIT_S)
    assert not writing.is_alive(), "the write waited for the summary"
    assert building.is_alive(), "the summary was not held open"
    release.set()
    building.join(WAIT_S)
    assert not building.is_alive() and results
    assert results[0] == before, "the write that landed mid-build reached the summary"
    assert triple(records(store)) == (
        triple(before)[0] + 1,
        triple(before)[1],
        triple(before)[2] + 1,
    )
    store.close()


# ---- the snapshot ----------------------------------------------------------------------------


def test_a_snapshot_reads_one_state_and_the_kept_answers_describe_it(db: str) -> None:
    store = Store(db)
    other = Store(db)
    kept = store.journal_state()
    with store.snapshot():
        entries, flights = store.entry_count(), store.flight_count()
        live = len(store.projection().live)
        other.append_entry(entry("SYN-02", "outside, mid-snapshot"))
        assert (store.entry_count(), store.flight_count()) == (entries, flights)
        assert len(store.projection().live) == live
        assert store.journal_state() == kept and store.readable() is None
        with store.snapshot():  # nested: the same snapshot
            assert store.entry_count() == entries
    assert store.entry_count() == entries + 1
    assert len(store.projection().live) == live + 1
    assert store.journal_state() == walked_afresh(db) != kept
    other.close()
    store.close()


def test_one_outside_commit_costs_one_walk_even_when_a_snapshot_straddles_it(
    db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The snapshot that straddles the commit shows the journal result it began on, with no
    walk; the first read after it walks once; the twenty after that, not at all."""
    store = Store(db)
    other = Store(db)
    kept = store.journal_state()
    walks = count_walks(monkeypatch)
    with store.snapshot():
        store.entry_count()
        other.append_entry(entry("SYN-02", "outside, mid-snapshot"))
        assert store.journal_state() == kept
    assert sum(walks) == 0
    after = store.journal_state()
    assert sum(walks) == 1 and after.ok and after.length == kept.length + 1
    for _ in range(20):
        assert store.journal_state() == after
    assert sum(walks) == 1
    other.close()
    store.close()


def test_an_own_write_landing_mid_snapshot_is_not_in_it_and_moves_the_kept_head_without_a_walk(
    db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Store(db)
    kept = store.journal_state()
    walks = count_walks(monkeypatch)
    with store.snapshot():
        store.entry_count()
        writing = threading.Thread(None, lambda: store.append_entry(entry("SYN-02", "own")))
        writing.start()
        writing.join(WAIT_S)
        assert not writing.is_alive(), "the write waited for the snapshot"
        assert store.journal_state() == kept
    after = store.journal_state()
    assert after.ok and after.length == kept.length + 1 and after == walked_afresh(db)
    assert sum(walks) == 0, f"{sum(walks)} walks for one write through the store"
    store.close()


def test_a_write_from_the_store_and_one_from_outside_close_together_leave_the_kept_result_a_walk(
    db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An outside commit right before an own one: the kept result cannot be moved on by the
    own link alone, since the state before that link is not the one it described; the next
    read walks, once, and has both."""
    store = Store(db)
    other = Store(db)
    store.journal_state()
    walks = count_walks(monkeypatch)
    other.append_entry(entry("SYN-03", "outside"))
    store.append_entry(entry("SYN-02", "own, right after"))
    assert store.journal_state() == walked_afresh(db)
    assert sum(walks) == 1
    assert store.journal_state() == walked_afresh(db) and sum(walks) == 1
    other.close()
    store.close()


# ---- the label of an own write -----------------------------------------------------------------


def break_a_flight(path: str) -> None:
    """An edit made with sqlite3 that leaves a flight record unreadable and its link broken."""
    connection = sqlite3.connect(path)
    with connection:
        connection.execute("UPDATE flights SET record = 'not JSON' WHERE id = 1")
    connection.close()


def record_outside(path: str) -> None:
    other = Store(path)
    other.append_entry(entry("SYN-03", "outside, mid-write"))
    other.close()


@pytest.mark.parametrize(
    ("outside", "breaks"), [(record_outside, False), (break_a_flight, True)], ids=["record", "edit"]
)
def test_an_outside_commit_at_any_point_of_an_own_write_never_leaves_the_kept_answers_on_it(
    db: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outside: Callable[[str], None],
    breaks: bool,
) -> None:
    """An own write reads the state's label and the writing connection's version before and
    after its commit, to tell whether the kept answers described the state right before it.
    A commit from outside landing just before or just after any one of those reads must leave
    the kept journal result and `readable` equal to a fresh walk: never a length one short,
    never "unchanged" and every record reading where an edit broke the store."""
    import shutil

    seeded = tmp_path / "seeded.sqlite"
    shutil.copyfile(db, seeded)
    at = 0
    while True:
        path = str(tmp_path / f"case{at}.sqlite")
        shutil.copyfile(seeded, path)
        if not own_write_meeting_an_outside_commit(path, at, outside, breaks, monkeypatch):
            break
        at += 1
    assert at >= 6, f"an own write made {at // 2} label and version reads"


def own_write_meeting_an_outside_commit(
    path: str,
    at: int,
    outside: Callable[[str], None],
    breaks: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> bool:
    """One own write, with `outside` committed just before (`at` even) or just after (odd) its
    read number `at // 2` of a label or a version; False when the write made fewer reads."""
    import uas_workbench.service.store as store_module

    store = Store(path)
    record_outside(path)  # the label moved since the store's last own commit
    store.journal_state()
    assert store.readable() is None
    reads = [0]
    data_version, label = store_module._data_version, store._label

    def around(read: Callable[[], Any]) -> Any:
        reads[0] += 1
        if at == 2 * (reads[0] - 1):
            outside(path)
        value = read()
        if at == 2 * (reads[0] - 1) + 1:
            outside(path)
        return value

    monkeypatch.setattr(store, "_label", lambda: around(label))
    monkeypatch.setattr(store_module, "_data_version", lambda c: around(lambda: data_version(c)))
    store.append_entry(entry("SYN-02", "own"))
    monkeypatch.undo()
    reached = at < 2 * reads[0]
    if reached:
        assert store.journal_state() == walked_afresh(path), f"outside commit at {at}"
        assert (store.readable() is not None) is breaks, f"outside commit at {at}"
    store.close()
    return reached


# ---- dated projections ----------------------------------------------------------------------


def test_a_dated_projection_is_folded_once_per_snapshot_and_never_outlives_it(
    db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fleet summary asks every aircraft's summary for the projection at the computation
    date, and the grounded flights at each flight's own time; inside one snapshot, one state,
    each date is one fold, the same one a fresh fold gives. The next snapshot folds its own
    state again."""
    import uas_workbench.service.store as store_module
    from uas_workbench.evidence import build_fleet_records
    from uas_workbench.ledger.project import project

    store = Store(db)
    store.journal_state()
    store.readable()
    folds: list[datetime | None] = []

    def counted(entries: Any, as_of: datetime | None = None) -> Any:
        folds.append(as_of)
        return project(entries, as_of)

    monkeypatch.setattr(store_module, "project", counted)
    store.projection()  # the whole-ledger one, kept
    folds.clear()
    build_fleet_records(store, CONFIG, as_of=AS_OF, commit="abc1234", generated_utc=NOW)
    assert AS_OF in folds
    assert len(folds) == len(set(folds)), f"{len(folds)} folds of {len(set(folds))} dates"
    with store.snapshot():
        assert store.projection(AS_OF) == project(store.entries(), AS_OF)
        live = len(store.projection(AS_OF).live)
    store.append_entry(entry(KEY, "after the snapshot"))
    assert len(store.projection(AS_OF).live) == live + 1
    store.close()
