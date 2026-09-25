"""Acceptance test for reconcile(), on the three consecutive ALFA flights in tests/fixtures.

Measured on 2026-09-25 from the excerpts (CC BY 4.0, Keipour, Mousaei and Scherer, CMU):

    log                  STAT_FLTTIME at boot   last written in log   flying time in log
    2018-07-30_16-30-14  16748 s                17052 s               312.4 s
    2018-07-30_16-46-36  17052 s                17312 s               260.0 s
    2018-07-30_17-28-50  17667 s                19074 s               1395.4 s

Log 2 ends with the counter at 17312 s, and log 3 boots with it at 17667 s: the aircraft
flew 355 s more after log 2 stopped, and no log covers it.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from uas_workbench.flight import FlightRecord, Unknown, is_known, reconcile
from uas_workbench.flight.ardupilot import read_dataflash

FIXTURES = Path(__file__).parent / "fixtures" / "alfa"
LICENCE = "CC BY 4.0"
ATTRIBUTION = "ALFA dataset, Keipour, Mousaei and Scherer, Carnegie Mellon University"
AIRCRAFT = "alfa-fixed-wing"  # the excerpts mask the board id, so the caller names the aircraft


@pytest.fixture(scope="module")
def records() -> list[FlightRecord]:
    return [
        read_dataflash(p, licence=LICENCE, attribution=ATTRIBUTION)
        for p in sorted(FIXTURES.glob("*.bin"))
    ]


def test_records_carry_what_the_logs_support(records: list[FlightRecord]) -> None:
    a, b, c = records
    assert [r.log_ref for r in records] == [
        "2018-07-30_16-30-14",
        "2018-07-30_16-46-36",
        "2018-07-30_17-28-50",
    ]
    assert a.source == "ardupilot" and a.licence == LICENCE and a.attribution == ATTRIBUTION
    assert a.firmware == "ArduPlane 3.9.0"
    assert a.log_span_s == pytest.approx(490.7, abs=0.1)
    assert is_known(a.utc_start)
    assert abs((a.utc_start - datetime(2018, 7, 30, 20, 22, 7, tzinfo=UTC)).total_seconds()) < 1
    assert [r.flight_time_s for r in records] == [
        pytest.approx(312.4, abs=0.1),
        pytest.approx(260.0, abs=0.1),
        pytest.approx(1395.4, abs=0.1),
    ]
    assert [r.landings for r in records] == [1, 1, 1]
    assert [r.boot_count for r in records] == [326, 327, 328]
    assert is_known(a.lifetime) and is_known(b.lifetime) and is_known(c.lifetime)
    assert (a.lifetime.at_boot_s, a.lifetime.last_seen_s) == (16748, 17052)
    assert (b.lifetime.at_boot_s, b.lifetime.last_seen_s) == (17052, 17312)
    assert (c.lifetime.at_boot_s, c.lifetime.last_seen_s) == (17667, 19074)
    assert is_known(a.fault_events) and a.fault_events == ()


def test_unknowns_say_why(records: list[FlightRecord]) -> None:
    a = records[0]
    assert a.aircraft_key == Unknown("board id masked in boot messages; BRD_SERIAL_NUM is 0")
    assert a.arm_cycles == Unknown(
        "ARMING_REQUIRE=0: the aircraft is always armed and logs no arm events"
    )
    assert a.battery_mah == Unknown("battery monitor disabled (BATT_MONITOR=0)")
    assert a.battery_wh == Unknown("battery monitor disabled (BATT_MONITOR=0)")


def test_reconcile_finds_the_unlogged_flight(records: list[FlightRecord]) -> None:
    result = reconcile(records, aircraft_key=AIRCRAFT)

    assert [(c.log_ref, c.next_log_ref, round(c.unlogged_s, 1), c.boots_between)
            for c in result.coverage] == [
        ("2018-07-30_16-30-14", "2018-07-30_16-46-36", -8.4, 0),
        ("2018-07-30_16-46-36", "2018-07-30_17-28-50", 355.0, 0),
    ]  # fmt: skip
    assert len(result.findings) == 1
    finding = result.findings[0]
    assert (finding.kind, finding.log_ref, round(finding.seconds)) == (
        "unlogged_flight",
        "2018-07-30_16-46-36",
        355,
    )
    assert finding.message == (
        "355 s of flight on aircraft alfa-fixed-wing is not covered by any log "
        "(after 2018-07-30_16-46-36, before 2018-07-30_17-28-50)"
    )
    assert result.unchecked == {
        "2018-07-30_17-28-50": "last known log of this aircraft; the counter was not seen again"
    }


def test_the_counter_agrees_with_each_log_within_one_flush(records: list[FlightRecord]) -> None:
    """Counter written during the log vs flight time derived from the log: -8, 0, +12 s."""
    for r in records:
        assert is_known(r.lifetime) and is_known(r.flight_time_s)
        in_log = r.lifetime.last_seen_s - r.lifetime.at_boot_s
        assert abs(in_log - r.flight_time_s) <= 30
