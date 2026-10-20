"""The gauges behind /metrics fold the ledger once per refresh, whatever the size of the fleet
(issue #92).

After every write through the service `refresh_gauges` computes each aircraft's due list, and
each due list asked the store for a dated projection twice, each its own snapshot at its own
"now": a write at 3,000 entries read the whole ledger 20 times and folded it 20 times, so one
write cost about (aircraft) x (ledger) and grew with both. The gauges now read one state at one
time: one snapshot, one read of the ledger, one fold, for every aircraft.

What must not move: the values. After own writes, refused writes, a batch rolled back, a commit
from another connection and a batch, the gauges equal a computation made afresh, aircraft by
aircraft as before, on a store opened anew at the same frozen time. And two writes refreshing
at once cannot leave the older state's gauges standing.

The gauges' one snapshot is a read like any other, so the store's own checkpoint (#91) may
wait for it; the last test holds a reader open across the commit that checkpoints, past the
busy timeout, and every write and read sent meanwhile is answered, none of them a 500, and
every write answered 201 is whole.
"""

from __future__ import annotations

import re
import sqlite3
import threading
import time
from collections import Counter
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from uas_workbench.fleet import load_config
from uas_workbench.fleet.model import Aircraft
from uas_workbench.fleet.showcase import showcase
from uas_workbench.fleet.synthetic import generate
from uas_workbench.flight import is_known
from uas_workbench.ledger import Entry, LedgerError, append
from uas_workbench.ledger.project import project
from uas_workbench.service import app as app_module
from uas_workbench.service import store as store_module
from uas_workbench.service.app import DUE_ORDER, create_app, due_view, reconcile_view
from uas_workbench.service.store import Store

FIXTURES = Path(__file__).parent / "fixtures"
CONFIG = load_config()
TOKEN = "t"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
FROZEN = datetime(2026, 10, 1, 12, tzinfo=UTC)


class _Frozen(datetime):
    @classmethod
    def now(cls, tz: tzinfo | None = None) -> _Frozen:
        return cls(FROZEN.year, FROZEN.month, FROZEN.day, FROZEN.hour, tzinfo=tz or UTC)


@pytest.fixture(autouse=True)
def frozen(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_module, "datetime", _Frozen)


def extra_aircraft(n: int) -> list[Aircraft]:
    return [
        Aircraft(
            key=f"XTRA-{i:02d}",
            label=f"extra {i}",
            source="px4",
            synthetic=True,
            licence="CC0-1.0",
            attribution="synthetic",
        )
        for i in range(n)
    ]


def seeded(path: str, extra: int = 0) -> Store:
    store = Store(path)
    store.add_fleet(generate(CONFIG))
    store.add_fleet(showcase(FIXTURES))
    for aircraft in extra_aircraft(extra):
        store.add_aircraft(aircraft)
    return store


@pytest.fixture
def counted(monkeypatch: pytest.MonkeyPatch) -> Counter[str]:
    """How often the store folds entries into a projection and reads the whole ledger."""
    counts: Counter[str] = Counter()
    fold, read = project, Store._entries

    def counting_fold(*args: Any, **kwargs: Any) -> Any:
        counts["fold"] += 1
        return fold(*args, **kwargs)

    def counting_read(self: Store, db: Any, subject: str | None = None) -> list[Entry]:
        if subject is None:
            counts["read"] += 1
        return read(self, db, subject)

    monkeypatch.setattr(store_module, "project", counting_fold)
    monkeypatch.setattr(Store, "_entries", counting_read)
    return counts


def body(subject: str, kind: str, details: dict[str, Any], at: str = "2026-09-20T09:00:00Z") -> Any:
    return {
        "subject": subject,
        "kind": kind,
        "occurred_utc": at,
        "entered_by": "A. Tester, maintenance",
        "statement": "work done as described",
        "details": details,
    }


