"""A logged flight that starts while the records show the aircraft grounded is reported.

Grounded means a known board state of unserviceable, in maintenance or AOG. The state is
judged at the instant the log starts, on everything dated up to and including that instant,
the log itself not yet counted (a log counts from its end): the same answer the board gives
at `as_of=<the log's start>`, so every finding can be checked against the board at the
time its sentence prints.

  reports      one sentence per such flight, naming the aircraft, the log's start, the state
               and its cause; a cue of at most 40 characters beside it, and the cues of the
               cause. The words report what the records show; they do not accuse, and they
               certify nothing.
  never        a log is never refused and its usage counts exactly as before; the board, the
  refuses      due list and every answer the recorded assistant runs read are untouched.
  cannot know  a state that is not known is never a finding: no record at that time, a log
               with no UTC start, a log that does not say whether the aircraft flew. Each is
               listed as not judged, with why. The two public showcase aircraft are never
               judged, whatever the store holds.
  the seed     on the demo seed, unchanged in this change, the finding reports exactly seven
               flights: three by SYN-01 and four by SYN-05.
  mutation     each rule, broken, changes that answer; each property of a finding, broken,
               is refused by the checker the tests share.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import uas_workbench.life.grounded as grounded_module
import uas_workbench.service.app as app_module
from uas_workbench.evidence import build_pack, render_html
from uas_workbench.fleet import load_config
from uas_workbench.fleet.model import Aircraft, Fleet
from uas_workbench.fleet.showcase import ALFA_KEY, PX4_KEY, showcase
from uas_workbench.fleet.synthetic import generate
from uas_workbench.flight import FlightRecord, LifetimeCounter, Maybe, Unknown
from uas_workbench.ledger import Entry, append
from uas_workbench.life import Board, DueList
from uas_workbench.life.cue import CUE_LIMIT, flight_cue, flight_cue_problems
from uas_workbench.life.engine import log_end
from uas_workbench.life.grounded import (
    GROUNDING,
    GroundedFlight,
    GroundedFlights,
    StateAt,
    problems,
)
from uas_workbench.service.app import (
    aircraft_view,
    create_app,
    due_view,
    fleet_grounded,
    grounded_view,
)
from uas_workbench.service.static_export import export_static
from uas_workbench.service.store import Store

FIXTURES = Path(__file__).parent / "fixtures"
CONFIG = load_config()
POLICY = CONFIG.life
H = 3600.0
S = timedelta(seconds=1)
HOUR = timedelta(hours=1)
D = timedelta(days=1)
KEY = "AC-1"
FITTED = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)
T1 = datetime(2026, 3, 10, 10, 0, tzinfo=UTC)
T2 = T1 + D
SPAN_S = 1900.0
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
BY = "A. Tester, maintenance"
WORK = "[synthetic] as described"
# The seven flights of the unchanged demo seed, in the order they started.
SEVEN = [
    ("SYN-01", "2026-08-06 11:07:54", "unserviceable"),
    ("SYN-01", "2026-08-06 12:09:19", "unserviceable"),
    ("SYN-05", "2026-08-07 08:41:42", "in maintenance"),
    ("SYN-05", "2026-08-08 08:54:49", "in maintenance"),
    ("SYN-01", "2026-08-08 12:24:12", "unserviceable"),
    ("SYN-05", "2026-08-10 09:18:30", "in maintenance"),
    ("SYN-05", "2026-08-10 10:02:32", "in maintenance"),
]
SYN_05_WORK = (
    "aircraft SYN-05 is in maintenance since 2026-08-07: [synthetic] low-battery message in "
    "flight 2: inspect the battery connector and the pack before the next flight"
)


# ---- builders ---------------------------------------------------------------------------


def flight(
    n: int,
    utc: Maybe[datetime],
    flight_s: Maybe[float] = 1800.0,
    *,
    key: str = KEY,
    boot: int | None = None,
) -> FlightRecord:
    return FlightRecord(
        source="px4",
        log_ref=f"{key}-{n:02d}",
        licence="CC0",
        attribution="synthetic",
        firmware="test",
        log_span_s=SPAN_S,
        aircraft_key=key,
        utc_start=utc,
        flight_time_s=flight_s,
        arm_cycles=1,
        landings=1,
        battery_mah=Unknown("n/a"),
        battery_wh=Unknown("n/a"),
        fault_events=(),
        boot_count=boot if boot is not None else n,
        lifetime=LifetimeCounter(0.0, 0.0, 0.0),
        synthetic=True,
    )


def store_with(logs: Sequence[FlightRecord], key: str = KEY) -> Store:
    """One synthetic aircraft with its logs and no entries; the test writes the ledger."""
    store = Store(":memory:")
    aircraft = Aircraft(key, "Synthetic test aircraft", "px4", True, "CC0", "synthetic")
    store.add_fleet(Fleet((aircraft,), {key: tuple(logs)}))
    return store


def entry(subject: str, kind: str, details: dict[str, Any] | None = None, *, at: datetime) -> Entry:
    return Entry(
        id=None,
        subject=subject,
        kind=kind,
        occurred_utc=at,
        recorded_utc=NOW,
        entered_by=BY,
        statement=WORK,
        details=details or {},
        supersedes=None,
        reason=None,
        synthetic=True,
    )


def write(store: Store, e: Entry) -> Entry:
    return append(store, e, policy=POLICY, now=NOW, tolerance_s=CONFIG.tolerance_s)


def fit(store: Store, id_: str, kind: str, at: datetime = FITTED, **usage: Any) -> None:
    details = {
        "kind": kind,
        "in_service_since": "2025-06-01",
        "hours_s_before": 0.0,
        "cycles_before": 0,
        **usage,
    }
    write(store, entry(id_, "component.register", details, at=at - D))
    write(store, entry(id_, "component.install", {"aircraft_key": KEY}, at=at))


def with_a_record(logs: Sequence[FlightRecord]) -> Store:
    """The aircraft has a propeller set well inside its life, so its state is known."""
    store = store_with(logs)
    fit(store, "PROP-OK", "propeller set")
    return store


def open_work(store: Store, state: str, at: datetime, work_id: str = "WO-1") -> None:
    write(store, entry(KEY, "work_order.open", {"state": state, "work_id": work_id}, at=at))


def judge(store: Store, key: str = KEY, as_of: datetime | None = NOW) -> GroundedFlights:
    aircraft = store.get_aircraft(key)
    assert aircraft is not None
    return grounded_view(store, aircraft, CONFIG, as_of)[0]


def seeded() -> Store:
    store = Store(":memory:")
    store.add_fleet(generate(CONFIG))
    store.add_fleet(showcase(FIXTURES))
    return store


def seen(findings: Sequence[Any]) -> list[tuple[str, str, str]]:
    return [(f.aircraft_key, f"{f.utc_start:%Y-%m-%d %H:%M:%S}", f.status) for f in findings]


def state_fn(store: Store, aircraft: Aircraft) -> StateAt:
    def state_at(t: datetime) -> tuple[DueList, Board]:
        due, state, _ = due_view(store, aircraft, CONFIG, t)
        return due, state

    return state_at


def all_findings(store: Store) -> list[GroundedFlight]:
    out: list[GroundedFlight] = []
    for a in store.aircraft():
        out.extend(grounded_view(store, a, CONFIG, NOW)[0].findings)
    return sorted(out, key=lambda f: (f.utc_start, f.aircraft_key))


# ---- the demo seed: exactly seven ---------------------------------------------------------


def test_the_demo_seed_holds_exactly_seven_such_flights() -> None:
    store = seeded()
    found = all_findings(store)
    assert seen(found) == SEVEN
    assert found[0].message == (
        "a flight of aircraft SYN-01 was logged from 2026-08-06 11:07:54 UTC while the records "
        "show the aircraft unserviceable at that time: battery pack BAT-04A on aircraft SYN-01 "
        "has reached its 300-cycle life limit"
    )
    assert found[1].reasons == (
        "battery pack BAT-04A on aircraft SYN-01 is 1 cycle past its 300-cycle life limit",
    )
    assert found[4].reasons == (
        "battery pack BAT-04A on aircraft SYN-01 is 2 cycles past its 300-cycle life limit",
    )
    assert found[2].message == (
        "a flight of aircraft SYN-05 was logged from 2026-08-07 08:41:42 UTC while the records "
        f"show the aircraft in maintenance at that time: {SYN_05_WORK}"
    )
    assert all(f.reasons == (SYN_05_WORK,) for f in found if f.aircraft_key == "SYN-05")
    assert [f.cue for f in found[:3]] == [
        "flown unserviceable 2026-08-06 11:07",
        "flown unserviceable 2026-08-06 12:09",
        "flown in maintenance 2026-08-07 08:41",
    ]
    assert found[0].cause_cues == ("BAT-04A reached 300-cycle limit",)
    assert found[2].cause_cues == ("in maintenance since 2026-08-07",)


def test_every_log_of_the_seed_is_judged_or_listed_as_not_judged_with_why() -> None:
    store = seeded()
    judged = {a.key: judge(store, a.key) for a in store.aircraft()}
    counts = {k: (v.logs, v.judged, len(v.findings), len(v.not_judged)) for k, v in judged.items()}
    assert counts == {
        ALFA_KEY: (3, 0, 0, 3),
        PX4_KEY: (1, 0, 0, 1),
        "SYN-01": (5, 5, 3, 0),
        "SYN-02": (6, 6, 0, 0),
        "SYN-03": (7, 6, 0, 1),  # one duplicate upload
        "SYN-04": (3, 3, 0, 0),
        "SYN-05": (6, 6, 4, 0),
        "SYN-06": (3, 0, 0, 3),  # no maintenance record; one log has no UTC start
        "SYN-07": (6, 6, 0, 0),
    }  # fmt: skip
    whys = [n.why for n in judged["SYN-06"].not_judged]
    assert whys == [
        "the state of aircraft SYN-06 at 2026-08-06 13:00:00 UTC is not known: no maintenance "
        "record entered for this aircraft",
        "the state of aircraft SYN-06 at 2026-08-08 15:30:16 UTC is not known: no maintenance "
        "record entered for this aircraft",
        "log SYN-06-2026-08-08-02.synthetic has no UTC start (no GPS fix with a valid week in "
        "the log), so the state when it began cannot be looked up",
    ]
    (duplicate,) = judged["SYN-03"].not_judged
    assert duplicate.why == (
        "log SYN-03-2026-08-11-01.synthetic is a duplicate of "
        "SYN-03-2026-08-11-01-upload2.synthetic and is not judged twice"
    )


def test_each_finding_agrees_with_the_board_at_the_log_start() -> None:
    """What a reader can check: the board at `as_of=<the log's start>` says the same state
    and holds the same reasons."""
    store = seeded()
    for f in all_findings(store):
        aircraft = store.get_aircraft(f.aircraft_key)
        assert aircraft is not None
        row = aircraft_view(store, aircraft, CONFIG, f.utc_start)
        assert row.status == f.status
        assert set(f.reasons) <= set(row.status_reasons)
        assert problems(f, state_fn(store, aircraft)) == []


# ---- the hand-built fixture: each state ---------------------------------------------------


def test_a_flight_on_a_part_that_has_reached_its_limit_is_reported_as_unserviceable() -> None:
    store = with_a_record([flight(1, T1), flight(2, T2)])
    fit(store, "BAT-ON", "battery pack", cycles_before=299)
    result = judge(store)
    assert (result.logs, result.judged, result.not_judged) == (2, 2, ())
    (found,) = result.findings  # the 300th cycle itself is a permitted flight
    assert (found.log_ref, found.utc_start, found.status) == ("AC-1-02", T2, "unserviceable")
    assert found.reasons == (
        "battery pack BAT-ON on aircraft AC-1 has reached its 300-cycle life limit",
    )
    assert found.message == (
        "a flight of aircraft AC-1 was logged from 2026-03-11 10:00:00 UTC while the records "
        "show the aircraft unserviceable at that time: battery pack BAT-ON on aircraft AC-1 has "
        "reached its 300-cycle life limit"
    )
    assert found.cue == "flown unserviceable 2026-03-11 10:00"
    assert found.cause_cues == ("BAT-ON reached 300-cycle limit",)


def test_a_flight_with_a_work_order_in_work_is_reported_as_in_maintenance() -> None:
    store = with_a_record([flight(1, T1), flight(2, T2)])
    open_work(store, "in_work", T1 - HOUR)
    write(store, entry(KEY, "work_order.close", {"work_id": "WO-1"}, at=T1 + 2 * HOUR))
    (found,) = judge(store).findings  # the second flight follows the close
    assert (found.log_ref, found.status) == ("AC-1-01", "in maintenance")
    assert found.message == (
        "a flight of aircraft AC-1 was logged from 2026-03-10 10:00:00 UTC while the records "
        "show the aircraft in maintenance at that time: aircraft AC-1 is in maintenance since "
        "2026-03-10: [synthetic] as described"
    )
    assert found.cue == "flown in maintenance 2026-03-10 10:00"
    assert found.cause_cues == ("in maintenance since 2026-03-10",)


def test_a_flight_while_awaiting_parts_is_reported_as_aog() -> None:
    store = with_a_record([flight(1, T1)])
    open_work(store, "awaiting_parts", T1 - HOUR)
    (found,) = judge(store).findings
    assert found.status == "AOG"
    assert found.message == (
        "a flight of aircraft AC-1 was logged from 2026-03-10 10:00:00 UTC while the records "
        "show the aircraft AOG at that time: aircraft AC-1 is on the ground awaiting parts "
        "since 2026-03-10: [synthetic] as described"
    )
    assert found.cue == "flown AOG 2026-03-10 10:00"
    assert found.cause_cues == ("AOG awaiting parts since 2026-03-10",)


def test_every_grounding_cause_at_that_moment_is_named_and_nothing_else() -> None:
    """AOG with a spent pack and a deferred defect: the two causes that ground are in the
    sentence, worst first; the deferred defect, which does not ground, is not."""
    store = with_a_record([flight(1, T1)])
    fit(store, "BAT-ON", "battery pack", cycles_before=299)
    store.add_flight(KEY, flight(0, T1 - D))  # the pack's 300th cycle, the day before
    open_work(store, "awaiting_parts", T1 - HOUR, "WO-1")
    open_work(store, "deferred", T1 - HOUR, "WO-2")
    (found,) = [f for f in judge(store).findings if f.utc_start == T1]
    assert found.status == "AOG"
    assert found.reasons == (
        "aircraft AC-1 is on the ground awaiting parts since 2026-03-10: [synthetic] as described",
        "battery pack BAT-ON on aircraft AC-1 has reached its 300-cycle life limit",
    )
    assert found.message.endswith("at that time: " + "; ".join(found.reasons))
    assert found.cause_cues == (
        "AOG awaiting parts since 2026-03-10",
        "BAT-ON reached 300-cycle limit",
    )


def test_states_that_do_not_ground_are_not_findings() -> None:
    """Serviceable, and serviceable with deferred defects (a deferred defect, an inspection
    overflown inside its tolerance): the aircraft may fly, and nothing is reported."""
    plain = with_a_record([flight(1, T1)])
    assert (judge(plain).judged, judge(plain).findings) == (1, ())
    deferred = with_a_record([flight(1, T1)])
    open_work(deferred, "deferred", T1 - HOUR)
    assert (judge(deferred).judged, judge(deferred).findings) == (1, ())
    tolerance = with_a_record([flight(1, T1)])
    write(tolerance, entry(KEY, "time_in_service.set", {"before_s": 104.0 * H}, at=FITTED))
    aircraft = tolerance.get_aircraft(KEY)
    assert aircraft is not None
    assert aircraft_view(tolerance, aircraft, CONFIG, T1).status == (
        "serviceable with deferred defects"
    )
    assert (judge(tolerance).judged, judge(tolerance).findings) == (1, ())


# ---- cannot know ---------------------------------------------------------------------------


def test_a_flight_before_any_record_exists_is_not_judged() -> None:
    """The aircraft's first entries are dated after its first flight: at that flight no
    record existed, the state is not known, and nothing is reported. The second flight, with
    the order open, is."""
    store = store_with([flight(1, T1), flight(2, T2)])
    fit(store, "PROP-OK", "propeller set", at=T1 + 6 * HOUR)
    open_work(store, "in_work", T1 + 12 * HOUR)
    result = judge(store)
    assert (result.logs, result.judged) == (2, 1)
    assert [f.log_ref for f in result.findings] == ["AC-1-02"]
    (unknown,) = result.not_judged
    assert (unknown.log_ref, unknown.utc_start) == ("AC-1-01", T1)
    assert unknown.why == (
        "the state of aircraft AC-1 at 2026-03-10 10:00:00 UTC is not known: no maintenance "
        "record entered for this aircraft"
    )


def test_a_log_that_cannot_be_placed_or_shows_no_flight_is_not_judged() -> None:
    """All of these lie in an open work order's window, and none is a finding: a log with no
    UTC start cannot be placed in time; a log with 0 s airborne is a ground run, which
    maintenance does; a log that cannot say whether the aircraft flew is not called a flight;
    the same log uploaded twice is one flight."""
    logs = [
        flight(1, Unknown("no GPS fix")),
        flight(2, T1, 0.0),
        flight(3, T1 + 2 * HOUR, Unknown("vehicle_land_detected has no landed field")),
        flight(4, T2),
        flight(5, T2 + timedelta(seconds=0.2), boot=4),
    ]
    store = with_a_record(logs)
    open_work(store, "in_work", T1 - HOUR)
    result = judge(store)
    assert (result.logs, result.judged) == (5, 1)
    assert [f.log_ref for f in result.findings] == ["AC-1-04"]
    assert [(n.log_ref, n.why) for n in result.not_judged] == [
        ("AC-1-02", "log AC-1-02 shows no flight: 0 s airborne"),
        (
            "AC-1-03",
            "log AC-1-03 does not say whether the aircraft flew (vehicle_land_detected has no "
            "landed field)",
        ),
        ("AC-1-05", "log AC-1-05 is a duplicate of AC-1-04 and is not judged twice"),
        (
            "AC-1-01",
            "log AC-1-01 has no UTC start (no GPS fix), so the state when it began cannot be "
            "looked up",
        ),
    ]


def test_the_two_showcase_aircraft_are_never_judged_whatever_the_store_holds() -> None:
    """They hold no ledger entries and the write rules refuse any (403), so their state is not
    known. Even with an entry put into the store directly, as a file altered outside the
    application could be, their real flights are not judged: the same entry on a synthetic
    aircraft with the same logs is reported, which shows the exclusion is what holds."""
    store = seeded()
    for key in (ALFA_KEY, PX4_KEY):
        result = judge(store, key)
        assert result.judged == 0 and result.findings == ()
        assert {n.why for n in result.not_judged} == {
            f"aircraft {key} is a public showcase aircraft: no maintenance record is held or "
            "accepted for it, so its flights are not judged"
        }
    early = datetime(2018, 7, 1, tzinfo=UTC)
    forced = dataclasses.replace(
        entry(ALFA_KEY, "work_order.open", {"state": "in_work", "work_id": "WO-X"}, at=early),
        synthetic=False,
    )
    store.append_entry(forced)
    aircraft = store.get_aircraft(ALFA_KEY)
    assert aircraft is not None
    assert aircraft_view(store, aircraft, CONFIG, NOW).status == "in maintenance"
    assert judge(store, ALFA_KEY).findings == () and judge(store, ALFA_KEY).judged == 0
    assert all(f.aircraft_key != ALFA_KEY for f in fleet_grounded(store, CONFIG, NOW).findings)
    # The same logs under a synthetic key, with the same order open: reported.
    twin = store_with([dataclasses.replace(r, synthetic=True) for r in store.flights(ALFA_KEY)])
    twin.append_entry(dataclasses.replace(forced, subject=KEY, synthetic=True))
    assert len(judge(twin).findings) == 3


# ---- the instant that is judged ------------------------------------------------------------


@pytest.mark.parametrize(
    ("opened", "reported"),
    [(T1 + S, False), (T1, True), (T1 - S, True)],
    ids=["log starts the second before the order opens", "same instant", "second after"],
)
def test_the_state_is_judged_at_the_instant_the_log_starts(
    opened: datetime, reported: bool
) -> None:
    store = with_a_record([flight(1, T1)])
    open_work(store, "in_work", opened)
    assert bool(judge(store).findings) is reported


def test_a_log_is_not_judged_on_its_own_cycle_but_is_on_one_that_ended_as_it_began() -> None:
    """The pack's 300th cycle is a permitted flight: its own cycle counts when its log ends.
    A log that starts at the very instant that log ends starts on a pack with nothing left."""
    first = flight(1, T1)
    end = log_end(first)
    assert isinstance(end, datetime)
    store = with_a_record([first, flight(2, end)])
    fit(store, "BAT-ON", "battery pack", cycles_before=299)
    assert [f.log_ref for f in judge(store).findings] == ["AC-1-02"]


def test_a_part_removed_before_the_flight_or_fitted_after_it_is_no_cause() -> None:
    store = with_a_record([flight(1, T1), flight(2, T2), flight(3, T2 + D)])
    fit(store, "BAT-ON", "battery pack", cycles_before=299)
    write(store, entry("BAT-ON", "component.remove", {"aircraft_key": KEY}, at=T2 - HOUR))
    assert judge(store).findings == ()  # reached at the end of log 1, off before log 2


def test_only_logs_that_had_ended_by_the_date_asked_are_judged() -> None:
    store = with_a_record([flight(1, T1), flight(2, T2)])
    open_work(store, "in_work", T1 - HOUR)
    assert [f.log_ref for f in judge(store, as_of=T1 + 60 * S).findings] == []
    assert judge(store, as_of=T1 + 60 * S).logs == 0
    ended = T1 + timedelta(seconds=SPAN_S)
    assert [f.log_ref for f in judge(store, as_of=ended).findings] == ["AC-1-01"]
    assert [f.log_ref for f in judge(store).findings] == ["AC-1-01", "AC-1-02"]


def test_an_entry_dated_back_makes_an_earlier_flight_a_finding_and_a_retraction_unmakes_it() -> (
    None
):
    """Valid time, as everywhere: the state at the log's start is what the ledger says today
    about that time. A finding says the records disagree with the log, not who knew what when."""
    store = with_a_record([flight(1, T1)])
    assert judge(store).findings == ()
    order = write(
        store, entry(KEY, "work_order.open", {"state": "in_work", "work_id": "WO-1"}, at=T1 - D)
    )
    assert len(judge(store).findings) == 1
    assert order.id is not None
    retraction = dataclasses.replace(
        entry(KEY, "retraction", at=T1 - D),
        supersedes=order.id,
        reason="entered on the wrong aircraft",
    )
    retraction = dataclasses.replace(retraction, statement="")
    write(store, retraction)
    assert judge(store).findings == ()


# ---- reports, never refuses ----------------------------------------------------------------


def test_a_log_for_a_grounded_aircraft_is_ingested_counted_and_reported() -> None:
    """The service accepts the upload (201), counts its flight time and its cycle as for any
    log, and reports it. Nothing in the write path looks at the board."""
    log = FIXTURES / "px4" / "flight_review_board_validation_2026-06-12_excerpt.ulg"
    store = store_with([])
    fit(store, "BAT-ON", "battery pack", at=datetime(2026, 6, 1, 9, tzinfo=UTC), cycles_before=10)
    open_work(store, "in_work", datetime(2026, 6, 10, 9, tzinfo=UTC))
    api = TestClient(create_app(store, write_token=None), client=("127.0.0.1", 50000))
    with log.open("rb") as f:
        uploaded = api.post(
            "/ingest",
            data={"aircraft_key": KEY},
            files={"log": (log.name, f, "application/octet-stream")},
        )
    assert uploaded.status_code == 201, uploaded.text
    due = api.get(f"/aircraft/{KEY}/due", params={"as_of": "2026-10-01T00:00:00Z"}).json()
    cycles = next(i for i in due["items"] if i["basis"] == "cycles")
    assert cycles["used"] == 11  # the flight counted, as any flight does
    assert due["time_in_service_s"] > 190
    body = api.get(f"/aircraft/{KEY}/grounded-flights").json()
    assert [f["status"] for f in body["findings"]] == ["in maintenance"]
    assert body["findings"][0]["message"].startswith(
        "a flight of aircraft AC-1 was logged from 2026-06-12 03:28:15 UTC while the records "
        "show the aircraft in maintenance at that time: "
    )


def test_the_board_and_the_due_list_do_not_carry_the_finding() -> None:
    """The board is the state now and its reasons; a past flight is neither. The row's
    `findings` stays the count of reconciliation findings, as the recorded runs read it."""
    store = seeded()
    api = TestClient(create_app(store))
    rows = {
        r["key"]: r for r in api.get("/aircraft", params={"as_of": "2026-10-01T00:00:00Z"}).json()
    }
    assert (rows["SYN-01"]["status"], rows["SYN-01"]["status_reasons"]) == ("serviceable", [])
    assert rows["SYN-01"]["findings"] == 0 and rows["SYN-05"]["findings"] == 0
    assert rows["SYN-05"]["status_reasons"] == [SYN_05_WORK]
    recon = api.get("/aircraft/SYN-01/reconcile").json()
    assert recon["findings"] == []
    assert sorted(rows["SYN-01"]) == [
        "attribution", "due_soon", "findings", "flight_time_known_s", "flights", "key", "label",
        "licence", "overdue", "source", "status", "status_reasons", "synthetic",
    ]  # fmt: skip


def test_the_words_report_and_do_not_accuse_or_certify() -> None:
    store = seeded()
    texts = [f.message for f in all_findings(store)] + [f.cue for f in all_findings(store)]
    for a in store.aircraft():
        texts += [n.why for n in judge(store, a.key).not_judged]
    # Words of blame and words of clearance, written in halves so this file holds none whole.
    halves = (
        ("viol", "at"), ("ille", "gal"), ("unlaw", "ful"), ("breach", ""), ("should", " not"),
        ("must", " not"), ("fault", " of"), ("negli", "gen"), ("airw", "orth"), ("compl", "ian"),
        ("cert", "if"), ("appr", "oved"), ("clea", "red"), ("sa", "fe"),
    )  # fmt: skip
    banned = tuple(a + b for a, b in halves)
    for text in texts:
        assert not any(word in text.lower() for word in banned), text
    for f in all_findings(store):
        assert "the records show" in f.message and f.message.startswith("a flight of aircraft ")


# ---- the routes ------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(create_app(seeded()))


def test_the_aircraft_route_lists_findings_and_what_was_not_judged(client: TestClient) -> None:
    body = client.get("/aircraft/SYN-01/grounded-flights", params={"as_of": "2026-10-01T00:00:00Z"})
    assert body.status_code == 200
    data = body.json()
    assert sorted(data) == [
        "aircraft_key", "as_of", "findings", "judged", "logs", "not_judged", "synthetic",
    ]  # fmt: skip
    assert (data["aircraft_key"], data["synthetic"], data["logs"], data["judged"]) == (
        "SYN-01", True, 5, 5,
    )  # fmt: skip
    assert data["as_of"] == "2026-10-01T00:00:00Z"
    first = data["findings"][0]
    assert first == {
        "aircraft_key": "SYN-01",
        "kind": "flight_while_grounded",
        "log_ref": "SYN-01-2026-08-06-03.synthetic",
        "utc_start": "2026-08-06T11:07:54.400000Z",
        "status": "unserviceable",
        "status_reasons": [
            "battery pack BAT-04A on aircraft SYN-01 has reached its 300-cycle life limit"
        ],
        "message": "a flight of aircraft SYN-01 was logged from 2026-08-06 11:07:54 UTC while "
        "the records show the aircraft unserviceable at that time: battery pack BAT-04A on "
        "aircraft SYN-01 has reached its 300-cycle life limit",
        "synthetic": True,
    }
    unknown = client.get("/aircraft/SYN-06/grounded-flights").json()
    assert (unknown["logs"], unknown["judged"], unknown["findings"]) == (3, 0, [])
    assert unknown["not_judged"][2]["utc_start"] == {
        "unknown": "no GPS fix with a valid week in the log"
    }
    assert client.get("/aircraft/NOPE/grounded-flights").status_code == 404


def test_the_fleet_route_lists_all_seven_in_the_order_they_started(client: TestClient) -> None:
    data = client.get("/fleet/grounded-flights", params={"as_of": "2026-10-01T00:00:00Z"}).json()
    assert sorted(data) == ["as_of", "findings", "judged", "logs", "not_judged"]
    assert (data["logs"], data["judged"], data["not_judged"]) == (40, 32, 8)
    got = [
        (f["aircraft_key"], f["utc_start"][:19].replace("T", " "), f["status"])
        for f in data["findings"]
    ]
    assert got == SEVEN
    early = client.get("/fleet/grounded-flights", params={"as_of": "2026-08-06T12:00:00Z"}).json()
    assert [f["log_ref"] for f in early["findings"]] == ["SYN-01-2026-08-06-03.synthetic"]


def test_cues_are_added_on_request_and_only_then(client: TestClient) -> None:
    query = {"as_of": "2026-10-01T00:00:00Z"}
    for path in ("/aircraft/SYN-05/grounded-flights", "/fleet/grounded-flights"):
        plain = client.get(path, params=query)
        again = client.get(path, params={**query, "cues": "false"})
        cued = client.get(path, params={**query, "cues": "true"})
        assert plain.content == again.content
        assert b'"cue"' not in plain.content and b'"cause_cues"' not in plain.content
        for f in cued.json()["findings"]:
            assert len(f["cue"]) <= CUE_LIMIT and f["cue"].startswith("flown ")
            assert len(f["cause_cues"]) == len(f["status_reasons"]) >= 1
            assert all(len(c) <= CUE_LIMIT for c in f["cause_cues"])
        stripped = cued.json()
        stripped["findings"] = [
            {k: v for k, v in f.items() if k not in ("cue", "cause_cues")}
            for f in stripped["findings"]
        ]
        assert stripped == plain.json()


# ---- the evidence pack and the demo page -------------------------------------------------------


def test_the_evidence_pack_lists_the_flights_and_keeps_its_ledger_hash() -> None:
    store = seeded()
    at = datetime(2026, 10, 1, tzinfo=UTC)
    pack = build_pack(store, "SYN-01", CONFIG, as_of=at, commit=None, generated_utc=at)
    section = pack["usage"]["grounded_flights"]
    assert (section["logs"], section["judged"], len(section["findings"])) == (5, 5, 3)
    assert section["findings"][0]["message"].startswith(
        "a flight of aircraft SYN-01 was logged from 2026-08-06 11:07:54 UTC while the records "
    )
    assert "cue" not in section["findings"][0]  # the pack holds sentences, never cues
    html = render_html(pack)
    assert "Flights logged while the records show the aircraft grounded" in html
    assert "was logged from 2026-08-06 12:09:19 UTC while the records show the aircraft" in html
    clean = build_pack(store, "SYN-04", CONFIG, as_of=at, commit=None, generated_utc=at)
    assert clean["ledger_hash"] == (
        "3515dcb1fc7f1282c73fad1c6fbc9ebe39df61537c5629145a4b55d96f42eabf"
    )
    assert clean["usage"]["grounded_flights"]["findings"] == []
    assert (
        "Of the 3 logs judged, not one started while the records show the aircraft "
        "unserviceable, in maintenance or AOG."
    ) in render_html(clean)
    real = build_pack(store, ALFA_KEY, CONFIG, as_of=at, commit=None, generated_utc=at)
    assert real["usage"]["grounded_flights"]["judged"] == 0
    assert "No log of this aircraft could be judged; the reasons follow." in render_html(real)
    assert "public showcase aircraft" in render_html(real)


def test_the_static_export_carries_the_findings_for_the_page(tmp_path: Path) -> None:
    site = export_static(seeded(), tmp_path / "site")
    data = json.loads((site / "fleet.json").read_text(encoding="utf-8"))
    section = data["grounded_flights"]
    assert (section["logs"], section["judged"], section["not_judged"]) == (40, 32, 8)
    assert len(section["findings"]) == 7
    assert all(f["synthetic"] is True for f in section["findings"])
    page = (site / "index.html").read_text(encoding="utf-8")
    assert 'id="grounded"' in page


# ---- mutation: each rule, broken, changes the answer --------------------------------------------


def test_judging_at_the_end_of_the_log_instead_of_its_start_changes_the_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(grounded_module, "judged_at", log_end)
    assert seen(all_findings(seeded())) != SEVEN


def test_counting_a_state_that_does_not_ground_changes_the_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        grounded_module, "GROUNDING", (*GROUNDING, "serviceable with deferred defects")
    )
    found = seen(all_findings(seeded()))
    assert found != SEVEN and any(key == "SYN-03" for key, _, _ in found)


def test_dropping_a_grounding_state_changes_the_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    for kept in (("unserviceable", "AOG"), ("in maintenance", "AOG")):
        monkeypatch.setattr(grounded_module, "GROUNDING", kept)
        assert seen(all_findings(seeded())) != SEVEN


def test_treating_not_known_as_grounded_changes_the_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(grounded_module, "grounds", lambda status: status != "serviceable")
    found = all_findings(seeded())
    assert seen(found) != SEVEN and any(f.aircraft_key == "SYN-06" for f in found)


def test_judging_the_showcase_aircraft_changes_the_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    store = seeded()
    forced = dataclasses.replace(
        entry(
            ALFA_KEY,
            "work_order.open",
            {"state": "in_work", "work_id": "WO-X"},
            at=datetime(2018, 7, 1, tzinfo=UTC),
        ),
        synthetic=False,
    )
    store.append_entry(forced)
    assert seen(all_findings(store)) == SEVEN
    monkeypatch.setattr(app_module, "showcase_keys", lambda: set())
    assert seen(all_findings(store)) != SEVEN


# ---- mutation: the checker refuses each broken property -----------------------------------------


def one_finding() -> tuple[GroundedFlight, StateAt]:
    store = with_a_record([flight(1, T1)])
    open_work(store, "in_work", T1 - HOUR)
    aircraft = store.get_aircraft(KEY)
    assert aircraft is not None
    (found,) = judge(store).findings
    return found, state_fn(store, aircraft)


def test_the_checker_accepts_a_true_finding_and_refuses_each_altered_one() -> None:
    found, state_at = one_finding()
    assert problems(found, state_at) == []

    def bad(**changes: Any) -> list[str]:
        return problems(dataclasses.replace(found, **changes), state_at)

    assert any("state" in p for p in bad(status="unserviceable"))
    assert any("state" in p for p in bad(status="serviceable"))
    assert any("not grounded" in p for p in bad(utc_start=T1 - 2 * HOUR))  # before the order
    assert any("reason" in p for p in bad(reasons=()))
    assert any("reason" in p for p in bad(reasons=("aircraft AC-1 is in maintenance",)))
    assert any("sentence" in p for p in bad(message=found.message.replace("AC-1", "AC-2")))
    assert any("sentence" in p for p in bad(message=found.message.replace("10:00:00", "10:00:01")))
    assert any(
        "sentence" in p for p in bad(message=found.message.replace("in maintenance at", "AOG at"))
    )
    assert any("sentence" in p for p in bad(message=found.message.split(": ")[0]))
    assert any("cue" in p for p in bad(cue="flown AOG 2026-03-10 10:00"))
    assert any("cue" in p for p in bad(cause_cues=()))


def test_the_cue_checker_refuses_each_broken_property() -> None:
    cue = flight_cue("in maintenance", T1)
    assert cue == "flown in maintenance 2026-03-10 10:00"
    assert flight_cue_problems(cue, "in maintenance", T1) == []
    for status in GROUNDING:
        made = flight_cue(status, datetime(2026, 12, 31, 23, 59, 59, tzinfo=UTC))
        assert (
            len(made) <= CUE_LIMIT
            and flight_cue_problems(made, status, datetime(2026, 12, 31, 23, 59, 59, tzinfo=UTC))
            == []
        )

    def bad(text: str, status: str = "in maintenance") -> list[str]:
        return flight_cue_problems(text, status, T1)

    assert any("longer" in p for p in bad(cue + " and some more"))
    assert any("state" in p for p in bad("flown unserviceable 2026-03-10 10:00"))
    assert any("state" in p for p in bad("flown AOG 2026-03-10 10:00"))
    assert any("state" in p for p in bad("flown serviceable 2026-03-10 10:00"))
    assert any("state" in p for p in bad("flown serviceable 2026-03-10 10:00", "unserviceable"))
    assert any("state" in p for p in bad("flown not known 2026-03-10 10:00"))
    assert any("start" in p for p in bad("flown in maintenance 2026-03-11 10:00"))
    assert any("start" in p for p in bad("flown in maintenance 2026-03-10 10:01"))
    assert any("start" in p for p in bad("flown in maintenance"))
    assert any("flown" in p for p in bad("in maintenance 2026-03-10 10:00"))
    assert any("number" in p for p in bad("flown in maintenance 2026-03-10 10:00 7"))
