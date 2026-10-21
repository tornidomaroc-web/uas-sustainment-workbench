"""FlightRecord from a PX4 ULog (.ulg).

Every derivation names the topic it rests on. Where the log does not carry the topic, the
field is Unknown with that reason.
"""

from __future__ import annotations

import io
from collections.abc import Buffer
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import BinaryIO

import numpy as np
from pyulog import ULog

from uas_workbench.census.model import FloatArray
from uas_workbench.census.px4 import (
    ARMING_STATE_ARMED,
    LOG_LEVEL_ERR,
    firmware_version,
    syslog_level,
)

from .record import NOT_COUNTED, FaultEvent, FlightRecord, LifetimeCounter, Maybe, Unknown

TOPICS = (
    "vehicle_status",
    "vehicle_land_detected",
    "battery_status",
    "vehicle_gps_position",
    "sensor_gps",
    "failure_detector_status",
)
FAILURE_FLAGS = ("fd_motor", "fd_imbalanced_prop", "fd_battery", "fd_roll", "fd_pitch", "fd_alt")


def _has_data(path: Path, end: int | None) -> bool:
    """Whether the log holds a data message of any topic. Definitions, info, parameters and
    logged text with no data are a logger started and stopped before its first sample, no
    evidence of a flight. pyulog keeps data only of the topics it is asked for, so a log with
    none of TOPICS is read again whole; the topics it holds then are the answer."""
    with _open(path, end) as handle:
        return bool(ULog(handle, disable_str_exceptions=True).data_list)


def _dataset(ulog: ULog, name: str) -> ULog.Data | None:
    for dataset in ulog.data_list:
        if dataset.name == name and dataset.multi_id == 0:
            return dataset
    return None


def _column(dataset: ULog.Data, name: str) -> FloatArray | None:
    if name not in dataset.data:
        return None
    return np.asarray(dataset.data[name], dtype=np.float64)


def _rising(values: FloatArray) -> int:
    on = values != 0
    return int(np.sum(~on[:-1] & on[1:]))


def _falling(values: FloatArray) -> int:
    on = values != 0
    return int(np.sum(on[:-1] & ~on[1:]))


def _aircraft_key(ulog: ULog) -> Maybe[str]:
    raw = ulog.msg_info_dict.get("sys_uuid")
    key = str(raw).strip().strip("\x00") if raw is not None else ""
    if key and set(key) != {"0"}:
        return key
    return Unknown("sys_uuid missing or all zeros")


def _utc_start(ulog: ULog) -> Maybe[datetime]:
    for topic in ("vehicle_gps_position", "sensor_gps"):
        dataset = _dataset(ulog, topic)
        if dataset is None:
            continue
        utc = _column(dataset, "time_utc_usec")
        timestamps = _column(dataset, "timestamp")
        if utc is None or timestamps is None or not np.any(utc > 0):
            continue
        i = int(np.argmax(utc > 0))
        fix = datetime.fromtimestamp(utc[i] / 1e6, tz=UTC)
        return fix - timedelta(microseconds=float(timestamps[i] - ulog.start_timestamp))
    return Unknown("no GPS UTC time in vehicle_gps_position or sensor_gps")


def _flight_and_landings(ulog: ULog) -> tuple[Maybe[float], Maybe[int]]:
    dataset = _dataset(ulog, "vehicle_land_detected")
    if dataset is None:
        reason = Unknown("no vehicle_land_detected topic")
        return reason, reason
    landed = _column(dataset, "landed")
    t = _column(dataset, "timestamp")
    if landed is None or t is None:
        reason = Unknown("vehicle_land_detected has no landed field")
        return reason, reason
    airborne = landed[:-1] == 0
    flight_s = float(np.sum(np.diff(t)[airborne]) / 1e6)
    return flight_s, _rising(landed)