def entry(subject: str, work_id: str, state: str = "in_work") -> Entry:
    return Entry(
        id=None,
        subject=subject,
        kind="work_order.open",
        occurred_utc=datetime(2026, 9, 21, 9, tzinfo=UTC),
        recorded_utc=FROZEN,
        entered_by="A. Tester, maintenance",
        statement="opened from another connection",
        details={"work_id": work_id, "state": state},
        supersedes=None,
        reason=None,
        synthetic=False,
    )


# ---- one fold per refresh ---------------------------------------------------------------


@pytest.mark.parametrize("extra", [0, 20])
def test_the_gauges_fold_the_ledger_once_whatever_the_fleet(
    tmp_path: Path, counted: Counter[str], extra: int
) -> None:
    store = seeded(str(tmp_path / "s.sqlite"), extra)
    assert len(store.aircraft()) == 9 + extra
    counted.clear()
    app_module.refresh_gauges(store, CONFIG)
    # On 0.6.x before this: two folds and two reads per aircraft, 18 and 58 of each.
    assert dict(counted) == {"fold": 1, "read": 1}


def test_a_write_through_the_service_costs_the_same_for_a_larger_fleet(
    tmp_path: Path, counted: Counter[str]
) -> None:
    per_write = []
    for extra in (0, 20):
        store = seeded(str(tmp_path / f"s{extra}.sqlite"), extra)
        client = TestClient(create_app(store, write_token=TOKEN))
        costs = []
        for n in range(3):
            counted.clear()
            work = {"work_id": f"WO-C-{n}", "state": "deferred"}
            r = client.post("/entries", json=body("SYN-03", "work_order.open", work), headers=AUTH)
            assert r.status_code == 201, r.text
            costs.append(dict(counted))
        per_write.append(costs)
    assert per_write[0] == per_write[1], per_write
    # Validation reads the ledger once; the gauges read and fold it once; the response's
    # whole-ledger projection, dropped by the write, is read and folded once more, and the
    # next write's validation uses it. The first write after start computes it for itself.
    steady = {"fold": 2, "read": 3}
    assert per_write[0] == [{"fold": 3, "read": 4}, steady, steady], per_write


# ---- the values do not move -------------------------------------------------------------

GAUGES = "flights_stored|findings|aircraft_by_status|due_items"
SAMPLE = re.compile(rf"^(uasw_(?:{GAUGES}))(\{{[^}}]*\}})? (\S+)$")


def served(text: str) -> dict[str, str]:
    """The four gauges as /metrics prints them, sample by sample."""
    return {m[1] + (m[2] or ""): m[3] for line in text.splitlines() if (m := SAMPLE.match(line))}


def afresh(path: str) -> dict[str, str]:
    """The four gauges computed the way they always were, aircraft by aircraft and call by call,
    on a store opened anew at the frozen time."""
    store = Store(path)
    try:
        kinds: Counter[str] = Counter()
        statuses: Counter[str] = Counter()
        states: Counter[str] = Counter()
        for aircraft in store.aircraft():
            result, _ = reconcile_view(store, aircraft, CONFIG)
            kinds.update(f.kind for f in result.findings)
            due, state, _ = due_view(store, aircraft, CONFIG, FROZEN)
            statuses[state.status if is_known(state.status) else "unknown"] += 1
            states.update(i.state for i in due.items)
        out = {"uasw_flights_stored": float(store.flight_count())}
        for kind in ("unlogged_flight", "counter_mismatch"):
            out[f'uasw_findings{{kind="{kind}"}}'] = float(kinds.get(kind, 0))
        for status in (*CONFIG.states, "unknown"):
            out[f'uasw_aircraft_by_status{{status="{status}"}}'] = float(statuses.get(status, 0))
        for name in DUE_ORDER:
            out[f'uasw_due_items{{state="{name}"}}'] = float(states.get(name, 0))
        return {k: repr(v) for k, v in out.items()}
    finally:
        store.close()


