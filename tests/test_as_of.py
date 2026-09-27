"""as_of means as_of: the state at a time is built from the entries that had occurred by then.

Liveness (which entries a correction or retraction has killed) is decided over the whole
ledger first, as known now; then only live entries with occurred_utc at or before as_of
count. v0.1.0 folded every live entry whatever its date, so a work order opened in August
made an aircraft "in maintenance" in January, and an inspection done in July was measured
against in January. "Now" queries were never wrong: no entry can be dated in the future.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import uas_workbench.service.app as app_module
from uas_workbench.assistant.recording import load_recordings, replay
from uas_workbench.assistant.tools import stamp
from uas_workbench.evidence import build_pack
from uas_workbench.fleet import load_config
from uas_workbench.fleet.model import Fleet
from uas_workbench.fleet.showcase import showcase
from uas_workbench.fleet.synthetic import generate
from uas_workbench.flight import Unknown
from uas_workbench.ledger import Entry, LedgerError, append, project
from uas_workbench.life import NO_RECORD, board, due_list
from uas_workbench.service.app import create_app
from uas_workbench.service.store import Store

FIXTURES = Path(__file__).parent / "fixtures"
CONFIG = load_config()
T0 = datetime(2026, 5, 1, 9, 0, tzinfo=UTC)
D = timedelta(days=1)
H = 3600.0
FROZEN = datetime(2028, 1, 1, tzinfo=UTC)
MIGRATED_IN_0_2_0 = {"which-aircraft-are-not-serviceable-and-what-stops-each-one"}


def entry(
    id_: int,
    kind: str,
    occurred: datetime,
    details: dict[str, Any] | None = None,
    *,
    subject: str = "A",
    supersedes: int | None = None,
) -> Entry:
    return Entry(
        id=id_,
        subject=subject,
        kind=kind,
        occurred_utc=occurred,
        recorded_utc=occurred,
        entered_by="A. Tester, maintenance",
        statement="a statement",
        details=details or {},
        supersedes=supersedes,
        reason="a reason" if supersedes else None,
        synthetic=False,
    )


def state_at(entries: list[Entry], at: datetime) -> tuple[Any, tuple[str, ...]]:
    p = project(entries, as_of=at)
    due = due_list(
        "A",
        maintenance=p.maintenance.get("A"),
        components=p.components,
        flights_of=lambda k: (),
        policy=CONFIG.life,
        as_of=at,
        tolerance_s=CONFIG.tolerance_s,
    )
    b = board(due)
    return b.status, b.reasons


RECORD = [
    entry(1, "time_in_service.set", T0, {"before_s": 20 * H}),
    entry(2, "inspection.done", T0, {"name": "100-hour inspection", "at_hours_s": 20 * H}),
    entry(3, "inspection.done", T0, {"name": "annual inspection", "at_hours_s": 20 * H}),
]


# 1. The projection at a time holds only what had occurred; work orders open, change and
#    close at their own dates.
def test_work_orders_exist_change_and_close_at_their_own_dates() -> None:
    entries = [
        *RECORD,
        entry(4, "work_order.open", T0 + 2 * D, {"work_id": "WO-1", "state": "in_work"}),
        entry(5, "work_order.state", T0 + 4 * D, {"work_id": "WO-1", "state": "awaiting_parts"}),
        entry(6, "work_order.close", T0 + 5 * D, {"work_id": "WO-1"}),
        entry(7, "work_order.open", T0 + 6 * D, {"work_id": "WO-2", "state": "deferred"}),
    ]
    assert project(entries, as_of=T0 + D).maintenance["A"].work_orders == ()
    at_3 = project(entries, as_of=T0 + 3 * D).maintenance["A"].work_orders
    assert [(w.state, w.opened_utc) for w in at_3] == [("in_work", T0 + 2 * D)]
    at_4h = project(entries, as_of=T0 + 4 * D + D / 2).maintenance["A"].work_orders
    assert [w.state for w in at_4h] == ["awaiting_parts"]
    at_5h = project(entries, as_of=T0 + 5 * D + D / 2)
    assert at_5h.maintenance["A"].work_orders == ()
    assert [w.closed_utc for w in at_5h.work_orders["A"]] == [T0 + 5 * D]
    assert [w.state for w in project(entries, as_of=T0 + 7 * D).maintenance["A"].work_orders] == [
        "deferred"
    ]
    assert project(entries).as_of is None
    assert project(entries, as_of=T0 + 3 * D).as_of == T0 + 3 * D

    assert state_at(entries, T0 + D)[0] == "serviceable"
    assert state_at(entries, T0 + 3 * D)[0] == "in maintenance"
    assert state_at(entries, T0 + 4 * D + D / 2)[0] == "AOG"
    assert state_at(entries, T0 + 5 * D + D / 2)[0] == "serviceable"
    status, reasons = state_at(entries, T0 + 7 * D)
    assert status == "serviceable with deferred defects"
    assert all("WO-1" not in r for r in reasons)


# 2. Inspections and time in service dated after the time do not exist yet; an aircraft with
#    no entry at or before the time has no record.
def test_inspections_and_time_in_service_count_from_their_own_dates() -> None:
    entries = [
        entry(1, "time_in_service.set", T0 + 3 * D, {"before_s": 95 * H}),
        entry(
            2, "inspection.done", T0 + 3 * D, {"name": "100-hour inspection", "at_hours_s": 95 * H}
        ),
        entry(
            3, "inspection.done", T0 + 3 * D, {"name": "annual inspection", "at_hours_s": 95 * H}
        ),
    ]
    assert "A" not in project(entries, as_of=T0 + D).maintenance
    assert state_at(entries, T0 + D)[0] == Unknown(NO_RECORD)
    record = project(entries, as_of=T0 + 4 * D).maintenance["A"]
    assert record.time_in_service_before_s == 95 * H
    assert [i.name for i in record.inspections] == ["100-hour inspection", "annual inspection"]
    assert state_at(entries, T0 + 4 * D)[0] == "serviceable"


# 3. Liveness is decided over the whole ledger first: a retraction or correction entered
#    later still governs earlier times, so a naive date filter would revive the dead entry.
def test_a_later_retraction_or_correction_governs_earlier_times() -> None:
    retracted = [
        *RECORD,
        entry(4, "work_order.open", T0 + 2 * D, {"work_id": "WO-1", "state": "in_work"}),
        entry(5, "retraction", T0 + 9 * D, {}, supersedes=4),
    ]
    p = project(retracted, as_of=T0 + 3 * D)
    assert p.maintenance["A"].work_orders == ()
    assert p.superseded_by == {4: 5}
    assert state_at(retracted, T0 + 3 * D)[0] == "serviceable"

    corrected = [
        entry(1, "time_in_service.set", T0, {"before_s": 30 * H}),
        entry(2, "inspection.done", T0, {"name": "100-hour inspection", "at_hours_s": 30 * H}),
        entry(3, "inspection.done", T0, {"name": "annual inspection", "at_hours_s": 30 * H}),
        entry(4, "time_in_service.set", T0, {"before_s": 40 * H}, supersedes=1),
    ]
    assert project(corrected, as_of=T0 + D).maintenance["A"].time_in_service_before_s == 40 * H
    assert project(corrected, as_of=T0 + D).live == tuple(corrected[1:])


@pytest.fixture(scope="module")
def client() -> TestClient:
    store = Store(":memory:")
    store.add_fleet(generate(CONFIG))
    store.add_fleet(showcase(FIXTURES))
    return TestClient(create_app(store, write_token=None), client=("127.0.0.1", 50000))


# 4. The seeded fleet through the API: SYN-05's work order was opened on 2026-08-07 and
#    SYN-07's on 2026-08-14; before those dates neither aircraft is in work.
@pytest.mark.parametrize(
    ("key", "before", "after", "status"),
    [
        ("SYN-05", "2026-08-01T00:00:00Z", "2026-08-08T00:00:00Z", "in maintenance"),
        ("SYN-07", "2026-08-01T00:00:00Z", "2026-08-15T00:00:00Z", "AOG"),
    ],
)
def test_due_routes_answer_for_the_time_asked(
    client: TestClient, key: str, before: str, after: str, status: str
) -> None:
    early = client.get(f"/aircraft/{key}/due", params={"as_of": before}).json()
    assert early["status"] == "serviceable", early["status_reasons"]
    assert early["status_reasons"] == []
    late = client.get(f"/aircraft/{key}/due", params={"as_of": after}).json()
    assert late["status"] == status
    assert any(f"aircraft {key}" in r for r in late["status_reasons"])
    fleet_early = client.get("/fleet/due", params={"as_of": before}).json()
    assert all(i["aircraft_key"] != key or i["state"] == "due_soon" for i in fleet_early)


# 5. The board honours as_of too, for the list and for one aircraft.
def test_the_board_honours_as_of(client: TestClient) -> None:
    early = {
        a["key"]: a
        for a in client.get("/aircraft", params={"as_of": "2026-08-01T00:00:00Z"}).json()
    }
    assert early["SYN-05"]["status"] == "serviceable"
    assert early["SYN-07"]["status"] == "serviceable"
    assert early["SYN-04"]["status"] == "serviceable"  # its packs are fitted on 2026-08-12
    late = client.get("/aircraft/SYN-05", params={"as_of": "2026-08-08T00:00:00Z"}).json()
    assert late["status"] == "in maintenance"
    now = {a["key"]: a for a in client.get("/aircraft").json()}
    assert now["SYN-05"]["status"] == "in maintenance" and now["SYN-07"]["status"] == "AOG"


# 6. Frozen clock: every recorded run replays identically with the clock at 2028-01-01, so no
#    recorded call depends on the day the test runs. Recordings made before 0.2.0 called
#    list_aircraft without a date; since 0.2.0 it carries the run's as_of, and the recorded
#    result must still equal the dated one, which shows those answers were consistent with
#    their own as_of all along.
class _Frozen(datetime):
    @classmethod
    def now(cls, tz: tzinfo | None = None) -> _Frozen:
        return cls(FROZEN.year, FROZEN.month, FROZEN.day, tzinfo=tz or UTC)


def test_recorded_runs_replay_identically_with_the_clock_frozen_in_2028(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    monkeypatch.setattr(app_module, "datetime", _Frozen)
    assert client.get("/aircraft").json()[0]  # the frozen clock is what "now" means here

    def call(path: str, query: dict[str, str]) -> Any:
        response = client.get(path, params=query)
        response.raise_for_status()
        return response.json()

    for recording in load_recordings():
        answer = replay(recording, call)
        for now, then in zip(answer.calls, recording.calls, strict=True):
            expected_query = dict(then.query)
            if recording.slug in MIGRATED_IN_0_2_0 and then.name == "list_aircraft":
                expected_query["as_of"] = stamp(recording.as_of)
            assert (now.path, now.query) == (then.path, expected_query), recording.slug
            assert now.result == then.result, f"{recording.slug}: {then.name} differs at 2028"
        assert answer.text == recording.answer, recording.slug


# 7. The evidence pack at a time reads only the entries and flights that had occurred, so
#    its ledger hash does not move when a later entry is added, and a pack dated before an
#    aircraft's work order does not carry it.
def test_the_evidence_pack_is_fixed_at_its_as_of() -> None:
    store = Store(":memory:")
    store.add_fleet(generate(CONFIG))
    store.add_fleet(showcase(FIXTURES))
    as_of = datetime(2026, 10, 1, tzinfo=UTC)
    later = datetime(2026, 12, 1, tzinfo=UTC)
    first = build_pack(store, "SYN-02", CONFIG, as_of=as_of, commit=None, generated_utc=later)
    append(
        store,
        Entry(
            id=None,
            subject="SYN-02",
            kind="work_order.open",
            occurred_utc=datetime(2026, 11, 1, tzinfo=UTC),
            recorded_utc=later,
            entered_by="A. Tester, maintenance",
            statement="a later entry must not reach an earlier pack",
            details={"state": "deferred"},
            supersedes=None,
            reason=None,
            synthetic=False,
        ),
        policy=CONFIG.life,
        now=later,
    )
    again = build_pack(store, "SYN-02", CONFIG, as_of=as_of, commit=None, generated_utc=later)
    assert again["ledger_hash"] == first["ledger_hash"]
    assert again["log"] == first["log"] and again["programme"] == first["programme"]
    after = build_pack(store, "SYN-02", CONFIG, as_of=later, commit=None, generated_utc=later)
    assert after["ledger_hash"] != first["ledger_hash"]
    assert after["programme"]["status"] == "serviceable with deferred defects"

    early = build_pack(
        store,
        "SYN-05",
        CONFIG,
        as_of=datetime(2026, 7, 1, tzinfo=UTC),
        commit=None,
        generated_utc=later,
    )
    assert "WO-SYN-05-1" not in json.dumps(early)
    assert early["programme"]["status"] == "serviceable"
    assert early["usage"]["flights"] == []  # every flight of SYN-05 is in August 2026
    assert all(e["occurred_utc"] <= "2026-07-01" for e in early["log"])
    assert early["counts"]["entries"] == len(early["log"]) > 0


# 8. Writes are judged at the entry's own date. A new entry is checked against the state at
#    its occurred_utc (liveness over the whole ledger first, as in every read), then every
#    already recorded later entry of the same subject is checked again with the new one in
#    place; if one of them would no longer hold, the write is refused with that entry's id
#    and nothing is written. A correction keeps the date of the entry it corrects; to move a
#    date, the entry is retracted and a new one written. Before this, a new entry was judged
#    against the ledger as of now: a state change back-dated inside a work order's open
#    window was refused if the order was closed later, and a close dated before a later
#    state change, a remove dated inside a window a later remove closes, a retraction that
#    puts a part on two aircraft at once, and a correction dated away from its target were
#    all accepted.
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
T = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
BY = "A. Tester, maintenance"
PACK = {
    "kind": "battery pack",
    "in_service_since": "2026-08-01",
    "hours_s_before": 0.0,
    "cycles_before": 0,
}


def seeded() -> Store:
    s = Store(":memory:")
    s.add_fleet(generate(CONFIG))
    s.add_fleet(showcase(FIXTURES))
    return s


def write(
    store: Store,
    subject: str,
    kind: str,
    details: dict[str, Any] | None = None,
    *,
    at: datetime = T,
    supersedes: int | None = None,
    reason: str | None = None,
) -> Entry:
    e = Entry(
        id=None,
        subject=subject,
        kind=kind,
        occurred_utc=at,
        recorded_utc=NOW,
        entered_by=BY,
        statement="" if kind == "retraction" else "as described",
        details=details or {},
        supersedes=supersedes,
        reason=reason,
        synthetic=False,
    )
    return append(store, e, policy=CONFIG.life, now=NOW, tolerance_s=CONFIG.tolerance_s)


def refused(store: Store, status: int, *words: str, **kw: Any) -> str:
    before = store.entry_count()
    with pytest.raises(LedgerError) as exc:
        write(store, **kw)
    assert exc.value.status == status, exc.value.detail
    for w in words:
        assert w in exc.value.detail, exc.value.detail
    assert store.entry_count() == before
    return exc.value.detail


def orders_at(store: Store, key: str, at: datetime) -> list[tuple[str, str, datetime | None]]:
    return [(w.work_id, w.state, w.closed_utc) for w in store.projection(at).work_orders[key]]


def windows(store: Store, cid: str) -> list[tuple[str, datetime, datetime | None]]:
    (c,) = [c for c in store.components() if c.id == cid]
    return [(i.aircraft_key, i.from_utc, i.to_utc) for i in c.installations]


def test_a_state_change_back_dated_inside_the_open_window_is_accepted() -> None:
    s = seeded()
    write(s, "SYN-01", "work_order.open", {"work_id": "WO-A", "state": "in_work"})
    write(s, "SYN-01", "work_order.close", {"work_id": "WO-A"}, at=T + 4 * D)
    write(
        s,
        "SYN-01",
        "work_order.state",
        {"work_id": "WO-A", "state": "awaiting_parts"},
        at=T + 2 * D,
    )
    assert orders_at(s, "SYN-01", T + D) == [("WO-A", "in_work", None)]
    assert orders_at(s, "SYN-01", T + 3 * D) == [("WO-A", "awaiting_parts", None)]
    assert orders_at(s, "SYN-01", T + 5 * D) == [("WO-A", "awaiting_parts", T + 4 * D)]
    # Outside the window the refusals stand, and each names the boundary it crossed.
    refused(
        s,
        409,
        "WO-A",
        "closed",
        subject="SYN-01",
        kind="work_order.state",
        details={"work_id": "WO-A", "state": "deferred"},
        at=T + 5 * D,
    )
    refused(
        s,
        409,
        "WO-A",
        "opened",
        subject="SYN-01",
        kind="work_order.state",
        details={"work_id": "WO-A", "state": "deferred"},
        at=T - D,
    )


def test_a_write_that_would_make_a_later_entry_impossible_is_refused_naming_it() -> None:
    s = seeded()
    # A close dated before a later state change would leave that change on a closed order.
    write(s, "SYN-01", "work_order.open", {"work_id": "WO-B", "state": "in_work"})
    later = write(
        s,
        "SYN-01",
        "work_order.state",
        {"work_id": "WO-B", "state": "awaiting_parts"},
        at=T + 3 * D,
    )
    refused(
        s,
        409,
        f"entry {later.id}",
        subject="SYN-01",
        kind="work_order.close",
        details={"work_id": "WO-B"},
        at=T + D,
    )
    assert orders_at(s, "SYN-01", T + 4 * D) == [("WO-B", "awaiting_parts", None)]
    # A second close dated before the recorded close would make that close a close of a
    # closed order; the refusal names the recorded close, not a date that lies after the new one.
    closed = write(s, "SYN-01", "work_order.close", {"work_id": "WO-B"}, at=T + 4 * D)
    detail = refused(
        s,
        409,
        f"entry {closed.id}",
        subject="SYN-01",
        kind="work_order.close",
        details={"work_id": "WO-B"},
        at=T + 2 * D,
    )
    assert "was closed on" not in detail
    # A remove dated inside a window that a later remove already closes would leave the later
    # remove taking a part off an aircraft it is not on.
    write(s, "NEW-E", "component.register", PACK, at=T - D)
    write(s, "NEW-E", "component.install", {"aircraft_key": "SYN-01"})
    removed = write(s, "NEW-E", "component.remove", {"aircraft_key": "SYN-01"}, at=T + 5 * D)
    refused(
        s,
        409,
        f"entry {removed.id}",
        subject="NEW-E",
        kind="component.remove",
        details={"aircraft_key": "SYN-01"},
        at=T + 2 * D,
    )
    assert windows(s, "NEW-E") == [("SYN-01", T, T + 5 * D)]
    # An open-ended install into a gap before a later install elsewhere.
    moved = write(s, "NEW-E", "component.install", {"aircraft_key": "SYN-02"}, at=T + 10 * D)
    refused(
        s,
        409,
        f"entry {moved.id}",
        "SYN-03",
        subject="NEW-E",
        kind="component.install",
        details={"aircraft_key": "SYN-03"},
        at=T + 6 * D,
    )
    # Hours before the first log set lower, back-dated before an inspection that states more
    # hours than the aircraft would then have had.
    done = write(
        s, "SYN-02", "inspection.done", {"name": "100-hour inspection", "at_hours_s": 50 * H}
    )
    refused(
        s,
        409,
        f"entry {done.id}",
        subject="SYN-02",
        kind="time_in_service.set",
        details={"before_s": 0.0},
        at=T - D,
    )


def test_a_retraction_that_would_make_a_later_entry_impossible_is_refused() -> None:
    s = seeded()
    write(s, "NEW-G", "component.register", PACK, at=T - D)
    write(s, "NEW-G", "component.install", {"aircraft_key": "SYN-01"})
    removed = write(s, "NEW-G", "component.remove", {"aircraft_key": "SYN-01"}, at=T + 5 * D)
    moved = write(s, "NEW-G", "component.install", {"aircraft_key": "SYN-02"}, at=T + 10 * D)
    assert removed.id is not None and moved.id is not None
    # Retracting the remove would put the part on SYN-01 and SYN-02 at once.
    refused(
        s,
        409,
        f"entry {moved.id}",
        subject="NEW-G",
        kind="retraction",
        at=NOW - D,
        supersedes=removed.id,
        reason="never removed",
    )
    assert windows(s, "NEW-G") == [("SYN-01", T, T + 5 * D), ("SYN-02", T + 10 * D, None)]
    # In dependency order it goes through: the later install first, then the remove.
    write(s, "NEW-G", "retraction", at=NOW - D, supersedes=moved.id, reason="wrong part")
    write(s, "NEW-G", "retraction", at=NOW - D, supersedes=removed.id, reason="never removed")
    assert windows(s, "NEW-G") == [("SYN-01", T, None)]


def test_a_correction_keeps_the_date_of_the_entry_it_corrects() -> None:
    s = seeded()
    first = write(s, "SYN-01", "time_in_service.set", {"before_s": 100 * H})
    assert first.id is not None
    detail = refused(
        s,
        409,
        str(first.id),
        "2026-09-01 09:00 UTC",
        "retract",
        subject="SYN-01",
        kind="time_in_service.set",
        details={"before_s": 200 * H},
        at=T + 5 * D,
        supersedes=first.id,
        reason="typo",
    )
    assert "2026-09-06" in detail
    assert s.projection(T + 2 * D).maintenance["SYN-01"].time_in_service_before_s == 100 * H
    fixed = write(
        s,
        "SYN-01",
        "time_in_service.set",
        {"before_s": 200 * H},
        supersedes=first.id,
        reason="typo",
    )
    assert fixed.occurred_utc == first.occurred_utc
    assert s.projection(T + 2 * D).maintenance["SYN-01"].time_in_service_before_s == 200 * H
    assert s.projection(T + 6 * D).maintenance["SYN-01"].time_in_service_before_s == 200 * H
    # A correction takes the place of the entry it corrects among entries at the same instant.
    first_2 = write(s, "SYN-02", "time_in_service.set", {"before_s": 100 * H})
    write(s, "SYN-02", "time_in_service.set", {"before_s": 150 * H})
    assert first_2.id is not None
    write(
        s,
        "SYN-02",
        "time_in_service.set",
        {"before_s": 120 * H},
        supersedes=first_2.id,
        reason="misread",
    )
    assert s.maintenance("SYN-02").time_in_service_before_s == 150 * H  # type: ignore[union-attr]
    # To move a date: retract, then record anew. The retraction's own date is not the target's.
    assert fixed.id is not None
    write(s, "SYN-01", "retraction", at=NOW - D, supersedes=fixed.id, reason="wrong day")
    write(s, "SYN-01", "time_in_service.set", {"before_s": 200 * H}, at=T + 5 * D)
    assert s.projection(T + 6 * D).maintenance["SYN-01"].time_in_service_before_s == 200 * H


def test_the_seeded_ledger_holds_entry_by_entry_under_the_time_aware_rules() -> None:
    """Every seeded entry is accepted when appended in order, so the re-check of later
    entries cannot refuse a write over a seeded entry that never held."""
    fleet = generate(CONFIG)
    s = Store(":memory:")
    s.add_fleet(Fleet(fleet.aircraft, fleet.flights, ()))
    s.add_fleet(showcase(FIXTURES))
    assigned: dict[int, int] = {}
    for e in fleet.entries:
        target = assigned[e.supersedes] if e.supersedes is not None else None
        stored = append(
            s,
            Entry(**{**e.__dict__, "id": None, "supersedes": target}),
            policy=CONFIG.life,
            now=NOW,
            tolerance_s=CONFIG.tolerance_s,
        )
        assert e.id is not None and stored.id is not None
        assigned[e.id] = stored.id
    assert s.projection().maintenance == seeded().projection().maintenance
    assert s.projection().components == seeded().projection().components


def test_the_api_accepts_and_refuses_by_the_entry_date() -> None:
    s = seeded()
    api = TestClient(create_app(s, write_token=None), client=("127.0.0.1", 50000))

    def post(kind: str, details: dict[str, Any], at: str) -> Any:
        body = {
            "subject": "SYN-01",
            "kind": kind,
            "occurred_utc": at,
            "entered_by": BY,
            "statement": "as described",
            "details": details,
        }
        return api.post("/entries", json=body)

    assert (
        post(
            "work_order.open", {"work_id": "WO-H", "state": "in_work"}, "2026-09-01T09:00:00Z"
        ).status_code
        == 201
    )
    assert post("work_order.close", {"work_id": "WO-H"}, "2026-09-05T09:00:00Z").status_code == 201
    inside = post(
        "work_order.state", {"work_id": "WO-H", "state": "deferred"}, "2026-09-03T09:00:00Z"
    )
    assert inside.status_code == 201, inside.text
    early = post("work_order.close", {"work_id": "WO-H"}, "2026-09-02T09:00:00Z")
    assert early.status_code == 409 and f"entry {inside.json()['id']}" in early.json()["detail"]
    at = api.get("/aircraft/SYN-01", params={"as_of": "2026-09-04T00:00:00Z"}).json()
    assert at["status"] == "serviceable with deferred defects"
