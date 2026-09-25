"""Cut a privacy-safe excerpt from an ArduPilot DataFlash log, byte for byte.

Keeps only the message types the maintenance counters read, zeroes position fields and
masks the flight controller's unique id in boot messages. Everything kept is copied
unchanged, so the excerpt is still a genuine DataFlash file that pymavlink reads as usual.
This is how the real-log test fixtures in tests/fixtures/alfa/ are made (see DATA.md).
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass

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