def test_the_gauges_equal_a_fresh_computation_after_every_kind_of_change(tmp_path: Path) -> None:
    path = str(tmp_path / "s.sqlite")
    store = seeded(path)
    client = TestClient(create_app(store, write_token=TOKEN))
    seen: list[dict[str, str]] = []

    def check(step: str) -> None:
        now = served(client.get("/metrics").text)
        assert now == afresh(path), step
        seen.append(now)

    check("at start")
    work = {"work_id": "WO-OWN", "state": "in_work"}
    r = client.post("/entries", json=body("SYN-01", "work_order.open", work), headers=AUTH)
    assert r.status_code == 201, r.text
    check("after an own write")
    r = client.post(
        "/entries", json=body("SYN-02", "work_order.close", {"work_id": "WO-NONE"}), headers=AUTH
    )
    assert r.status_code == 409, r.text
    check("after a refused write")
    with pytest.raises(RuntimeError), store.batch():
        store.append_entry(entry("SYN-05", "WO-ROLLED"))
        raise RuntimeError("rolled back")
    check("after a batch rolled back")
    other = Store(path)
    append(other, entry("SYN-06", "WO-OUTSIDE"), policy=CONFIG.life, now=FROZEN)
    other.close()
    work = {"work_id": "WO-AFTER-OUT", "state": "deferred"}
    r = client.post("/entries", json=body("SYN-03", "work_order.open", work), headers=AUTH)
    assert r.status_code == 201, r.text
    check("after a commit from another connection and an own write")
    with store.batch():
        store.append_entry(entry("SYN-02", "WO-BATCH-1"))
        store.append_entry(entry("SYN-02", "WO-BATCH-2", "awaiting_parts"))
    work = {"work_id": "WO-AFTER-BATCH", "state": "deferred"}
    r = client.post("/entries", json=body("SYN-03", "work_order.open", work), headers=AUTH)
    assert r.status_code == 201, r.text
    check("after a batch and an own write")
    log = FIXTURES / "px4" / "flight_review_board_validation_2026-06-12_excerpt.ulg"
    with log.open("rb") as f:
        r = client.post(
            "/ingest", data={"aircraft_key": "NEW-01"}, files={"log": (log.name, f)}, headers=AUTH
        )
    assert r.status_code == 201, r.text
    check("after an ingest")
    # Not vacuous: the writes moved the gauges.
    assert len({tuple(sorted(s.items())) for s in seen}) >= 4, seen


