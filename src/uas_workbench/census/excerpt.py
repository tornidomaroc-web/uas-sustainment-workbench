"""Cut privacy-safe excerpts from real flight logs for use as test fixtures.

ArduPilot DataFlash: keeps only the message types the maintenance counters read, byte for
byte, zeroes position fields and masks the flight controller's unique id in boot messages.
PX4 ULog: keeps only the topics the counters read, zeroes position fields, removes the
sys_uuid info message and the multi-line info blocks (boot console output). Both outputs
are genuine log files that pymavlink and pyulog read as usual. This is how the fixtures in
tests/fixtures/ are made (see DATA.md). A ULog that pyulog could not write back as it was
read, an information message its writer cannot pack or a parameter of a type the format does
not allow, is refused rather than cut into a different log (#138, #141, #143).
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass
from pathlib import Path

from pyulog import ULog

from uas_workbench.flight import ulog_declared

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


# Info fields that name the hardware product or its vendor's firmware branch. This project
# names no real aircraft or drone product, so they are dropped with the device id.
ULOG_DROP_INFO: tuple[str, ...] = ("sys_uuid", "ver_hw", "ver_hw_subtype", "ver_sw_branch")


def strip_identity(ulog: ULog) -> ULog:
    """Remove the device id, the hardware name and the multi-line info blocks, in place."""
    for key in ULOG_DROP_INFO:
        ulog.msg_info_dict.pop(key, None)
    ulog.msg_info_multiple_dict.clear()
    return ulog


def excerpt_ulog(src: Path) -> ULog:
    """Load a ULog keeping only ULOG_KEEP, then strip identity and position in memory.
    Write the result with ULog.write_ulog(). Read as the flight reader reads a file: a log
    past a bound, or one pyulog marks corrupt, is refused with ValueError (#125, #132), as is
    one holding an information message pyulog cannot write back (#138) or a parameter of a
    type the ULog format does not allow, which pyulog would write back as another (#141, #143)."""
    ulog, _ = ulog_declared.read_file(src, list(ULOG_KEEP))
    strip_identity(ulog)
    unwritable = unwritable_info(ulog)
    if unwritable:
        named = ", ".join(f"'{typed_key}'" for typed_key in unwritable)
        raise ValueError(
            f"it holds an information message pyulog cannot write back ({named}); "
            "nothing is written"
        )
    disallowed = disallowed_parameters(ulog)
    if disallowed:
        named = ", ".join(f"'{typed_key}'" for typed_key in disallowed)
        raise ValueError(
            f"it holds a parameter of a type the ULog format does not allow ({named}); "
            "nothing is written"
        )
    for dataset in ulog.data_list:
        for name, values in dataset.data.items():
            if ULOG_ZERO.match(name):
                values[:] = 0
    return ulog


def unwritable_info(ulog: ULog) -> list[str]:
    """The typed keys of the information messages ULog.write_ulog() cannot pack, in file order.

    pyulog reads an information message of a type it does not know (`uint32_t[2] some_key`)
    without marking damage and keeps its value as raw bytes; its writer packs only text, byte
    arrays and the scalar types it knows, and raises KeyError on the rest (#138)."""
    types = ulog._msg_info_dict_types | ulog._msg_info_multiple_dict_types
    keys = [*ulog.msg_info_dict, *ulog.msg_info_multiple_dict]
    return [
        f"{types[key]} {key}"
        for key in keys
        if not types[key].startswith(("char[", "uint8_t[")) and types[key] not in ULog._UNPACK_TYPES
    ]


# The ULog format restricts a parameter message to these two types, the two pyulog's writer
# packs: a Python int as int32_t, a Python float as float.
PARAMETER_TYPES: frozenset[str] = frozenset({"int32_t", "float"})


def disallowed_parameters(ulog: ULog) -> list[str]:
    """The typed keys of the parameters whose declared type the ULog format does not allow, in
    file order: initial (P), changed in flight (P in the data section) and default (Q) alike.

    pyulog reads a parameter of any declared type without marking damage and keeps no record
    of the type, so its writer would pack a `double` back as `float` and an `int64_t` or
    `uint32_t` in the int32 range as `int32_t`, with no word (#143), and raise on text, an
    array or an integer past int32 (#141). The declared type is read in the bounded reader,
    where pyulog parses it (`ulog_declared.read`)."""
    types: list[str] = ulog.parameter_types
    return [typed_key for typed_key in types if typed_key.split(" ", 1)[0] not in PARAMETER_TYPES]