def _arm_cycles(ulog: ULog) -> Maybe[int]:
    dataset = _dataset(ulog, "vehicle_status")
    if dataset is None:
        return Unknown("no vehicle_status topic")
    state = _column(dataset, "arming_state")
    if state is None:
        return Unknown("vehicle_status has no arming_state field")
    armed = (state == ARMING_STATE_ARMED).astype(np.float64)
    # PX4 starts logging at arming by default, so a log that opens armed is inside a cycle.
    return _rising(armed) + int(armed[0] != 0)


def _battery(ulog: ULog) -> tuple[Maybe[float], Maybe[float]]:
    dataset = _dataset(ulog, "battery_status")
    if dataset is None:
        reason = Unknown("no battery_status topic")
        return reason, reason
    t = _column(dataset, "timestamp")
    discharged = _column(dataset, "discharged_mah")
    voltage = _column(dataset, "voltage_v")
    current = _column(dataset, "current_a")
    assert t is not None
    mah: Maybe[float]
    if discharged is not None and np.any(discharged >= 0):
        valid = discharged[discharged >= 0]
        mah = float(valid[-1] - valid[0])
    elif current is not None and np.any(current > 0):
        ok = current >= 0
        mah = float(np.trapezoid(current[ok], t[ok] / 1e6) / 3.6)
    else:
        mah = Unknown("battery_status has no valid discharged_mah and no current")
    if current is None or voltage is None or not np.any(current > 0):
        return mah, Unknown("battery_status has no current measurement to integrate")
    ok = (current >= 0) & (voltage > 0)
    wh = float(np.trapezoid(current[ok] * voltage[ok], t[ok] / 1e6) / 3600)
    return mah, wh


def _faults(ulog: ULog) -> Maybe[tuple[FaultEvent, ...]]:
    detector = _dataset(ulog, "failure_detector_status")
    messages = ulog.logged_messages
    if detector is None and not messages:
        return Unknown("no failure_detector_status topic and no logged messages")
    start = ulog.start_timestamp
    events = [
        FaultEvent((m.timestamp - start) / 1e6, "error_message", m.message)
        for m in messages
        if syslog_level(m.log_level) <= LOG_LEVEL_ERR
    ]
    if detector is not None:
        t = _column(detector, "timestamp")
        assert t is not None
        for flag in FAILURE_FLAGS:
            values = _column(detector, flag)
            if values is None:
                continue
            on = values != 0
            for i in np.flatnonzero(~on[:-1] & on[1:]) + 1:
                events.append(
                    FaultEvent((t[i] - start) / 1e6, f"failure_detector:{flag}", "flag raised")
                )
    return tuple(sorted(events, key=lambda e: e.t_s))


def _counter_value(hi: float, lo: float) -> float:
    return ((int(hi) << 32) | (int(lo) & 0xFFFFFFFF)) / 1e6


def _lifetime(ulog: ULog) -> Maybe[LifetimeCounter]:
    params = ulog.initial_parameters
    if "LND_FLIGHT_T_HI" not in params or "LND_FLIGHT_T_LO" not in params:
        return Unknown("LND_FLIGHT_T_HI/LO not in parameters")
    hi, lo = float(params["LND_FLIGHT_T_HI"]), float(params["LND_FLIGHT_T_LO"])
    at_boot = _counter_value(hi, lo)
    last_seen, last_at = at_boot, 0.0
    for timestamp, name, value in ulog.changed_parameters:  # PX4 saves at disarm, unnotified
        if name == "LND_FLIGHT_T_HI":
            hi = float(value)
        elif name == "LND_FLIGHT_T_LO":
            lo = float(value)
        else:
            continue
        last_seen, last_at = _counter_value(hi, lo), (timestamp - ulog.start_timestamp) / 1e6
    return LifetimeCounter(at_boot, last_seen, last_at)


# The most bytes one message takes, its 3-byte header included. A zero run longer than this
# cannot lie wholly inside the log's last message, so three zeros at least follow that message
# and pyulog would mark the file corrupt for them.
LONGEST_MESSAGE = 3 + 0xFFFF
FILE_HEADER = 16