def test_two_refreshes_at_once_leave_the_newer_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refresh that read the store before a write and sets its gauges after the write's own
    refresh would leave the gauges describing the state before the write."""
    path = str(tmp_path / "s.sqlite")
    store = seeded(path)
    app_module.refresh_gauges(store, CONFIG)
    paused, go = threading.Event(), threading.Event()
    real = app_module.due_view

    def pausing(*args: Any, **kwargs: Any) -> Any:
        answer = real(*args, **kwargs)
        if args[1].key == "SYN-01" and not paused.is_set():  # the first refresh, after SYN-01
            paused.set()
            assert go.wait(30)
        return answer

    monkeypatch.setattr(app_module, "due_view", pausing)
    first = threading.Thread(None, app_module.refresh_gauges, args=(store, CONFIG))
    first.start()
    assert paused.wait(30)
    # The write lands while the first refresh is under way; its own refresh follows it.
    append(store, entry("SYN-01", "WO-RACE"), policy=CONFIG.life, now=FROZEN)
    second = threading.Thread(None, app_module.refresh_gauges, args=(store, CONFIG))
    second.start()
    second.join(2)  # done by now unless it waits for the first refresh
    go.set()
    first.join(30)
    second.join(30)
    assert not first.is_alive() and not second.is_alive()
    monkeypatch.setattr(app_module, "due_view", real)
    expected = afresh(path)
    from prometheus_client import generate_latest

    assert served(generate_latest().decode()) == expected


# ---- the store's own checkpoint waiting for a reader ------------------------------------

WAIT_MS = 1500  # the busy timeout here, so the wait is short and its end is in the test
EVERY = 5  # own commits per checkpoint here


def test_a_checkpoint_waiting_for_an_old_reader_delays_writes_and_fails_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(store_module, "BUSY_TIMEOUT_MS", WAIT_MS)
    monkeypatch.setattr(store_module, "CHECKPOINT_COMMITS", EVERY)
    path = str(tmp_path / "s.sqlite")
    store = seeded(path)
    client = TestClient(create_app(store, write_token=TOKEN))
    # Fold what seeding left, so the count starts at 0. A batch that writes nothing no longer
    # checkpoints (issue #117), so it is the checkpoint itself that is run here.
    with store._lock:
        store._checkpoint()

    def write(n: int) -> tuple[int, float]:
        work = {"work_id": f"WO-W-{n}", "state": "deferred"}
        t = time.monotonic()
        r = client.post("/entries", json=body("SYN-03", "work_order.open", work), headers=AUTH)
        return r.status_code, time.monotonic() - t

    for n in range(EVERY - 1):
        assert write(n)[0] == 201
    # A reader of another connection begun before the commit that checkpoints: RESTART waits
    # for it, the busy timeout, then gives up and leaves the fold to the next checkpoint.
    reader = sqlite3.connect(path)
    reader.execute("BEGIN")
    reader.execute("SELECT COUNT(*) FROM entries").fetchone()
    answers: dict[str, tuple[int, float]] = {}

    def timed(name: str, call: Any) -> threading.Thread:
        def run() -> None:
            t = time.monotonic()
            answers[name] = (call(), time.monotonic() - t)

        thread = threading.Thread(None, run)
        thread.start()
        return thread

    first = timed("checkpointing write", lambda: write(EVERY - 1)[0])
    time.sleep(0.3)  # the checkpoint is waiting by now
    others = [
        timed("second write", lambda: write(EVERY)[0]),
        timed("health", lambda: client.get("/health").status_code),
        timed("board", lambda: client.get("/aircraft").status_code),
        timed("metrics", lambda: client.get("/metrics").status_code),
    ]
    outside: list[object] = []

    def from_outside() -> None:
        other = Store(path)
        try:
            outside.append(append(other, entry("SYN-06", "WO-OUT"), policy=CONFIG.life, now=FROZEN))
        except Exception as exc:  # what it is is the assertion below
            outside.append(exc)
        finally:
            other.close()

    out = threading.Thread(None, from_outside)
    out.start()
    for thread in (first, *others, out):
        thread.join(60)
    reader.rollback()
    reader.close()
    statuses = {name: answer[0] for name, answer in answers.items()}
    assert statuses == {
        "checkpointing write": 201,
        "second write": 201,
        "health": 200,
        "board": 200,
        "metrics": 200,
    }, answers
    # The checkpoint did wait for the reader: that write took about the busy timeout.
    assert answers["checkpointing write"][1] >= 0.8 * WAIT_MS / 1000, answers
    # The write from another connection either went in whole or was refused with nothing
    # written, never anything else.
    (result,) = outside
    if isinstance(result, Exception):
        assert isinstance(result, LedgerError) and result.status == 503, repr(result)
    # Every write answered 201 is whole: in the ledger, linked in the journal, readable.
    fresh = Store(path)
    ids = {e.details.get("work_id") for e in fresh.entries("SYN-03")}
    fresh.close()
    assert {f"WO-W-{n}" for n in range(EVERY + 1)} <= ids
    verify = client.get("/journal/verify", headers=AUTH).json()
    assert verify["ok"] is True, verify
    assert client.get("/health").json()["status"] == "ok"
    # Once the reader is gone the next checkpoint folds the WAL, and the gauges are fresh.
    for n in range(EVERY + 1, 2 * EVERY + 1):
        assert write(n)[0] == 201
    assert served(client.get("/metrics").text) == afresh(path)
