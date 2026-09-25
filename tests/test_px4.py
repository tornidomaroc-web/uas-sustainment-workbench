from pathlib import Path

import pytest

from builders.ulog import ULogBuilder
from uas_workbench.census import px4
from uas_workbench.census.model import COUNTERS, FieldResult

S = 1_000_000  # one second in microseconds


def _formats(b: ULogBuilder) -> ULogBuilder:
    return (
        b.format(
            "vehicle_status",
            [
                "uint64_t timestamp",
                "uint64_t armed_time",
                "uint64_t takeoff_time",
                "uint8_t arming_state",
            ],
        )
        .format("vehicle_land_detected", ["uint64_t timestamp", "bool landed"])
        .format(
            "battery_status",
            [
                "uint64_t timestamp",
                "float voltage_v",
                "float current_a",
                "float discharged_mah",
                "uint16_t cycle_count",
                "uint16_t state_of_health",
            ],
        )
        .format(
            "esc_report",
            [
                "uint64_t timestamp",
                "int32_t esc_rpm",
                "float esc_temperature",
                "uint32_t esc_errorcount",
            ],
        )
        .format("esc_status", ["uint64_t timestamp", "uint8_t esc_count", "esc_report[2] esc"])
        .format(
            "failure_detector_status",
            ["uint64_t timestamp", "bool fd_motor", "bool fd_imbalanced_prop", "bool fd_battery"],
        )
        .format("vehicle_gps_position", ["uint64_t timestamp", "uint64_t time_utc_usec"])
    )


def flown_log(path: Path) -> Path:
    b = _formats(ULogBuilder(start_us=S))
    b.info("sys_uuid", "synthetic-vehicle-0001").param("LND_FLIGHT_T_LO", 1_500_000)
    status = b.subscribe("vehicle_status")
    land = b.subscribe("vehicle_land_detected")
    battery = b.subscribe("battery_status")
    esc = b.subscribe("esc_status")
    fd = b.subscribe("failure_detector_status")
    gps = b.subscribe("vehicle_gps_position")
    b.data(status, {"timestamp": 2 * S, "arming_state": 1})
    b.data(status, {"timestamp": 3 * S, "arming_state": 2, "armed_time": 3 * S})
    b.data(
        status, {"timestamp": 5 * S, "arming_state": 2, "armed_time": 3 * S, "takeoff_time": 5 * S}
    )
    b.data(land, {"timestamp": 2 * S, "landed": True})
    b.data(land, {"timestamp": 5 * S, "landed": False})
    b.data(land, {"timestamp": 60 * S, "landed": True})
    b.data(
        battery, {"timestamp": 3 * S, "voltage_v": 16.4, "current_a": 12.0, "discharged_mah": 350.0}
    )
    rpm = {"timestamp": 5 * S, "esc_rpm": 9000, "esc_temperature": 41.0}
    b.data(esc, {"timestamp": 5 * S, "esc_count": 2, "esc": [rpm, rpm]})
    b.data(fd, {"timestamp": 5 * S})
    b.data(gps, {"timestamp": 5 * S, "time_utc_usec": 1_758_780_000 * S})
    b.log(3, "Motor failure detected", 40 * S)
    return b.write(path)


def test_flown_log_supports_every_counter(tmp_path: Path) -> None:
    result = px4.probe_ulog(flown_log(tmp_path / "flown.ulg"), "fixed_wing")

    assert result.airborne
    assert result.counters == dict.fromkeys(COUNTERS, True)
    assert result.duration_s == pytest.approx(59.0)
    f = result.fields
    assert f["vehicle_land_detected.landed"] == FieldResult(True, True)
    assert f["esc_status.esc[].esc_rpm"] == FieldResult(True, True)
    assert f["esc_status.esc[].esc_errorcount"] == FieldResult(True, False)
    # Smart-battery fields exist in the format but were never filled: present, not real.
    assert f["battery_status.cycle_count"] == FieldResult(True, False)
    # A field the format does not define at all is absent, not zero.
    assert f["battery_status.serial_number"] == FieldResult(False, False)
    assert f["failure_detector_status.fd_motor"] == FieldResult(True, False)
    assert f["param LND_FLIGHT_T_HI"] == FieldResult(False, False)
    assert f["logged messages, level ERROR or worse"] == FieldResult(True, True)


def test_bench_log_is_honest_about_what_it_cannot_show(tmp_path: Path) -> None:
    b = _formats(ULogBuilder(start_us=S)).info("sys_uuid", "00000000")
    status = b.subscribe("vehicle_status")
    land = b.subscribe("vehicle_land_detected")
    battery = b.subscribe("battery_status")
    b.data(status, {"timestamp": 2 * S, "arming_state": 1})
    b.data(land, {"timestamp": 2 * S, "landed": True})
    b.data(battery, {"timestamp": 2 * S, "voltage_v": 16.8, "discharged_mah": -1.0})
    b.log(6, "Takeoff detected", 3 * S)  # INFO, not an error

    result = px4.probe_ulog(b.write(tmp_path / "bench.ulg"), "vtol")

    assert not result.airborne
    c = result.counters
    assert not c["aircraft_key"]  # an all-zero id identifies nothing
    assert not c["arm_cycles"] and not c["landings"] and not c["flight_time"]
    assert not c["battery_energy"]  # discharged_mah == -1 is PX4's "invalid"
    assert not c["esc_data"] and not c["fault_events"] and not c["utc_date"]
    assert result.fields["esc_status.esc[].esc_rpm"] == FieldResult(False, False)
    assert result.fields["logged messages, level ERROR or worse"] == FieldResult(True, False)


@pytest.mark.parametrize(("raw", "level"), [(ord("3"), 3), (ord("6"), 6), (3, 3), (0, 0)])
def test_syslog_level_accepts_ascii_and_raw(raw: int, level: int) -> None:
    assert px4.syslog_level(raw) == level


def test_every_field_spec_is_reported(tmp_path: Path) -> None:
    result = px4.probe_ulog(flown_log(tmp_path / "flown.ulg"), "fixed_wing")
    assert list(result.fields) == [spec.key for spec in px4.FIELD_SPECS]
