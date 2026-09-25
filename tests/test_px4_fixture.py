"""Real-log tests on a position-free excerpt of one PX4 Flight Review public log (CC BY 4.0;
a board-support validation flight, see DATA.md). Made with `uasw-census px4-excerpt`."""

import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pyulog import ULog

from uas_workbench.census.excerpt import ULOG_DROP_INFO, ULOG_KEEP, ULOG_ZERO
from uas_workbench.census.px4 import probe_ulog
from uas_workbench.flight import Unknown, is_known
from uas_workbench.flight.px4 import read_ulog

FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "px4"
    / "flight_review_board_validation_2026-06-12_excerpt.ulg"
)
COORDINATE = re.compile(r"-?\b\d{1,3}\.\d{5,}\b")


def test_fixture_is_present_and_small() -> None:
    assert FIXTURE.stat().st_size < 400_000


def test_fixture_names_no_hardware_product() -> None:
    """The hardware name, its subtype and the firmware branch name a commercial board."""
    ulog = ULog(str(FIXTURE), disable_str_exceptions=True)
    assert not set(ULOG_DROP_INFO) & set(ulog.msg_info_dict)


def test_fixture_carries_no_position_and_no_device_id() -> None:
    ulog = ULog(str(FIXTURE), disable_str_exceptions=True)
    assert "sys_uuid" not in ulog.msg_info_dict
    assert ulog.msg_info_multiple_dict == {}
    assert {d.name for d in ulog.data_list} <= set(ULOG_KEEP)
    for dataset in ulog.data_list:
        for name, values in dataset.data.items():
            if ULOG_ZERO.match(name):
                assert not values.any(), f"{dataset.name}.{name} is not zeroed"
    for message in ulog.logged_messages:
        assert COORDINATE.search(message.message) is None
    for key, value in ulog.initial_parameters.items():
        if isinstance(value, float) and value != round(value):
            assert not any(s in key for s in ("LAT", "LON", "HOME")), key


def test_census_probe_on_the_real_log() -> None:
    result = probe_ulog(FIXTURE, "quadrotor")
    assert result.airborne
    assert result.firmware == "v1.18.0 (alpha)"
    c = result.counters
    assert c["flight_time"] and c["landings"] and c["arm_cycles"] and c["utc_date"]
    assert c["lifetime_hours"] and c["fault_events"]
    assert not c["aircraft_key"]  # sys_uuid removed from the excerpt
    assert not c["battery_energy"] and not c["esc_data"]  # this aircraft logged neither


def test_flight_record_on_the_real_log() -> None:
    r = read_ulog(FIXTURE, licence="CC BY 4.0", attribution="PX4 Flight Review public log")

    assert r.source == "px4" and r.firmware == "v1.18.0 (alpha)"
    assert r.log_span_s == pytest.approx(204.3, abs=0.1)
    assert is_known(r.utc_start)
    assert abs((r.utc_start - datetime(2026, 6, 12, 3, 28, 15, tzinfo=UTC)).total_seconds()) < 1
    assert r.flight_time_s == pytest.approx(199.1, abs=0.1)
    assert r.arm_cycles == 1  # the log opens armed: PX4 starts logging at arming
    assert r.landings == 1
    assert r.fault_events == ()  # detector logged, nothing raised, no ERROR messages
    assert is_known(r.lifetime)
    assert r.lifetime.at_boot_s == pytest.approx(1901.3, abs=0.1)
    assert r.lifetime.last_seen_s == r.lifetime.at_boot_s  # PX4 never writes it mid-log
    assert r.lifetime.last_seen_at_s == 0.0
    assert r.aircraft_key == Unknown("sys_uuid missing or all zeros")
    assert r.battery_mah == Unknown("no battery_status topic")
    assert r.boot_count == Unknown("PX4 logs no boot counter")
