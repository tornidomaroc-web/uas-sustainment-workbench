"""Cut privacy-safe excerpts from real flight logs for use as test fixtures.

ArduPilot DataFlash: keeps only the message types the maintenance counters read, byte for
byte, zeroes position fields and masks the flight controller's unique id in boot messages.
PX4 ULog: keeps only the topics the counters read, zeroes position fields, removes the
sys_uuid info message and the multi-line info blocks (boot console output). Both outputs
are genuine log files that pymavlink and pyulog read as usual. This is how the fixtures in
tests/fixtures/ are made (see DATA.md).
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass
from pathlib import Path

from pyulog import ULog

HEAD = b"\xa3\x95"
FMT_TYPE = 0x80
FMT_LENGTH = 89
# Byte size of each DataFlash format character (AP_Logger LogStructure.h).
SIZES = {
    "a": 64, "b": 1, "B": 1, "h": 2, "H": 2, "i": 4, "I": 4, "f": 4, "d": 8, "n": 4,
    "N": 16, "Z": 64, "c": 2, "C": 2, "e": 4, "E": 4, "L": 4, "M": 1, "q": 8, "Q": 8,
}  # fmt: skip

KEEP: frozenset[str] = frozenset({"PARM", "MSG", "MODE", "STAT", "GPS"})
# Fields that locate the aircraft. Course is included: with speed and time it traces a path.
ZERO: dict[str, frozenset[str]] = {"GPS": frozenset({"Lat", "Lng", "Alt", "GCrs"})}
BOARD_ID = re.compile(rb"\b[0-9A-F]{8} [0-9A-F]{8} [0-9A-F]{8}\b")
BOARD_ID_MASK = b"XXXXXXXX XXXXXXXX XXXXXXXX"


@dataclass(frozen=True)
class _Format:
    name: str
    length: int
    offsets: dict[str, tuple[int, int]]  # column -> (byte offset in message, size)


def _parse_fmt(message: bytes) -> tuple[int, _Format]:
    type_id, length, name, fmt, columns = struct.unpack("<BB4s16s64s", message[3:FMT_LENGTH])
    fmt_text = fmt.rstrip(b"\0").decode("ascii")
    names = columns.rstrip(b"\0").decode("ascii").split(",")
    offsets, position = {}, 3
    for column, char in zip(names, fmt_text, strict=False):
        offsets[column] = (position, SIZES[char])
        position += SIZES[char]
    return type_id, _Format(name.rstrip(b"\0").decode("ascii"), length, offsets)


def excerpt(log: bytes, keep: frozenset[str] = KEEP) -> bytes:
    formats: dict[int, _Format] = {}
    out = bytearray()
    i = 0
    while i + 3 <= len(log):
        if log[i : i + 2] != HEAD:
            i += 1  # resynchronise after corruption, as the readers do
            continue
        type_id = log[i + 2]
        length = (
            FMT_LENGTH if type_id == FMT_TYPE else formats.get(type_id, _Format("", 0, {})).length
        )
        if length == 0 or i + length > len(log):
            i += 1
            continue
        message = bytearray(log[i : i + length])
        if type_id == FMT_TYPE:
            new_type, fmt = _parse_fmt(bytes(message))
            formats[new_type] = fmt
            if fmt.name in keep:
                out += message
        else:
            fmt = formats[type_id]
            if fmt.name in keep:
                for column in ZERO.get(fmt.name, ()):
                    if column in fmt.offsets:
                        start, size = fmt.offsets[column]
                        message[start : start + size] = bytes(size)
                if fmt.name == "MSG":
                    message[:] = BOARD_ID.sub(BOARD_ID_MASK, bytes(message))
                out += message
        i += length
    return bytes(out)


ULOG_KEEP: tuple[str, ...] = (
    "vehicle_status",
    "vehicle_land_detected",
    "battery_status",
    "vehicle_gps_position",
    "sensor_gps",
    "failure_detector_status",
)
# Any field that locates or orients the aircraft, in old and new PX4 field names.
ULOG_ZERO = re.compile(
    r"^(lat|lon|alt|alt_ellipsoid|latitude_deg|longitude_deg|altitude_msl_m|"
    r"altitude_ellipsoid_m|cog_rad|heading|heading_offset|heading_accuracy)$"
)


def excerpt_ulog(src: Path) -> ULog:
    """Load a ULog keeping only ULOG_KEEP, then strip identity and position in memory.
    Write the result with ULog.write_ulog()."""
    ulog = ULog(str(src), message_name_filter_list=list(ULOG_KEEP), disable_str_exceptions=True)
    ulog.msg_info_dict.pop("sys_uuid", None)
    ulog.msg_info_multiple_dict.clear()
    for dataset in ulog.data_list:
        for name, values in dataset.data.items():
            if ULOG_ZERO.match(name):
                values[:] = 0
    return ulog
