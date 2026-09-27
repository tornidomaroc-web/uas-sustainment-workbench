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
from uas_workbench.fleet.showcase import showcase
from uas_workbench.fleet.synthetic import generate
from uas_workbench.flight import Unknown
from uas_workbench.ledger import Entry, append, project
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
