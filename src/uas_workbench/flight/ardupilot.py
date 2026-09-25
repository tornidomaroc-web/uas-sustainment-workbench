"""FlightRecord from an ArduPilot DataFlash log (.bin).

Every derivation names the message it rests on. Where the log does not carry the message,
or the aircraft was configured not to produce it, the field is Unknown with that reason.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path

from pymavlink import DFReader

from uas_workbench.census.ardupilot import BOARD_UID, EV_ARMED, firmware_from_messages
from uas_workbench.census.excerpt import BOARD_ID_MASK

from .record import FaultEvent, FlightRecord, LifetimeCounter, Maybe, Unknown

READ_TYPES = ("PARM", "MSG", "STAT", "GPS", "EV", "ARM", "BAT", "CURR", "ERR")
GPS_EPOCH = datetime(1980, 1, 6, tzinfo=UTC)
GPS_MINUS_UTC = timedelta(seconds=18)  # leap seconds since 2017-01-01; ArduPilot uses the same
GPS_FIX_3D = 3

Sample = tuple[int, float]  # (TimeUS, value)


@dataclass
class _Log:
    params: dict[str, float] = field(default_factory=dict)
    flttime: list[Sample] = field(default_factory=list)
    texts: list[str] = field(default_factory=list)
    flying: list[Sample] = field(default_factory=list)
    crash: list[Sample] = field(default_factory=list)
    hit: list[Sample] = field(default_factory=list)
    armed: list[Sample] = field(default_factory=list)
    events: list[Sample] = field(default_factory=list)
    curr_tot: list[Sample] = field(default_factory=list)
    enrg_tot: list[Sample] = field(default_factory=list)
    errors: list[tuple[int, int, int]] = field(default_factory=list)
    gps_fix: tuple[int, int, int] | None = None  # (TimeUS, GWk, GMS) of the first 3D fix
    first_us: int | None = None
    last_us: int | None = None


def _read(path: Path) -> _Log:
    reader = DFReader.DFReader_binary(str(path), zero_time_base=True)
    log = _Log()
    try:
        while (m := reader.recv_match(type=list(READ_TYPES))) is not None:
            _collect(m, log)
    finally:
        reader.close()
    return log


def _collect(m: DFReader.DFMessage, log: _Log) -> None:
    t = int(getattr(m, "TimeUS", 0) or 0)
    if t > 0:
        log.first_us = t if log.first_us is None else min(log.first_us, t)
        log.last_us = t if log.last_us is None else max(log.last_us, t)
    match m.get_type():
        case "PARM":
            log.params.setdefault(str(m.Name), float(m.Value))
            if m.Name == "STAT_FLTTIME":
                log.flttime.append((t, float(m.Value)))
        case "MSG":
            log.texts.append(str(m.Message))
        case "STAT":
            log.flying.append((t, float(m.isFlying)))
            log.crash.append((t, float(m.Crash)))
            log.hit.append((t, float(m.Hit)))
        case "GPS":
            if log.gps_fix is None and int(m.Status) >= GPS_FIX_3D and int(m.GWk) > 0:
                log.gps_fix = (t, int(m.GWk), int(m.GMS))
        case "EV":
            log.events.append((t, float(m.Id)))
        case "ARM":
            log.armed.append((t, float(m.ArmState)))
        case "BAT" | "CURR":
            if int(getattr(m, "Inst", getattr(m, "Instance", 0))) == 0:
                log.curr_tot.append((t, float(m.CurrTot)))
                if hasattr(m, "EnrgTot"):
                    log.enrg_tot.append((t, float(m.EnrgTot)))
        case "ERR":
            log.errors.append((t, int(m.Subsys), int(m.ECode)))


def _rising(samples: list[Sample]) -> list[int]:
    """Times at which a state signal goes from zero to non-zero."""
    return [t for (_, prev), (t, cur) in pairwise(samples) if not prev and cur]


def _falling(samples: list[Sample]) -> list[int]:
    return [t for (_, prev), (t, cur) in pairwise(samples) if prev and not cur]


def _time_while_set(samples: list[Sample]) -> float:
    """Seconds during which a state signal was non-zero, sample to sample."""
    return sum((t - t_prev) / 1e6 for (t_prev, prev), (t, _) in pairwise(samples) if prev)


def _aircraft_key(log: _Log) -> Maybe[str]:
    for text in log.texts:
        if match := BOARD_UID.search(text):
            return match.group(0)
    serial = log.params.get("BRD_SERIAL_NUM")
    if serial:
        return f"BRD_SERIAL_NUM {int(serial)}"
    masked = any(BOARD_ID_MASK.decode() in t for t in log.texts)
    where = "board id masked in boot messages" if masked else "no board id in boot messages"
    how = "is 0" if serial == 0 else "not set"
    return Unknown(f"{where}; BRD_SERIAL_NUM {how}")


def _utc_start(log: _Log) -> Maybe[datetime]:
    if log.gps_fix is None or log.first_us is None:
        return Unknown("no GPS fix with a valid week in the log")
    t_fix, week, ms = log.gps_fix
    utc_fix = GPS_EPOCH + timedelta(weeks=week, milliseconds=ms) - GPS_MINUS_UTC
    return utc_fix - timedelta(microseconds=t_fix - log.first_us)


def _arm_cycles(log: _Log) -> Maybe[int]:
    if log.armed:  # ARM is written on every arm and disarm, so each armed message is one cycle
        return sum(1 for _, state in log.armed if state)
    if log.events:
        return sum(1 for _, event in log.events if event == EV_ARMED)
    if log.params.get("ARMING_REQUIRE") == 0:
        return Unknown("ARMING_REQUIRE=0: the aircraft is always armed and logs no arm events")
    return Unknown("no ARM or EV messages")


def _battery(log: _Log) -> tuple[Maybe[float], Maybe[float]]:
    if not log.curr_tot:
        if log.params.get("BATT_MONITOR") == 0:
            reason = "battery monitor disabled (BATT_MONITOR=0)"
        else:
            reason = "no BAT or CURR messages"
        return Unknown(reason), Unknown(reason)
    mah = log.curr_tot[-1][1] - log.curr_tot[0][1]
    if not log.enrg_tot:
        return mah, Unknown("no EnrgTot field in BAT/CURR messages")
    return mah, log.enrg_tot[-1][1] - log.enrg_tot[0][1]


def _faults(log: _Log, first_us: int) -> Maybe[tuple[FaultEvent, ...]]:
    if not (log.flying or log.events or log.errors):
        return Unknown("no STAT, EV or ERR messages")
    events = [
        FaultEvent((t - first_us) / 1e6, f"err:subsys {subsys}", f"code {code}")
        for t, subsys, code in log.errors
    ]
    events += [
        FaultEvent((t - first_us) / 1e6, "crash", "STAT.Crash set") for t in _rising(log.crash)
    ]
    events += [FaultEvent((t - first_us) / 1e6, "hit", "STAT.Hit set") for t in _rising(log.hit)]
    return tuple(sorted(events, key=lambda e: e.t_s))


def _lifetime(log: _Log, first_us: int) -> Maybe[LifetimeCounter]:
    if not log.flttime:
        return Unknown("STAT_FLTTIME not in parameters")
    (_, at_boot), (t_last, last_seen) = log.flttime[0], log.flttime[-1]
    return LifetimeCounter(at_boot, last_seen, (t_last - first_us) / 1e6)


def read_dataflash(path: Path, *, licence: str, attribution: str) -> FlightRecord:
    log = _read(path)
    first_us = log.first_us or 0
    last_us = log.last_us or 0
    no_stat = Unknown("no STAT messages (ArduPlane logs them; other vehicle types do not)")
    boot = log.params.get("STAT_BOOTCNT")
    battery_mah, battery_wh = _battery(log)
    return FlightRecord(
        source="ardupilot",
        log_ref=path.stem,
        licence=licence,
        attribution=attribution,
        firmware=firmware_from_messages(log.texts),
        log_span_s=(last_us - first_us) / 1e6,
        aircraft_key=_aircraft_key(log),
        utc_start=_utc_start(log),
        flight_time_s=_time_while_set(log.flying) if log.flying else no_stat,
        arm_cycles=_arm_cycles(log),
        landings=len(_falling(log.flying)) if log.flying else no_stat,
        battery_mah=battery_mah,
        battery_wh=battery_wh,
        fault_events=_faults(log, first_us),
        boot_count=int(boot) if boot is not None else Unknown("STAT_BOOTCNT not in parameters"),
        lifetime=_lifetime(log, first_us),
    )
