"""Impossible input is refused with a status and a sentence, before anything is written.

The rules run on the projection with the new entry included, so a back-dated entry that
would overlap an installation window is refused whatever order it arrives in.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from uas_workbench.fleet import load_config
from uas_workbench.fleet.showcase import ALFA_KEY, PX4_KEY, showcase
from uas_workbench.fleet.synthetic import generate
from uas_workbench.ledger import NOTE, Entry, LedgerError, append
from uas_workbench.service.store import Store

FIXTURES = Path(__file__).parent / "fixtures"
CONFIG = load_config()
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
T = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
D = timedelta(days=1)


@pytest.fixture
def store() -> Store:
    s = Store(":memory:")
    s.add_fleet(generate(CONFIG))
    s.add_fleet(showcase(FIXTURES))
    return s


def draft(
    subject: str,
    kind: str,
    details: dict[str, Any] | None = None,
    *,
    occurred: datetime = T,
    by: str = "A. Tester, maintenance",
    statement: str = "work done as described",
    supersedes: int | None = None,
    reason: str | None = None,
) -> Entry:
    return Entry(
        id=None,
        subject=subject,
        kind=kind,
        occurred_utc=occurred,
        recorded_utc=NOW,
        entered_by=by,
        statement=statement,
        details=details or {},
        supersedes=supersedes,
        reason=reason,
        synthetic=False,
    )


def add(store: Store, e: Entry) -> Entry:
    return append(store, e, policy=CONFIG.life, now=NOW, tolerance_s=CONFIG.tolerance_s)


def refused(store: Store, e: Entry, status: int, *words: str) -> None:
    with pytest.raises(LedgerError) as exc:
        add(store, e)
    assert exc.value.status == status, exc.value.detail
    for w in words:
        assert w in exc.value.detail, exc.value.detail
    assert exc.value.detail[0].islower() or exc.value.detail[0].isupper()


def test_a_valid_entry_is_appended_with_an_id_and_the_note(store: Store) -> None:
    e = add(store, draft("SYN-01", "work_order.open", {"state": "in_work"}))
    assert e.id is not None and e.details["work_id"] == f"WO-{e.id}"
    assert e.synthetic is False and e.recorded_utc == NOW
    assert store.entry(e.id) == e
    assert "does not certify airworthiness" in NOTE and "return to service" in NOTE
    record = store.maintenance("SYN-01")
    assert record is not None and record.work_orders[-1].state == "in_work"
    assert record.synthetic is False  # a real entry makes the record no longer wholly synthetic


def test_unknown_subjects_are_404_and_showcase_aircraft_are_403(store: Store) -> None:
    refused(store, draft("NO-SUCH", "work_order.open", {"state": "in_work"}), 404, "NO-SUCH")
    refused(
        store, draft("NO-PART", "component.install", {"aircraft_key": "SYN-01"}), 404, "NO-PART"
    )
    refused(
        store, draft("BAT-01A", "component.install", {"aircraft_key": "NO-SUCH"}), 404, "NO-SUCH"
    )
    for key in (ALFA_KEY, PX4_KEY):
        refused(store, draft(key, "work_order.open", {"state": "in_work"}), 403, key, "public")
        refused(store, draft("BAT-01A", "component.install", {"aircraft_key": key}), 403, key)


def test_shape_errors_are_422(store: Store) -> None:
    refused(store, draft("SYN-01", "no.such.kind"), 422, "kind")
    refused(store, draft("SYN-01", "work_order.open", {"state": "grounded"}), 422, "state")
    refused(
        store, draft("SYN-01", "work_order.open", {"state": "in_work"}, by="  "), 422, "entered_by"
    )
    refused(
        store,
        draft("SYN-01", "work_order.open", {"state": "in_work"}, statement=""),
        422,
        "statement",
    )
    refused(
        store,
        draft("SYN-01", "work_order.open", {"state": "in_work"}, occurred=NOW + D),
        422,
        "future",
    )
    refused(store, draft("SYN-01", "time_in_service.set", {"before_s": -1.0}), 422, "before_s")
    refused(
        store,
        draft(
            "NEW-1",
            "component.register",
            {
                "kind": "battery pack",
                "in_service_since": "2026-01-01",
                "hours_s_before": 0.0,
                "cycles_before": -3,
            },
        ),
        422,
        "cycles_before",
    )
    refused(
        store,
        draft(
            "NEW-1",
            "component.register",
            {
                "kind": "warp core",
                "in_service_since": "2026-01-01",
                "hours_s_before": 0.0,
                "cycles_before": 0,
            },
        ),
        422,
        "warp core",
    )
    refused(
        store,
        draft("SYN-01", "inspection.done", {"name": "no such inspection"}),
        422,
        "no such inspection",
    )
    refused(
        store, draft("SYN-01", "component.install", {"aircraft_key": "SYN-01"}), 422, "component"
    )


def test_work_order_transitions_are_checked_against_the_projection(store: Store) -> None:
    refused(store, draft("SYN-01", "work_order.close", {"work_id": "WO-9"}), 409, "WO-9", "open")
    opened = add(store, draft("SYN-01", "work_order.open", {"state": "awaiting_parts"}))
    wid = opened.details["work_id"]
    add(
        store,
        draft("SYN-01", "work_order.state", {"work_id": wid, "state": "in_work"}, occurred=T + D),
    )
    refused(
        store, draft("SYN-01", "work_order.open", {"work_id": wid, "state": "in_work"}), 409, wid
    )
    add(store, draft("SYN-01", "work_order.close", {"work_id": wid}, occurred=T + 2 * D))
    refused(
        store, draft("SYN-01", "work_order.close", {"work_id": wid}, occurred=T + 3 * D), 409, wid
    )
    refused(
        store,
        draft(
            "SYN-01", "work_order.state", {"work_id": wid, "state": "deferred"}, occurred=T + 3 * D
        ),
        409,
        wid,
    )
    # A state change dated before the order was opened is refused too.
    refused(
        store,
        draft("SYN-01", "work_order.state", {"work_id": wid, "state": "deferred"}, occurred=T - D),
        409,
        wid,
    )
    assert store.maintenance("SYN-01") is not None
    assert [w.state for w in store.maintenance("SYN-01").work_orders] == []  # type: ignore[union-attr]


def test_a_component_is_on_one_airframe_at_a_time(store: Store) -> None:
    add(
        store,
        draft(
            "NEW-1",
            "component.register",
            {
                "kind": "propeller set",
                "in_service_since": "2026-08-01",
                "hours_s_before": 0.0,
                "cycles_before": 0,
            },
            occurred=T - 10 * D,
        ),
    )
    refused(
        store,
        draft(
            "NEW-1",
            "component.register",
            {
                "kind": "propeller set",
                "in_service_since": "2026-08-01",
                "hours_s_before": 0.0,
                "cycles_before": 0,
            },
        ),
        409,
        "NEW-1",
    )
    refused(
        store, draft("NEW-1", "component.remove", {"aircraft_key": "SYN-01"}), 409, "not installed"
    )
    add(store, draft("NEW-1", "component.install", {"aircraft_key": "SYN-01"}))
    refused(
        store,
        draft("NEW-1", "component.install", {"aircraft_key": "SYN-02"}, occurred=T + D),
        409,
        "SYN-01",
        "installed",
    )
    refused(
        store,
        draft("NEW-1", "component.install", {"aircraft_key": "SYN-01"}, occurred=T + D),
        409,
        "SYN-01",
    )
    refused(
        store,
        draft("NEW-1", "component.remove", {"aircraft_key": "SYN-02"}, occurred=T + D),
        409,
        "SYN-02",
    )
    # Installing before the register date is impossible.
    refused(
        store,
        draft("NEW-1", "component.install", {"aircraft_key": "SYN-02"}, occurred=T - 20 * D),
        409,
        "register",
    )
    add(store, draft("NEW-1", "component.remove", {"aircraft_key": "SYN-01"}, occurred=T + 5 * D))
    add(store, draft("NEW-1", "component.install", {"aircraft_key": "SYN-02"}, occurred=T + 5 * D))
    # A back-dated install that would overlap the closed window on SYN-01 is refused.
    refused(
        store,
        draft("NEW-1", "component.install", {"aircraft_key": "SYN-03"}, occurred=T + 2 * D),
        409,
        "SYN-01",
    )
    (c,) = [c for c in store.components() if c.id == "NEW-1"]
    assert [(i.aircraft_key, i.to_utc is None) for i in c.installations] == [
        ("SYN-01", False),
        ("SYN-02", True),
    ]
    # The part that moved from SYN-01 in the seeded fleet is on SYN-04 now.
    refused(store, draft("BAT-04A", "component.install", {"aircraft_key": "SYN-01"}), 409, "SYN-04")


def test_an_inspection_cannot_be_done_at_more_time_than_the_aircraft_has(store: Store) -> None:
    refused(
        store,
        draft("SYN-01", "inspection.done", {"name": "100-hour inspection", "at_hours_s": 9e9}),
        422,
        "time in service",
    )
    e = add(store, draft("SYN-01", "inspection.done", {"name": "100-hour inspection"}))
    assert e.details["derived"] is True and e.details["at_hours_s"] > 0
    assert e.details["carried_over_s"] == 0.0
    assert store.maintenance("SYN-01").inspections[-1].at_hours_s == e.details["at_hours_s"]  # type: ignore[union-attr]
    # The derived value counts only flight up to the entry's own date.
    early = add(
        store,
        draft(
            "SYN-01",
            "inspection.done",
            {"name": "annual inspection"},
            occurred=datetime(2026, 8, 1, tzinfo=UTC),
        ),
    )
    assert early.details["at_hours_s"] < e.details["at_hours_s"]


def test_corrections_need_a_live_target_of_the_same_subject_and_a_reason(store: Store) -> None:
    first = add(store, draft("SYN-01", "time_in_service.set", {"before_s": 100.0}))
    assert first.id is not None
    refused(
        store,
        draft("SYN-01", "time_in_service.set", {"before_s": 200.0}, supersedes=999, reason="x"),
        404,
        "999",
    )
    refused(
        store,
        draft(
            "SYN-02", "time_in_service.set", {"before_s": 200.0}, supersedes=first.id, reason="x"
        ),
        409,
        "SYN-01",
    )
    refused(
        store,
        draft("SYN-01", "time_in_service.set", {"before_s": 200.0}, supersedes=first.id),
        422,
        "reason",
    )
    refused(store, draft("SYN-01", "retraction", reason="oops", supersedes=None), 422, "supersedes")
    second = add(
        store,
        draft(
            "SYN-01", "time_in_service.set", {"before_s": 200.0}, supersedes=first.id, reason="typo"
        ),
    )
    assert store.maintenance("SYN-01").time_in_service_before_s == 200.0  # type: ignore[union-attr]
    refused(
        store,
        draft(
            "SYN-01",
            "time_in_service.set",
            {"before_s": 300.0},
            supersedes=first.id,
            reason="again",
        ),
        409,
        "superseded",
    )
    assert second.id is not None
    add(store, draft("SYN-01", "retraction", supersedes=second.id, reason="the first was right"))
    assert store.maintenance("SYN-01").time_in_service_before_s == 100.0  # type: ignore[union-attr]


def test_an_entry_with_live_dependents_cannot_be_superseded(store: Store) -> None:
    reg = add(
        store,
        draft(
            "NEW-2",
            "component.register",
            {
                "kind": "battery pack",
                "in_service_since": "2026-08-01",
                "hours_s_before": 0.0,
                "cycles_before": 0,
            },
            occurred=T - D,
        ),
    )
    inst = add(store, draft("NEW-2", "component.install", {"aircraft_key": "SYN-01"}))
    assert reg.id is not None and inst.id is not None
    refused(
        store,
        draft("NEW-2", "retraction", supersedes=reg.id, reason="wrong part"),
        409,
        str(inst.id),
    )
    opened = add(store, draft("SYN-02", "work_order.open", {"state": "in_work"}))
    closed = add(
        store,
        draft("SYN-02", "work_order.close", {"work_id": opened.details["work_id"]}, occurred=T + D),
    )
    assert opened.id is not None and closed.id is not None
    refused(
        store,
        draft("SYN-02", "retraction", supersedes=opened.id, reason="never happened"),
        409,
        str(closed.id),
    )
    add(store, draft("SYN-02", "retraction", supersedes=closed.id, reason="closed by mistake"))
    assert [w.state for w in store.maintenance("SYN-02").work_orders] == ["in_work"]  # type: ignore[union-attr]
    add(store, draft("SYN-02", "retraction", supersedes=opened.id, reason="never happened"))
    assert store.maintenance("SYN-02").work_orders == ()  # type: ignore[union-attr]


def test_nothing_is_written_when_an_entry_is_refused(store: Store) -> None:
    before = len(store.entries())
    refused(store, draft("SYN-01", "work_order.close", {"work_id": "WO-none"}), 409)
    assert len(store.entries()) == before
