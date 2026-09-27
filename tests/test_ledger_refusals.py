"""Every 409 in the write path, asserted as the whole sentence a person reads.

A refusal is the only explanation a person gets for why nothing was written, so its words
are part of the contract, not decoration. Tests elsewhere match a word or an id; a sentence
can be broken around that word and still pass them, as 0.2.0 development showed (an install
dated before registration read "... it was registered on <date> registered"). The first test
holds the sentences 0.1.0 already had, unchanged; the second holds those 0.2.0 added.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from uas_workbench.fleet import load_config
from uas_workbench.fleet.showcase import showcase
from uas_workbench.fleet.synthetic import generate
from uas_workbench.ledger import Entry, LedgerError, append
from uas_workbench.service.store import Store

FIXTURES = Path(__file__).parent / "fixtures"
CONFIG = load_config()
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
T = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
D = timedelta(days=1)
PACK = {
    "kind": "propeller set",
    "in_service_since": "2026-08-01",
    "hours_s_before": 0.0,
    "cycles_before": 0,
}


@pytest.fixture
def store() -> Store:
    s = Store(":memory:")
    s.add_fleet(generate(CONFIG))
    s.add_fleet(showcase(FIXTURES))
    return s


def entry(
    subject: str,
    kind: str,
    details: dict[str, Any] | None = None,
    *,
    at: datetime = T,
    supersedes: int | None = None,
    reason: str | None = None,
) -> Entry:
    return Entry(
        id=None,
        subject=subject,
        kind=kind,
        occurred_utc=at,
        recorded_utc=NOW,
        entered_by="A. Tester, maintenance",
        statement="" if kind == "retraction" else "as described",
        details=details or {},
        supersedes=supersedes,
        reason=reason,
        synthetic=False,
    )


def add(store: Store, e: Entry) -> int:
    stored = append(store, e, policy=CONFIG.life, now=NOW, tolerance_s=CONFIG.tolerance_s)
    assert stored.id is not None
    return stored.id


def sentence(store: Store, e: Entry) -> str:
    before = store.entry_count()
    with pytest.raises(LedgerError) as exc:
        add(store, e)
    assert exc.value.status == 409, exc.value.detail
    assert store.entry_count() == before
    return exc.value.detail


def test_the_refusal_sentences_of_0_1_0_are_kept_word_for_word(store: Store) -> None:
    reg = add(store, entry("NEW-1", "component.register", PACK, at=T - 10 * D))
    assert sentence(store, entry("NEW-1", "component.register", PACK)) == (
        "component NEW-1 is already registered"
    )
    install_early = entry("NEW-1", "component.install", {"aircraft_key": "SYN-01"}, at=T - 20 * D)
    assert sentence(store, install_early) == (
        "NEW-1 cannot be on an aircraft at 2026-08-12 09:00 UTC: it was registered on "
        "2026-08-22 09:00 UTC"
    )
    assert sentence(store, entry("NEW-1", "component.remove", {"aircraft_key": "SYN-01"})) == (
        "NEW-1 is not installed on any aircraft at 2026-09-01 09:00 UTC, not installed on SYN-01"
    )
    fitted = add(store, entry("NEW-1", "component.install", {"aircraft_key": "SYN-01"}))
    elsewhere = entry("NEW-1", "component.install", {"aircraft_key": "SYN-02"}, at=T + D)
    assert sentence(store, elsewhere) == "NEW-1 is installed on SYN-01 since 2026-09-01 09:00 UTC"
    wrong = entry("NEW-1", "component.remove", {"aircraft_key": "SYN-02"}, at=T + D)
    assert sentence(store, wrong) == (
        "NEW-1 is installed on SYN-01 at 2026-09-02 09:00 UTC, not installed on SYN-02"
    )
    retract_reg = entry("NEW-1", "retraction", supersedes=reg, reason="wrong part", at=T + D)
    assert sentence(store, retract_reg) == (
        f"entry {reg} cannot be superseded while entries {fitted} depend on it; correct those first"
    )

    assert sentence(store, entry("SYN-01", "work_order.close", {"work_id": "WO-none"})) == (
        "no work order WO-none is open on SYN-01"
    )
    add(store, entry("SYN-01", "work_order.open", {"work_id": "WO-X", "state": "in_work"}))
    again = entry("SYN-01", "work_order.open", {"work_id": "WO-X", "state": "deferred"}, at=T + D)
    assert sentence(store, again) == "work order WO-X already exists on SYN-01"
    early = entry("SYN-01", "work_order.state", {"work_id": "WO-X", "state": "deferred"}, at=T - D)
    assert sentence(store, early) == (
        "work order WO-X was opened on 2026-09-01 09:00 UTC, after 2026-08-31 09:00 UTC"
    )
    add(store, entry("SYN-01", "work_order.close", {"work_id": "WO-X"}, at=T + 3 * D))
    late = entry(
        "SYN-01", "work_order.state", {"work_id": "WO-X", "state": "deferred"}, at=T + 4 * D
    )
    assert sentence(store, late) == "work order WO-X on SYN-01 was closed on 2026-09-04 09:00 UTC"

    first = add(store, entry("SYN-02", "time_in_service.set", {"before_s": 100.0}))
    other = entry(
        "SYN-03", "time_in_service.set", {"before_s": 1.0}, supersedes=first, reason="typo"
    )
    assert sentence(store, other) == f"entry {first} is about SYN-02, not SYN-03"
    second = add(
        store,
        entry("SYN-02", "time_in_service.set", {"before_s": 200.0}, supersedes=first, reason="x"),
    )
    twice = entry(
        "SYN-02", "time_in_service.set", {"before_s": 300.0}, supersedes=first, reason="y"
    )
    assert sentence(store, twice) == f"entry {first} is already superseded by entry {second}"


def test_the_refusal_sentences_added_in_0_2_0_read_whole(store: Store) -> None:
    first = add(store, entry("SYN-02", "time_in_service.set", {"before_s": 100.0}))
    moved = entry(
        "SYN-02",
        "time_in_service.set",
        {"before_s": 200.0},
        at=T + 5 * D,
        supersedes=first,
        reason="typo",
    )
    assert sentence(store, moved) == (
        f"a correction keeps the date of the entry it corrects: entry {first} happened on "
        "2026-09-01 09:00 UTC and this one says 2026-09-06 09:00 UTC; to move the date, "
        f"retract entry {first} and record a new entry"
    )

    add(store, entry("SYN-01", "work_order.open", {"work_id": "WO-Y", "state": "in_work"}))
    state = add(
        store,
        entry(
            "SYN-01",
            "work_order.state",
            {"work_id": "WO-Y", "state": "awaiting_parts"},
            at=T + 2 * D,
        ),
    )
    close_early = entry("SYN-01", "work_order.close", {"work_id": "WO-Y"}, at=T + D)
    assert sentence(store, close_early) == (
        f"this would make entry {state} impossible (work_order.state on SYN-01, "
        "2026-09-03 09:00 UTC): work order WO-Y on SYN-01 was closed on 2026-09-02 09:00 UTC; "
        f"retract or correct entry {state} first"
    )

    add(store, entry("NEW-2", "component.register", PACK, at=T - D))
    add(store, entry("NEW-2", "component.install", {"aircraft_key": "SYN-01"}))
    off = add(store, entry("NEW-2", "component.remove", {"aircraft_key": "SYN-01"}, at=T + 5 * D))
    inside = entry("NEW-2", "component.remove", {"aircraft_key": "SYN-01"}, at=T + 2 * D)
    assert sentence(store, inside) == (
        f"this would make entry {off} impossible (component.remove on NEW-2, "
        "2026-09-06 09:00 UTC): NEW-2 is not installed on any aircraft at 2026-09-06 09:00 UTC, "
        f"not installed on SYN-01; retract or correct entry {off} first"
    )
    later = add(
        store, entry("NEW-2", "component.install", {"aircraft_key": "SYN-02"}, at=T + 10 * D)
    )
    gap = entry("NEW-2", "component.install", {"aircraft_key": "SYN-03"}, at=T + 6 * D)
    assert sentence(store, gap) == (
        f"this would make entry {later} impossible (component.install on NEW-2, "
        "2026-09-11 09:00 UTC): NEW-2 is installed on SYN-03 since 2026-09-07 09:00 UTC; "
        f"retract or correct entry {later} first"
    )

    done = add(
        store,
        entry("SYN-03", "inspection.done", {"name": "100-hour inspection", "at_hours_s": 360000.0}),
    )
    lower = entry("SYN-03", "time_in_service.set", {"before_s": 0.0}, at=T - D)
    text = sentence(store, lower)
    assert text.startswith(
        f"this would make entry {done} impossible (inspection.done on SYN-03, "
        "2026-09-01 09:00 UTC): at_hours_s 100.0 h is more than the time in service of SYN-03 "
        "at 2026-09-01 09:00 UTC, "
    ), text
    assert text.endswith(f" h; retract or correct entry {done} first"), text