class _EndingAt(io.FileIO):
    """The file as if it ended at `end`: a reader sees nothing past it. Wrapped in a buffered
    reader, it runs once per buffer, not once per message."""

    def __init__(self, path: Path, end: int) -> None:
        super().__init__(path, "rb")
        self._end = end

    def readinto(self, buffer: Buffer) -> int:
        return super().readinto(memoryview(buffer)[: max(0, self._end - self.tell())])

    def read(self, size: int | None = -1) -> bytes:
        room = max(0, self._end - self.tell())
        return super().read(room if size is None or size < 0 else min(size, room))

    def readall(self) -> bytes:
        return self.read()


def _zero_tail(path: Path) -> tuple[int, int] | None:
    """Where the zero bytes the file ends in begin and how many there are, or None if its last
    byte is not zero."""
    with path.open("rb") as handle:
        size = end = handle.seek(0, io.SEEK_END)
        while end > 0:
            start = max(0, end - (1 << 16))
            handle.seek(start)
            if kept := handle.read(end - start).rstrip(b"\x00"):
                zeros_from = start + len(kept)
                return (zeros_from, size - zeros_from) if zeros_from < size else None
            end = start
    return (0, size) if size else None


def _open(path: Path, end: int | None) -> BinaryIO:
    return path.open("rb") if end is None else io.BufferedReader(_EndingAt(path, end), 1 << 16)


def _parse(path: Path, end: int | None = None) -> ULog:
    # Given a path, pyulog opens the file itself and leaves it open when parsing fails; given
    # a handle, it reads from it, and this block closes it either way.
    with _open(path, end) as handle:
        return ULog(handle, message_name_filter_list=list(TOPICS), disable_str_exceptions=True)


def read_ulog(path: Path, *, licence: str, attribution: str) -> FlightRecord:
    # A log still being written when the power went can end in zero bytes, which pyulog reads
    # as message headers and marks the file corrupt for. The log is then read as if it ended
    # where they begin, as a log cut there reads: to its last whole message. A message that
    # runs into the zeros is dropped as a cut one is, since a cut message followed by zeros has
    # the bytes of a whole message whose last bytes are zero, and a record is never built from
    # bytes that may be padding. A file pyulog reads whole without damage is read as it is: the
    # zeros it ends in are then its last message's own. A run too long to be that is not read
    # at all, which spares pyulog its walk through it, byte by byte.
    tail = _zero_tail(path)
    if tail is not None and tail[0] < FILE_HEADER:  # pyulog refuses the file on its header
        tail = None
    end = None if tail is None or tail[1] <= LONGEST_MESSAGE else tail[0]
    ulog = _parse(path, end)
    if tail is not None and end is None and ulog.file_corruption:
        end = tail[0]
        ulog = _parse(path, end)
    # pyulog skips what it cannot parse and says so: a data message whose framing breaks, and
    # from 1.3.0 a definition or a subscription too, where 1.2.4 raised. What is left can lack
    # a topic, the aircraft key or most of the span, so the log is refused, as a damaged
    # DataFlash log is, and the operator is told what that leaves out of the counts.
    if ulog.file_corruption:
        raise ValueError(
            f"pyulog found messages it could not parse and skipped them; {NOT_COUNTED}"
        )
    if not ulog.data_list and not _has_data(path, end):
        raise ValueError("holds a header but no recorded data")
    flight_s, landings = _flight_and_landings(ulog)
    battery_mah, battery_wh = _battery(ulog)
    return FlightRecord(
        source="px4",
        log_ref=path.stem,
        licence=licence,
        attribution=attribution,
        firmware=firmware_version(ulog),
        log_span_s=(ulog.last_timestamp - ulog.start_timestamp) / 1e6,
        aircraft_key=_aircraft_key(ulog),
        utc_start=_utc_start(ulog),
        flight_time_s=flight_s,
        arm_cycles=_arm_cycles(ulog),
        landings=landings,
        battery_mah=battery_mah,
        battery_wh=battery_wh,
        fault_events=_faults(ulog),
        boot_count=Unknown("PX4 logs no boot counter"),
        lifetime=_lifetime(ulog),
    )
