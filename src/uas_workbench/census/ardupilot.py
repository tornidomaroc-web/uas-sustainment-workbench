"""Census probe for ArduPilot DataFlash files (.bin), built on pymavlink's DFReader."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from pymavlink import DFReader

from .model import (
    ABSENT,
    FieldResult,
    FieldSpec,
    FilledTest,
    FloatArray,
    LogCensus,
    equals,
    nonzero,
    positive,
    toggles,
)

EV_ARMED = 10  # LogEvent::ARMED in AP_Logger
# Boot banner printing the flight controller's unique MCU id: "<board> 0038002A 3238510D ..."
BOARD_UID = re.compile(r"\b[0-9A-F]{8} [0-9A-F]{8} [0-9A-F]{8}\b")
FIRMWARE = re.compile(r"^(Ardu\w+|ArduPilot)\s+V?(\d+\.\d+(?:\.\d+)?)")


@dataclass(frozen=True)
class MessageField:
    """A field read from one of several message types; the first one present wins.
    Several types cover renames between firmware versions (CURR became BAT in 3.10)."""

    spec: FieldSpec
    sources: tuple[tuple[str, str], ...]
    test: FilledTest


def _mf(
    key: str,
    counter: str,
    means: str,
    test: FilledTest,
    *sources: tuple[str, str],
    extra: bool = False,
) -> MessageField:
    if not sources:
        mtype, _, fname = key.partition(".")
        sources = ((mtype, fname),)
    return MessageField(FieldSpec(key, counter, means, extra), sources, test)


MESSAGE_FIELDS: tuple[MessageField, ...] = (
    _mf("ARM.ArmState", "arm_cycles", "armed (1) observed", nonzero),
    _mf("EV.Id", "arm_cycles", "ARMED event (10) observed", equals(EV_ARMED)),
    _mf("STAT.isFlying", "landings", "changes state (both values seen)", toggles),
    _mf("STAT.Armed", "arm_cycles", "armed observed", nonzero),
    _mf("STAT.Crash", "fault_events", "crash flagged", nonzero),
    _mf("STAT.Hit", "fault_events", "hit flagged", nonzero),
    _mf(
        "BAT.CurrTot (CURR before 3.10)",
        "battery_energy",
        "> 0 mAh",
        positive,
        ("BAT", "CurrTot"),
        ("CURR", "CurrTot"),
    ),
    _mf(
        "BAT.EnrgTot (CURR before 3.10)",
        "battery_energy",
        "> 0 Wh",
        positive,
        ("BAT", "EnrgTot"),
        ("CURR", "EnrgTot"),
    ),
    _mf("BAT.SH", "battery_energy", "non-zero", nonzero),
    _mf("ESC.RPM", "esc_data", "non-zero rpm", nonzero),
    _mf("ESC.Temp", "esc_data", "non-zero temperature", nonzero),
    _mf("ESC.Err", "esc_data", "errors reported (> 0)", nonzero),
    _mf("ERR.ECode", "fault_events", "non-zero error code", nonzero),
    _mf("GPS.GWk", "utc_date", "non-zero GPS week", nonzero),
)
RCOU_FIELD = FieldSpec("RCOU.C1..C14", "esc_data", "any output non-zero (load proxy)")
PARAM_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("PARM STAT_FLTTIME", "lifetime_hours", "non-zero"),
    FieldSpec("PARM STAT_BOOTCNT", "lifetime_hours", "non-zero"),
    FieldSpec("PARM STAT_RUNTIME", "lifetime_hours", "non-zero"),
    FieldSpec("PARM STAT_FLTCNT", "landings", "non-zero"),
    FieldSpec("PARM BRD_SERIAL_NUM", "aircraft_key", "non-zero (user-set)", extra=True),
    FieldSpec("PARM BATT_SERIAL_NUM", "battery_energy", "not 0 or -1", extra=True),
)
BOARD_UID_FIELD = FieldSpec(
    "MSG boot banner with MCU id", "aircraft_key", "id pattern found", extra=True
)

FIELD_SPECS: tuple[FieldSpec, ...] = (
    BOARD_UID_FIELD,
    *(mf.spec for mf in MESSAGE_FIELDS),
    RCOU_FIELD,
    *PARAM_FIELDS,
)

_WANTED: dict[str, set[str]] = defaultdict(set)
for _mfield in MESSAGE_FIELDS:
    for _mtype, _fname in _mfield.sources:
        _WANTED[_mtype].add(_fname)
_WANTED["GPS"].add("Spd")
READ_TYPES: tuple[str, ...] = (*sorted(_WANTED), "RCOU", "PARM", "MSG")
RCOU_CHANNEL = re.compile(r"C\d+")


@dataclass
class _Collected:
    series: dict[tuple[str, str], list[float]]
    rcou: list[float]
    params: dict[str, float]
    texts: list[str]
    first_us: int | None = None
    last_us: int | None = None


def _read(path: Path) -> _Collected:
    log = DFReader.DFReader_binary(str(path), zero_time_base=True)
    got = _Collected(series=defaultdict(list), rcou=[], params={}, texts=[])
    try:
        _collect(log, got)
    finally:
        log.close()  # releases the memory map, which otherwise locks the file on Windows
    return got


def _collect(log: DFReader.DFReader_binary, got: _Collected) -> None:
    while (msg := log.recv_match(type=list(READ_TYPES))) is not None:
        mtype = msg.get_type()
        time_us = getattr(msg, "TimeUS", None)
        if isinstance(time_us, int) and time_us > 0:
            got.first_us = time_us if got.first_us is None else min(got.first_us, time_us)
            got.last_us = time_us if got.last_us is None else max(got.last_us, time_us)
        if mtype == "PARM":
            got.params.setdefault(str(msg.Name), float(msg.Value))
        elif mtype == "MSG":
            got.texts.append(str(msg.Message))
        elif mtype == "RCOU":
            got.rcou.extend(
                float(getattr(msg, name))
                for name in msg.get_fieldnames()
                if RCOU_CHANNEL.fullmatch(name)
            )
        else:
            for fname in _WANTED.get(mtype, ()):
                value = getattr(msg, fname, None)
                if value is not None:
                    got.series[(mtype, fname)].append(float(value))


def _values(got: _Collected, sources: tuple[tuple[str, str], ...]) -> FloatArray | None:
    for source in sources:
        if got.series.get(source):
            return np.asarray(got.series[source], dtype=np.float64)
    return None


def _probe_param(got: _Collected, spec: FieldSpec) -> FieldResult:
    name = spec.key.removeprefix("PARM ")
    if name not in got.params:
        return ABSENT
    value = got.params[name]
    if name == "BATT_SERIAL_NUM":
        return FieldResult(present=True, filled=value not in (0.0, -1.0))
    return FieldResult(present=True, filled=value != 0)


def _firmware(texts: list[str]) -> str:
    for text in texts:
        if match := FIRMWARE.match(text):
            return f"{match.group(1)} {match.group(2)}"
    return "unknown"


def probe_dataflash(path: Path, group: str) -> LogCensus:
    got = _read(path)

    fields: dict[str, FieldResult] = {
        BOARD_UID_FIELD.key: FieldResult(
            present=bool(got.texts), filled=any(BOARD_UID.search(t) for t in got.texts)
        )
    }
    for mf in MESSAGE_FIELDS:
        values = _values(got, mf.sources)
        fields[mf.spec.key] = ABSENT if values is None else FieldResult(True, mf.test(values))
    rcou = np.asarray(got.rcou, dtype=np.float64)
    fields[RCOU_FIELD.key] = FieldResult(present=rcou.size > 0, filled=nonzero(rcou))
    for spec in PARAM_FIELDS:
        fields[spec.key] = _probe_param(got, spec)

    def filled(key: str) -> bool:
        return fields[key].filled

    flying = _values(got, (("STAT", "isFlying"),))
    speed = _values(got, (("GPS", "Spd"),))
    airborne = (flying is not None and bool(np.any(flying != 0))) or (
        speed is not None and bool(np.nanmax(speed) > 5.0)
    )
    counters = {
        "aircraft_key": filled(BOARD_UID_FIELD.key) or filled("PARM BRD_SERIAL_NUM"),
        "utc_date": filled("GPS.GWk"),
        "flight_time": flying is not None and bool(np.any(flying != 0)),
        "arm_cycles": filled("ARM.ArmState") or filled("EV.Id"),
        "landings": filled("STAT.isFlying"),
        "battery_energy": filled("BAT.CurrTot (CURR before 3.10)"),
        "esc_data": filled("ESC.RPM"),
        # ERR is written only when an error happens, so an event stream (EV) being recorded
        # is what makes "no ERR" a real answer.
        "fault_events": fields["EV.Id"].present,
        "lifetime_hours": filled("PARM STAT_FLTTIME"),
    }
    duration = 0.0
    if got.first_us is not None and got.last_us is not None:
        duration = (got.last_us - got.first_us) / 1e6
    return LogCensus(
        source="ardupilot",
        group=group,
        log_ref=path.stem,
        firmware=_firmware(got.texts),
        duration_s=duration,
        airborne=airborne,
        fields=fields,
        counters=counters,
    )
