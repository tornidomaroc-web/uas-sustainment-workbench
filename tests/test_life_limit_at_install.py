"""A part past a life limit cannot be fitted; a part that crosses one while fitted stays.

Installing a component that is past any of its life limits (hours, cycles or calendar) at
the install's own date is refused with 409 and a full sentence. The check is judged at the
entry's own date against the records as they stood then, like every other rule of the write
path: a back-dated install is accepted when the part was under its limits on that date, even
if it is past one now, because the normal life of a part is to cross its limit while fitted,
where the board reports it. Removing such a part stays allowed, and so does registering it:
the ledger records what exists; what it refuses is putting a spent part on an aircraft.

The re-check of later entries covers the rule too: a write that would leave an already
recorded later install over its limit is refused naming that install.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from uas_workbench.fleet import load_config
from uas_workbench.fleet.showcase import showcase
from uas_workbench.fleet.synthetic import generate
from uas_workbench.ledger import Entry, LedgerError, append
from uas_workbench.life import board, due_list
from uas_workbench.service.app import create_app
from uas_workbench.service.store import Store

FIXTURES = Path(__file__).parent / "fixtures"
CONFIG = load_config()
H = 3600.0
D = timedelta(days=1)
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
BY = "A. Tester, maintenance"
# SYN-01 flew three times, on 2026-08-05 and 2026-08-06; nothing before, nothing after. (Five
# on the 0.4.0 seed, so the counts below read 304 and 4 where they now read 302 and 2.)
BEFORE_FLIGHTS = datetime(2026, 8, 1, 9, tzinfo=UTC)
AFTER_FLIGHTS = datetime(2026, 9, 1, 9, tzinfo=UTC)
TAIL = "a life-limited part that has reached its life limit is replaced, not fitted again"


def seeded() -> Store:
    s = Store(":memory:")
    s.add_fleet(generate(CONFIG))
    s.add_fleet(showcase(FIXTURES))
    return s


def entry(subject: str, kind: str, details: dict[str, Any] | None = None, *, at: datetime) -> Entry:
    return Entry(
        id=None,
        subject=subject,
        kind=kind,
        occurred_utc=at,
        recorded_utc=NOW,
        entered_by=BY,
        statement="as described",
        details=details or {},
        supersedes=None,
        reason=None,
        synthetic=False,
    )


def write(store: Store, e: Entry) -> Entry:
    return append(store, e, policy=CONFIG.life, now=NOW, tolerance_s=CONFIG.tolerance_s)


def refused(store: Store, e: Entry) -> str:
    before = store.entry_count()
    with pytest.raises(LedgerError) as exc:
        write(store, e)
    assert exc.value.status == 409, exc.value.detail
    assert store.entry_count() == before
    return exc.value.detail


def battery(cycles_before: int, since: str = "2026-06-01") -> dict[str, Any]:
    return {
        "kind": "battery pack",
        "in_service_since": since,
        "hours_s_before": 0.0,
        "cycles_before": cycles_before,
    }


def status(store: Store, key: str, at: datetime) -> Any:
    p = store.projection(at)
    due = due_list(
        key,
        maintenance=p.maintenance.get(key),
        components=p.components,
        flights_of=store.flights,
        policy=CONFIG.life,
        as_of=at,
        tolerance_s=CONFIG.tolerance_s,
    )
    return board(due)


def test_a_part_that_crosses_its_limit_while_fitted_stays_fitted_and_grounds_the_aircraft() -> None:
    """The normal case: under its limit when fitted, over it after flying; the install stands,
    the board says so, and taking the part off is allowed."""
    s = seeded()
    write(s, entry("BAT-N", "component.register", battery(299), at=BEFORE_FLIGHTS - 30 * D))
    fitted = write(
        s, entry("BAT-N", "component.install", {"aircraft_key": "SYN-01"}, at=BEFORE_FLIGHTS)
    )
    assert fitted.id is not None
    # During SYN-01's first flight the pack has one cycle left and the aircraft is serviceable;
    # once that log has ended the pack has reached its limit, which grounds as past does.
    assert status(s, "SYN-01", datetime(2026, 8, 5, 8, 10, tzinfo=UTC)).status == "serviceable"
    at_limit = status(s, "SYN-01", datetime(2026, 8, 5, 9, tzinfo=UTC))
    assert at_limit.status == "unserviceable"
    assert "battery pack BAT-N on aircraft SYN-01 has reached its 300-cycle life limit" in (
        at_limit.reasons
    )
    grounded = status(s, "SYN-01", AFTER_FLIGHTS)
    assert grounded.status == "unserviceable"
    assert "battery pack BAT-N on aircraft SYN-01 is 2 cycles past its 300-cycle life limit" in (
        grounded.reasons
    )
    off = write(s, entry("BAT-N", "component.remove", {"aircraft_key": "SYN-01"}, at=AFTER_FLIGHTS))
    assert off.id is not None
    assert status(s, "SYN-01", AFTER_FLIGHTS + D).status == "serviceable"
    # Once past its limit it goes nowhere: not back, not elsewhere.
    for key in ("SYN-01", "SYN-02"):
        assert refused(
            s, entry("BAT-N", "component.install", {"aircraft_key": key}, at=AFTER_FLIGHTS + D)
        ) == (
            f"BAT-N cannot be fitted to {key} at 2026-09-02 09:00 UTC: it has flown 302 cycles, "
            f"past its 300-cycle life limit; {TAIL}"
        )


def test_the_install_is_judged_at_its_own_date_so_a_back_dated_install_is_accepted() -> None:
    """Entered on 2026-10-01, dated before SYN-01's flights, when the part had 299 cycles: the
    write is judged against the records as they stood on that date and accepted, although the
    part is past its limit today. Dated after the flights, the same install is refused."""
    s = seeded()
    write(s, entry("BAT-B", "component.register", battery(299), at=BEFORE_FLIGHTS - 30 * D))
    write(s, entry("BAT-B", "component.install", {"aircraft_key": "SYN-01"}, at=BEFORE_FLIGHTS))
    assert status(s, "SYN-01", NOW).status == "unserviceable"  # past its limit today
    write(s, entry("BAT-B", "component.remove", {"aircraft_key": "SYN-01"}, at=AFTER_FLIGHTS))
    # Back-dated into the window before the flights on another aircraft: refused for the
    # overlap, not for the limit, since it was under the limit then.
    overlap = refused(
        s, entry("BAT-B", "component.install", {"aircraft_key": "SYN-02"}, at=BEFORE_FLIGHTS + D)
    )
    assert overlap == "BAT-B is installed on SYN-01 since 2026-08-01 09:00 UTC"
    # Dated after the three flights it is past its limit, whatever aircraft it is meant for.
    late = refused(
        s, entry("BAT-B", "component.install", {"aircraft_key": "SYN-02"}, at=AFTER_FLIGHTS + D)
    )
    assert late == (
        "BAT-B cannot be fitted to SYN-02 at 2026-09-02 09:00 UTC: it has flown 302 cycles, "
        f"past its 300-cycle life limit; {TAIL}"
    )


def test_a_part_exactly_at_its_limit_is_refused_too() -> None:
    """14 CFR 43.10(c): the control method must deter installation of a part "after it has
    reached its life limit". Reached, not only exceeded: a part with nothing left has no
    flight to give a new airframe. Since 0.5.0 the board draws the same line: a fitted part
    that has reached its limit grounds the aircraft (until then it read due soon)."""
    s = seeded()
    write(s, entry("BAT-E", "component.register", battery(300), at=BEFORE_FLIGHTS - 30 * D))
    assert refused(
        s, entry("BAT-E", "component.install", {"aircraft_key": "SYN-01"}, at=BEFORE_FLIGHTS)
    ) == (
        "BAT-E cannot be fitted to SYN-01 at 2026-08-01 09:00 UTC: it has flown 300 cycles, the "
        f"whole of its 300-cycle life limit; {TAIL}"
    )
    # One cycle short it is fitted and flies its last cycle; when that flight's log ends it
    # has reached the limit and the aircraft is unserviceable. The next log puts it past.
    write(s, entry("BAT-F", "component.register", battery(299), at=BEFORE_FLIGHTS - 30 * D))
    write(s, entry("BAT-F", "component.install", {"aircraft_key": "SYN-01"}, at=BEFORE_FLIGHTS))
    flying = status(s, "SYN-01", datetime(2026, 8, 5, 8, 10, tzinfo=UTC))
    assert flying.status == "serviceable" and flying.reasons == ()
    at_limit = status(s, "SYN-01", datetime(2026, 8, 5, 9, tzinfo=UTC))
    assert at_limit.status == "unserviceable"
    assert at_limit.reasons == (
        "battery pack BAT-F on aircraft SYN-01 has reached its 300-cycle life limit",
    )
    past = status(s, "SYN-01", datetime(2026, 8, 6, 9, 8, tzinfo=UTC))
    assert past.status == "unserviceable"
    assert "battery pack BAT-F on aircraft SYN-01 is 1 cycle past its 300-cycle life limit" in (
        past.reasons
    )


def test_a_write_that_would_leave_a_later_install_over_its_limit_is_refused_naming_it() -> None:
    """The re-check of later entries carries the rule. No sequence of accepted writes reaches
    this history (a remove of a part not fitted is refused), so it is built in the store
    directly, as a file altered outside the application could be; the candidate is then
    judged by the ledger and refused for the install it would break."""
    s = seeded()
    write(s, entry("BAT-R", "component.register", battery(299), at=BEFORE_FLIGHTS - 30 * D))
    stray_remove = s.append_entry(
        entry("BAT-R", "component.remove", {"aircraft_key": "SYN-01"}, at=AFTER_FLIGHTS)
    )
    later_install = s.append_entry(
        entry("BAT-R", "component.install", {"aircraft_key": "SYN-02"}, at=AFTER_FLIGHTS + D)
    )
    assert stray_remove.id is not None and later_install.id is not None
    # The candidate opens a window on SYN-01 that the stray remove closes; the three flights
    # inside it would put the part at 302 cycles by the time of the install on SYN-02.
    detail = refused(
        s, entry("BAT-R", "component.install", {"aircraft_key": "SYN-01"}, at=BEFORE_FLIGHTS)
    )
    assert detail == (
        f"this would make entry {later_install.id} impossible (component.install on BAT-R, "
        "2026-09-02 09:00 UTC): BAT-R cannot be fitted to SYN-02 at 2026-09-02 09:00 UTC: it has "
        f"flown 302 cycles, past its 300-cycle life limit; {TAIL}; retract or correct entry "
        f"{later_install.id} first"
    )


def test_every_basis_is_checked_and_the_sentence_names_each_limit_past() -> None:
    s = seeded()
    write(
        s,
        entry(
            "PROP-H",
            "component.register",
            {
                "kind": "propeller set",
                "in_service_since": "2026-06-01",
                "hours_s_before": 300.3 * H,
                "cycles_before": 0,
            },
            at=BEFORE_FLIGHTS,
        ),
    )
    assert refused(
        s, entry("PROP-H", "component.install", {"aircraft_key": "SYN-01"}, at=AFTER_FLIGHTS)
    ) == (
        "PROP-H cannot be fitted to SYN-01 at 2026-09-01 09:00 UTC: it has flown 300.3 h, past "
        f"its 300 h life limit; {TAIL}"
    )
    write(
        s,
        entry(
            "BAT-C",
            "component.register",
            battery(0, "2024-06-30"),
            at=datetime(2026, 6, 1, tzinfo=UTC),
        ),
    )
    assert refused(
        s, entry("BAT-C", "component.install", {"aircraft_key": "SYN-01"}, at=AFTER_FLIGHTS)
    ) == (
        "BAT-C cannot be fitted to SYN-01 at 2026-09-01 09:00 UTC: its 24-calendar-month life "
        f"limit ended on 2026-06-30; {TAIL}"
    )
    # The calendar life is reached when its last day ends. One hour before that, on the last
    # day of the month, the part is within its life and is fitted, as the board calls a
    # fitted part valid that day (0.3.0 refused it with "ends that same day"; 0.5.0 does not).
    write(
        s,
        entry(
            "BAT-C",
            "component.install",
            {"aircraft_key": "SYN-01"},
            at=datetime(2026, 6, 30, 23, tzinfo=UTC),
        ),
    )
    write(s, entry("BAT-D", "component.register", battery(301, "2024-06-30"), at=BEFORE_FLIGHTS))
    assert refused(
        s, entry("BAT-D", "component.install", {"aircraft_key": "SYN-02"}, at=AFTER_FLIGHTS)
    ) == (
        "BAT-D cannot be fitted to SYN-02 at 2026-09-01 09:00 UTC: it has flown 301 cycles, past "
        "its 300-cycle life limit and its 24-calendar-month life limit ended on 2026-06-30; "
        f"{TAIL}"
    )


def test_the_api_answers_409_with_the_sentence_and_writes_nothing() -> None:
    s = seeded()
    api = TestClient(create_app(s, write_token=None), client=("127.0.0.1", 50000))

    def post(subject: str, kind: str, details: dict[str, Any], at: str) -> Any:
        return api.post(
            "/entries",
            json={
                "subject": subject,
                "kind": kind,
                "occurred_utc": at,
                "entered_by": BY,
                "statement": "as described",
                "details": details,
            },
        )

    assert (
        post("BAT-A", "component.register", battery(305), "2026-07-01T09:00:00Z").status_code == 201
    )
    before = s.entry_count()
    r = post("BAT-A", "component.install", {"aircraft_key": "SYN-01"}, "2026-09-01T09:00:00Z")
    assert r.status_code == 409
    assert r.json()["detail"] == (
        "BAT-A cannot be fitted to SYN-01 at 2026-09-01 09:00 UTC: it has flown 305 cycles, past "
        f"its 300-cycle life limit; {TAIL}"
    )
    assert s.entry_count() == before
