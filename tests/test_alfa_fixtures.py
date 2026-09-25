"""Real-log tests on position-free excerpts of three consecutive ALFA flights (CC BY 4.0,
Keipour, Mousaei and Scherer, CMU; see DATA.md). Made with `uasw-census alfa-excerpt`."""

import re
from pathlib import Path

import pytest
from pymavlink import DFReader

from uas_workbench.census import ardupilot
from uas_workbench.census.excerpt import KEEP, ZERO

FIXTURES = Path(__file__).parent / "fixtures" / "alfa"
LOGS = sorted(FIXTURES.glob("*.bin"))


def messages(path: Path) -> list[object]:
    reader = DFReader.DFReader_binary(str(path), zero_time_base=True)
    try:
        return list(iter(reader.recv_match, None))
    finally:
        reader.close()


def flying_seconds(path: Path) -> float:
    """Time with STAT.isFlying set, summed between consecutive STAT samples."""
    total, last = 0.0, None
    for m in messages(path):
        if m.get_type() == "STAT":  # type: ignore[attr-defined]
            now = m.TimeUS / 1e6  # type: ignore[attr-defined]
            if last is not None and last[1]:
                total += now - last[0]
            last = (now, m.isFlying)  # type: ignore[attr-defined]
    return total


def param(path: Path, name: str) -> float:
    for m in messages(path):
        if m.get_type() == "PARM" and m.Name == name:  # type: ignore[attr-defined]
            return float(m.Value)  # type: ignore[attr-defined]
    raise KeyError(name)


def test_three_fixtures_are_present_and_small() -> None:
    assert [p.stem for p in LOGS] == [
        "2018-07-30_16-30-14",
        "2018-07-30_16-46-36",
        "2018-07-30_17-28-50",
    ]
    assert sum(p.stat().st_size for p in LOGS) < 1_000_000


@pytest.mark.parametrize("path", LOGS, ids=lambda p: p.stem)
def test_fixture_carries_no_position_and_no_device_id(path: Path) -> None:
    msgs = messages(path)
    assert {m.get_type() for m in msgs} <= KEEP | {"FMT"}  # type: ignore[attr-defined]
    for m in msgs:
        if m.get_type() == "GPS":  # type: ignore[attr-defined]
            assert all(getattr(m, f) == 0 for f in ZERO["GPS"])
    assert re.search(rb"[0-9A-F]{8} [0-9A-F]{8} [0-9A-F]{8}", path.read_bytes()) is None


@pytest.mark.parametrize("path", LOGS, ids=lambda p: p.stem)
def test_census_on_a_real_log(path: Path) -> None:
    result = ardupilot.probe_dataflash(path, "alfa")
    assert result.firmware == "ArduPlane 3.9.0"  # V3.9.0-beta1
    assert result.airborne
    c = result.counters
    assert c["flight_time"] and c["landings"] and c["utc_date"] and c["lifetime_hours"]
    # This aircraft ran with BATT_MONITOR=0 and ARMING_REQUIRE=0: no battery, no arm events.
    assert not c["battery_energy"] and not c["arm_cycles"] and not c["esc_data"]
    assert not c["aircraft_key"]  # the board id is masked in the excerpt


def test_log_flight_time_against_the_autopilot_lifetime_counter() -> None:
    """The evidence the FlightRecord step builds on. STAT_FLTTIME in the next log, minus
    STAT_FLTTIME in this one, is what the autopilot says was flown in between."""
    first, second, third = LOGS
    assert [param(p, "STAT_BOOTCNT") for p in LOGS] == [326, 327, 328]  # consecutive boots
    counter_1 = param(second, "STAT_FLTTIME") - param(first, "STAT_FLTTIME")
    counter_2 = param(third, "STAT_FLTTIME") - param(second, "STAT_FLTTIME")
    # Agreement: the log shows 312 s flying, the counter 304 s.
    assert flying_seconds(first) == pytest.approx(312, abs=1)
    assert counter_1 == 304
    # A gap: the log shows 260 s, the counter 615 s. The log ends before the flight did.
    assert flying_seconds(second) == pytest.approx(260, abs=1)
    assert counter_2 == 615
