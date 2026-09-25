from datetime import UTC, datetime
from pathlib import Path

import pytest

from builders.ulog import ULogBuilder
from uas_workbench.flight import FaultEvent, LifetimeCounter, Unknown, is_known
from uas_workbench.flight.px4 import read_ulog

S = 1_000_000
ATTR = {"licence": "CC0", "attribution": "synthetic"}


def _formats(b: ULogBuilder) -> ULogBuilder:
    return (
        b.format("vehicle_status", ["uint64_t timestamp", "uint8_t arming_state"])
        .format("vehicle_land_detected", ["uint64_t timestamp", "bool landed"])
        .format(
            "battery_status",
            ["uint64_t timestamp", "float voltage_v", "float current_a", "float discharged_mah"],
        )
        .format("failure_detector_status", ["uint64_t timestamp", "bool fd_motor"])
        .format("vehicle_gps_position", ["uint64_t timestamp", "uint64_t time_utc_usec"])
    )


def flown_log(path: Path) -> Path:
    b = _formats(ULogBuilder(start_us=10 * S)).info("sys_uuid", "synthetic-vehicle-0001")
    b.param("LND_FLIGHT_T_HI", 1).param("LND_FLIGHT_T_LO", 705_032_704)  # 5 000 000 000 us
    status = b.subscribe("vehicle_status")
    land = b.subscribe("vehicle_land_detected")
    battery = b.subscribe("battery_status")
    fd = b.subscribe("failure_detector_status")
    gps = b.subscribe("vehicle_gps_position")
    b.data(gps, {"timestamp": 12 * S, "time_utc_usec": 1_707_018_364 * S})  # 2024-02-04T03:46:04Z
    for t, armed in [(20, 1), (25, 2), (200, 1), (210, 2), (290, 1)]:
        b.data(status, {"timestamp": t * S, "arming_state": armed})
    for t, landed in [(20, 1), (30, 0), (130, 1), (210, 0), (260, 1)]:
        b.data(land, {"timestamp": t * S, "landed": landed})
    for t in range(20, 300, 10):
        b.data(battery, {"timestamp": t * S, "voltage_v": 16.0, "current_a": 9.0,
                         "discharged_mah": 2.5 * t})  # fmt: skip
    b.data(fd, {"timestamp": 150 * S, "fd_motor": False})
    b.data(fd, {"timestamp": 160 * S, "fd_motor": True})
    b.log(3, "Battery low", 250 * S)
    b.log(6, "Landing detected", 261 * S)
    return b.write(path)


def test_flown_log_record(tmp_path: Path) -> None:
    r = read_ulog(flown_log(tmp_path / "flown.ulg"), **ATTR)

    assert r.source == "px4" and r.aircraft_key == "synthetic-vehicle-0001"
    assert r.log_span_s == pytest.approx(280.0)
    assert is_known(r.utc_start)
    assert r.utc_start == datetime(2024, 2, 4, 3, 46, 2, tzinfo=UTC)  # fix at 12 s, log at 10 s
    assert r.flight_time_s == pytest.approx(100 + 50)
    assert r.arm_cycles == 2
    assert r.landings == 2
    assert r.battery_mah == pytest.approx(2.5 * (290 - 20))
    assert r.battery_wh == pytest.approx(16.0 * 9.0 * (290 - 20) / 3600)
    assert r.fault_events == (
        FaultEvent(150.0, "failure_detector:fd_motor", "flag raised"),
        FaultEvent(240.0, "error_message", "Battery low"),
    )
    assert r.boot_count == Unknown("PX4 logs no boot counter")
    assert r.lifetime == LifetimeCounter(5000.0, 5000.0, 0.0)


def test_every_unknown_names_its_reason(tmp_path: Path) -> None:
    b = _formats(ULogBuilder(start_us=S)).info("sys_uuid", "0000000000000000")
    r = read_ulog(b.write(tmp_path / "bare.ulg"), **ATTR)

    assert r.aircraft_key == Unknown("sys_uuid missing or all zeros")
    assert r.utc_start == Unknown("no GPS UTC time in vehicle_gps_position or sensor_gps")
    assert r.flight_time_s == Unknown("no vehicle_land_detected topic")
    assert r.landings == Unknown("no vehicle_land_detected topic")
    assert r.arm_cycles == Unknown("no vehicle_status topic")
    assert r.battery_mah == Unknown("no battery_status topic")
    assert r.fault_events == Unknown("no failure_detector_status topic and no logged messages")
    assert r.lifetime == Unknown("LND_FLIGHT_T_HI/LO not in parameters")


def test_invalid_discharge_falls_back_to_integrated_current(tmp_path: Path) -> None:
    b = _formats(ULogBuilder(start_us=0))
    battery = b.subscribe("battery_status")
    for t in (0, 3600):
        b.data(battery, {"timestamp": t * S, "voltage_v": 0.0, "current_a": 2.0,
                         "discharged_mah": -1.0})  # fmt: skip
    r = read_ulog(b.write(tmp_path / "amps.ulg"), **ATTR)
    assert r.battery_mah == pytest.approx(2000.0)  # 2 A for one hour
    assert r.battery_wh == pytest.approx(0.0)  # no voltage samples: nothing to integrate


def test_a_log_that_opens_armed_is_one_arm_cycle(tmp_path: Path) -> None:
    """PX4 starts logging at arming by default, so the arm transition is never in the log."""
    b = _formats(ULogBuilder(start_us=0))
    status = b.subscribe("vehicle_status")
    for t, armed in [(0, 2), (100, 2), (200, 1)]:
        b.data(status, {"timestamp": t * S, "arming_state": armed})
    r = read_ulog(b.write(tmp_path / "armed.ulg"), **ATTR)
    assert r.arm_cycles == 1
