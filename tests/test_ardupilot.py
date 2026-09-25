from pathlib import Path

from builders.dataflash import DataFlashBuilder
from uas_workbench.census import ardupilot
from uas_workbench.census.model import FieldResult

S = 1_000_000


def _formats() -> DataFlashBuilder:
    return (
        DataFlashBuilder()
        .define("PARM", "QNf", "TimeUS,Name,Value")
        .define("MSG", "QZ", "TimeUS,Message")
        .define("EV", "QB", "TimeUS,Id")
        .define("STAT", "QBBBB", "TimeUS,isFlying,Armed,Crash,Hit")
        .define("CURR", "Qfff", "TimeUS,Volt,CurrTot,EnrgTot")
        .define("GPS", "QBIHf", "TimeUS,Status,GMS,GWk,Spd")
        .define("RCOU", "QHHH", "TimeUS,C1,C2,C3")
    )


def plane_2018_style(path: Path) -> Path:
    """Shaped like an ArduPlane 3.9 log: CURR (not BAT), no ARM message, no ESC telemetry."""
    b = _formats()
    b.msg("MSG", S, "ArduPlane V3.9.2 (b0e2a6fe)")
    b.msg("MSG", S, "PX4v2 DEADBEEF 00000000 00000001")  # synthetic board id
    for name, value in [
        ("STAT_FLTTIME", 36_000.0),
        ("STAT_BOOTCNT", 42.0),
        ("STAT_RUNTIME", 90_000.0),
        ("BRD_SERIAL_NUM", 0.0),
    ]:
        b.msg("PARM", S, name, value)
    b.msg("EV", 2 * S, 10)
    for t, flying in [(2, 0), (5, 1), (90, 1), (100, 0)]:
        b.msg("STAT", t * S, flying, 1, 0, 0)
        b.msg("GPS", t * S, 3, 1000 * t, 2010, 18.0 if flying else 0.0)
        b.msg("CURR", t * S, 12.1, 30.0 * t, 0.4 * t)
        b.msg("RCOU", t * S, 1500, 1500, 1100 + 5 * t)
    b.msg("EV", 101 * S, 11)
    return b.write(path)


def test_2018_plane_log(tmp_path: Path) -> None:
    result = ardupilot.probe_dataflash(plane_2018_style(tmp_path / "plane.bin"), "alfa")

    assert result.firmware == "ArduPlane 3.9.2"
    assert result.airborne
    assert result.duration_s == 100.0
    f = result.fields
    # The battery alias falls back to CURR when BAT is absent.
    assert f["BAT.CurrTot (CURR before 3.10)"] == FieldResult(True, True)
    assert f["BAT.SH"] == FieldResult(False, False)
    assert f["ARM.ArmState"] == FieldResult(False, False)
    assert f["EV.Id"] == FieldResult(True, True)
    assert f["STAT.isFlying"] == FieldResult(True, True)
    assert f["ESC.RPM"] == FieldResult(False, False)
    assert f["RCOU.C1..C14"] == FieldResult(True, True)
    assert f["PARM STAT_FLTCNT"] == FieldResult(False, False)  # added to ArduPilot in 2025
    assert f["PARM BRD_SERIAL_NUM"] == FieldResult(True, False)
    assert f["MSG boot banner with MCU id"] == FieldResult(True, True)
    c = result.counters
    assert c["aircraft_key"] and c["utc_date"] and c["flight_time"] and c["landings"]
    assert c["arm_cycles"] and c["battery_energy"] and c["fault_events"] and c["lifetime_hours"]
    assert not c["esc_data"]


def test_log_without_gps_time_or_ids(tmp_path: Path) -> None:
    b = _formats()
    b.msg("MSG", S, "ArduPlane V4.5.7 (1234abcd)")
    b.msg("GPS", S, 1, 0, 0, 0.0)
    b.msg("GPS", 30 * S, 1, 0, 0, 12.0)

    result = ardupilot.probe_dataflash(b.write(tmp_path / "nofix.bin"), "x")

    assert result.airborne  # no STAT, so ground speed decides
    assert not result.counters["utc_date"]
    assert not result.counters["aircraft_key"]
    assert not result.counters["flight_time"]  # airborne, but no signal to time it with
    assert result.fields["BAT.CurrTot (CURR before 3.10)"] == FieldResult(False, False)


def test_every_field_spec_is_reported(tmp_path: Path) -> None:
    result = ardupilot.probe_dataflash(plane_2018_style(tmp_path / "plane.bin"), "alfa")
    assert list(result.fields) == [spec.key for spec in ardupilot.FIELD_SPECS]
