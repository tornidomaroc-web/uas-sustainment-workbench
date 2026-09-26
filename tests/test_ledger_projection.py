"""The ledger is the only thing written; the records the engine reads are projected from it.

Entries are never edited or deleted. A correction is a new entry that supersedes an older
one with a reason; a retraction supersedes without replacing. An entry is dead when a live
newer entry supersedes it, so undoing a correction revives what it corrected. Live entries
fold, in the order they occurred, into exactly the maintenance records and components of
uas_workbench.life, which does not change.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

from uas_workbench.ledger import Entry, project

T0 = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)
D = timedelta(days=1)


def entry(
    id_: int,
    subject: str,
    kind: str,
    occurred: datetime,
    payload: dict[str, Any] | None = None,
    *,
    supersedes: int | None = None,
    reason: str | None = None,
    statement: str = "a statement",
    synthetic: bool = False,
) -> Entry:
    return Entry(
        id=id_,
        subject=subject,
        kind=kind,
        occurred_utc=occurred,
        recorded_utc=occurred,
        entered_by="A. Tester, maintenance",
        statement=statement,
        payload=payload or {},
        supersedes=supersedes,
        reason=reason,
        synthetic=synthetic,
    )


def test_work_orders_fold_into_open_ones_only_and_keep_their_history() -> None:
    entries = [
        entry(
            1,
            "A",
            "work_order.open",
            T0,
            {"work_id": "WO-1", "state": "awaiting_parts"},
            statement="propeller set on order",
        ),
        entry(2, "A", "work_order.state", T0 + D, {"work_id": "WO-1", "state": "in_work"}),
        entry(
            3,
            "A",
            "work_order.open",
            T0 + 2 * D,
            {"work_id": "WO-2", "state": "deferred"},
            statement="cosmetic cover damage",
        ),
        entry(4, "A", "work_order.close", T0 + 3 * D, {"work_id": "WO-1"}),
    ]
    p = project(entries)
    record = p.maintenance["A"]
    assert [(w.state, w.description, w.opened_utc) for w in record.work_orders] == [
        ("deferred", "cosmetic cover damage", T0 + 2 * D)
    ]
    history = {w.work_id: (w.state, w.closed_utc) for w in p.work_orders["A"]}
    assert history == {"WO-1": ("in_work", T0 + 3 * D), "WO-2": ("deferred", None)}
    assert record.time_in_service_before_s == 0.0 and record.inspections == ()
    assert record.synthetic is False


def test_components_fold_into_installation_windows() -> None:
    entries = [
        entry(
            1,
            "BAT-1",
            "component.register",
            T0 - 30 * D,
            {
                "kind": "battery pack",
                "in_service_since": "2026-01-15",
                "hours_s_before": 3600.0,
                "cycles_before": 12,
            },
        ),
        entry(2, "BAT-1", "component.install", T0, {"aircraft_key": "A"}),
        entry(3, "BAT-1", "component.remove", T0 + 5 * D, {"aircraft_key": "A"}),
        entry(4, "BAT-1", "component.install", T0 + 5 * D, {"aircraft_key": "B"}),
    ]
    (c,) = project(entries).components
    assert (c.id, c.kind, c.in_service_since) == ("BAT-1", "battery pack", date(2026, 1, 15))
    assert (c.hours_s_before, c.cycles_before) == (3600.0, 12)
    assert [(i.aircraft_key, i.from_utc, i.to_utc) for i in c.installations] == [
        ("A", T0, T0 + 5 * D),
        ("B", T0 + 5 * D, None),
    ]


def test_inspections_and_time_in_service_fold_into_the_record() -> None:
    entries = [
        entry(1, "A", "time_in_service.set", T0, {"before_s": 7200.0}),
        entry(
            2,
            "A",
            "inspection.done",
            T0 + D,
            {"name": "100-hour inspection", "at_hours_s": 9000.0, "carried_over_s": 0.0},
        ),
        entry(
            3,
            "A",
            "inspection.done",
            T0 + 2 * D,
            {"name": "annual inspection", "at_hours_s": 9100.0},
        ),
    ]
    record = project(entries).maintenance["A"]
    assert record.time_in_service_before_s == 7200.0
    assert [(i.name, i.done_utc, i.at_hours_s, i.carried_over_s) for i in record.inspections] == [
        ("100-hour inspection", T0 + D, 9000.0, 0.0),
        ("annual inspection", T0 + 2 * D, 9100.0, 0.0),
    ]


def test_a_correction_replaces_and_a_retraction_removes_and_undoing_revives() -> None:
    base = [
        entry(1, "A", "time_in_service.set", T0, {"before_s": 100.0}),
        entry(
            2,
            "A",
            "time_in_service.set",
            T0,
            {"before_s": 200.0},
            supersedes=1,
            reason="typo in the hours",
        ),
    ]
    p = project(base)
    assert p.maintenance["A"].time_in_service_before_s == 200.0
    assert p.superseded_by == {1: 2}
    assert [e.id for e in p.live] == [2]

    retracted = [*base, entry(3, "A", "retraction", T0, supersedes=2, reason="entered twice")]
    p = project(retracted)
    assert p.superseded_by == {2: 3}
    assert p.maintenance["A"].time_in_service_before_s == 100.0  # entry 1 is live again
    assert 3 in {e.id for e in p.live} and 2 not in {e.id for e in p.live}

    nothing = [base[0], entry(2, "A", "retraction", T0, supersedes=1, reason="wrong aircraft")]
    assert "A" not in project(nothing).maintenance


def test_entries_fold_in_the_order_they_occurred_not_the_order_they_were_entered() -> None:
    entries = [
        entry(1, "A", "work_order.open", T0 + 2 * D, {"work_id": "WO-1", "state": "in_work"}),
        entry(2, "A", "work_order.close", T0 + 3 * D, {"work_id": "WO-1"}),
        entry(3, "A", "work_order.open", T0, {"work_id": "WO-0", "state": "deferred"}),
    ]
    p = project(entries)
    assert [w.work_id for w in p.work_orders["A"]] == ["WO-0", "WO-1"]
    assert [w.description for w in p.maintenance["A"].work_orders] == ["a statement"]


def test_synthetic_flags_follow_the_entries() -> None:
    entries = [
        entry(1, "S", "work_order.open", T0, {"work_id": "W", "state": "in_work"}, synthetic=True),
        entry(
            2,
            "BAT-S",
            "component.register",
            T0,
            {
                "kind": "battery pack",
                "in_service_since": "2026-01-01",
                "hours_s_before": 0.0,
                "cycles_before": 0,
            },
            synthetic=True,
        ),
        entry(3, "R", "work_order.open", T0, {"work_id": "W", "state": "in_work"}),
    ]
    p = project(entries)
    assert p.maintenance["S"].synthetic is True and p.maintenance["S"].work_orders[0].synthetic
    assert p.maintenance["R"].synthetic is False
    assert p.components[0].synthetic is True
