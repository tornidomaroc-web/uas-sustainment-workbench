from datetime import UTC, datetime
from pathlib import Path

import pytest

from builders.dataflash import DataFlashBuilder
from uas_workbench.flight import FaultEvent, LifetimeCounter, Unknown, is_known
from uas_workbench.flight.ardupilot import read_dataflash

S = 1_000_000
ATTR = {"licence": "CC0", "attribution": "synthetic"}


def _formats() -> DataFlashBuilder:
    return (
        DataFlashBuilder()
        .define("PARM", "QNf", "TimeUS,Name,Value")
        .define("MSG", "QZ", "TimeUS,Message")
        .define("STAT", "QBBBBBBBB", "TimeUS,isFlying,isFlyProb,Armed,Safety,Crash,Still,Stage,Hit")
        .define("GPS", "QBIHf", "TimeUS,Status,GMS,GWk,Spd")
        .define("ARM", "QBH", "TimeUS,ArmState,ArmChecks")
        .define("BAT", "QBfff", "TimeUS,Inst,Volt,CurrTot,EnrgTot")
        .define("ERR", "QBB", "TimeUS,Subsys,ECode")
    )


def modern_plane(path: Path) -> Path:
    """An ArduPlane 4.x style log: ARM messages, BAT with energy, one ERR, two flights."""
    b = _formats()
    b.msg("MSG", 10 * S, "ArduPlane V4.5.7 (1234abcd)")
    b.msg("MSG", 10 * S, "FCBoard DEADBEEF 00000000 00000001")
    for name, value in [("STAT_FLTTIME", 3600.0), ("STAT_BOOTCNT", 12.0), ("BATT_MONITOR", 4.0)]:
        b.msg("PARM", 10 * S, name, value)
    # GPS week 2300, 100 000 s into the week: 2024-02-05 03:46:40 GPS, 03:46:22 UTC
    b.msg("GPS", 12 * S, 3, 100_000_000, 2300, 0.0)
    b.msg("ARM", 20 * S, 1, 0)
    flying = {t: 1 for t in range(30, 130)} | {t: 1 for t in range(200, 260)}
    for t in range(20, 300, 10):
        b.msg("STAT", t * S, flying.get(t, 0), 0, 1, 0, 0, 0, 0, 0)
        b.msg("BAT", t * S, 0, 22.2, 12.0 * t, 0.25 * t)
    b.msg("PARM", 40 * S, "STAT_FLTTIME", 3610.0)
    b.msg("PARM", 250 * S, "STAT_FLTTIME", 3750.0)
    b.msg("ERR", 210 * S, 2, 1)
    b.msg("ARM", 270 * S, 0, 0)
    b.msg("ARM", 280 * S, 1, 0)
    return b.write(path)


def test_modern_plane_record(tmp_path: Path) -> None:
    r = read_dataflash(modern_plane(tmp_path / "plane.bin"), **ATTR)

    assert r.source == "ardupilot" and r.firmware == "ArduPlane 4.5.7"
    assert r.aircraft_key == "DEADBEEF 00000000 00000001"
    assert r.log_span_s == 280.0
    assert is_known(r.utc_start)
    assert r.utc_start == datetime(2024, 2, 5, 3, 46, 20, tzinfo=UTC)  # fix at 12 s, log at 10 s
    assert r.flight_time_s == pytest.approx(100 + 60)  # sample-to-sample, 10 s steps
    assert r.arm_cycles == 2
    assert r.landings == 2
    assert r.battery_mah == pytest.approx(12.0 * (290 - 20))
    assert r.battery_wh == pytest.approx(0.25 * (290 - 20))
    assert r.fault_events == (FaultEvent(200.0, "err:subsys 2", "code 1"),)
    assert r.boot_count == 12
    assert r.lifetime == LifetimeCounter(3600.0, 3750.0, 240.0)


def test_every_unknown_names_its_reason(tmp_path: Path) -> None:
    b = _formats()
    b.msg("MSG", S, "ArduCopter V4.4.0 (abcdef01)")
    b.msg("GPS", 2 * S, 1, 0, 0, 0.0)  # no fix
    r = read_dataflash(b.write(tmp_path / "bare.bin"), **ATTR)

    assert r.aircraft_key == Unknown("no board id in boot messages; BRD_SERIAL_NUM not set")
    assert r.utc_start == Unknown("no GPS fix with a valid week in the log")
    no_stat = Unknown("no STAT messages (ArduPlane logs them; other vehicle types do not)")
    assert r.flight_time_s == no_stat and r.landings == no_stat
    assert r.arm_cycles == Unknown("no ARM or EV messages")
    assert r.battery_mah == Unknown("no BAT or CURR messages")
    assert r.fault_events == Unknown("no STAT, EV or ERR messages")
    assert r.boot_count == Unknown("STAT_BOOTCNT not in parameters")
    assert r.lifetime == Unknown("STAT_FLTTIME not in parameters")


def test_crash_flag_becomes_a_fault_event(tmp_path: Path) -> None:
    b = _formats()
    b.msg("MSG", S, "ArduPlane V4.5.7 (1234abcd)")
    b.msg("STAT", 2 * S, 1, 0, 1, 0, 0, 0, 0, 0)
    b.msg("STAT", 3 * S, 0, 0, 1, 0, 1, 0, 0, 1)
    r = read_dataflash(b.write(tmp_path / "crash.bin"), **ATTR)
    assert r.fault_events == (
        FaultEvent(2.0, "crash", "STAT.Crash set"),
        FaultEvent(2.0, "hit", "STAT.Hit set"),
    )
