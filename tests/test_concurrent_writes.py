"""Two writes made at once are checked as if made one after the other.

`ledger.append` validates an entry against the records and then writes it. Two requests
served at once both validated against the records as they were before either wrote, so two
corrections of the same entry were both accepted where the second, made after the first, is
refused with 409: measured through `uasw serve`, 60 of 60 such pairs both answered 201. The
check and the write it decides now hold the store's write lock together.
"""

from __future__ import annotations

import importlib
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from uas_workbench.fleet import load_config
from uas_workbench.fleet.synthetic import generate
from uas_workbench.ledger import Entry, LedgerError, append
from uas_workbench.service.store import Store

CONFIG = load_config()
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
T = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
JOIN_S = 30.0

# The package re-exports the function under its module's name, so the module is found by name.
VALIDATE = importlib.import_module("uas_workbench.ledger.validate")


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


def add(store: Store, e: Entry) -> Entry:
    return append(store, e, policy=CONFIG.life, now=NOW, tolerance_s=CONFIG.tolerance_s)


@pytest.fixture(params=["memory", "file"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> Any:
    s = Store(":memory:" if request.param == "memory" else str(tmp_path / "fleet.sqlite"))
    s.add_fleet(generate(CONFIG))
    yield s
    s.close()


def slow_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Widen the gap between the check and the write, so two writes made at once overlap."""
    original: Callable[..., Entry] = VALIDATE.validate

    def validate(*args: Any, **kwargs: Any) -> Entry:
        checked = original(*args, **kwargs)
        time.sleep(0.2)
        return checked

    monkeypatch.setattr(VALIDATE, "validate", validate)


def test_two_corrections_of_one_entry_made_at_once_accept_one(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = add(store, hours(100.0))
    slow_validation(monkeypatch)
    gate = threading.Barrier(2)
    accepted: list[Entry] = []
    refused: list[LedgerError] = []

    def correct(before_s: float) -> None:
        gate.wait()
        try:
            accepted.append(add(store, hours(before_s, supersedes=first.id)))
        except LedgerError as exc:
            refused.append(exc)

    writers = [threading.Thread(None, correct, args=(s,)) for s in (200.0, 300.0)]
    for w in writers:
        w.start()
    for w in writers:
        w.join(JOIN_S)
    assert not any(w.is_alive() for w in writers), "a write never finished"
    assert len(accepted) == 1, [e.id for e in accepted]
    assert [e.status for e in refused] == [409], [e.detail for e in refused]
    assert "already superseded" in refused[0].detail
    corrections = [e for e in store.entries("SYN-03") if e.supersedes == first.id]
    assert [e.id for e in corrections] == [accepted[0].id]


def test_reads_go_on_while_a_write_is_checked(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The check holds the write lock; a read beside it, the kept projection's included,
    neither waits for it on a file store nor takes the two locks the other way round."""
    before = len(store.entries("SYN-03"))
    slow_validation(monkeypatch)
    done = threading.Event()
    reads: list[int] = []

    def read() -> None:
        while not done.is_set():
            store.projection()
            reads.append(len(store.entries("SYN-03")))

    reader = threading.Thread(None, read)
    reader.start()
    try:
        writers = [threading.Thread(None, add, args=(store, hours(100.0 + i))) for i in range(3)]
        for w in writers:
            w.start()
        for w in writers:
            w.join(JOIN_S)
        assert not any(w.is_alive() for w in writers), "a write never finished"
    finally:
        done.set()
        reader.join(JOIN_S)
    assert not reader.is_alive(), "a read never finished"
    assert reads and all(before <= n <= before + 3 for n in reads)
    assert len(store.entries("SYN-03")) == before + 3
