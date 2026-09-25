"""Write a synthetic ArduPilot DataFlash (.bin) file."""

from __future__ import annotations

import struct
from pathlib import Path

HEAD = b"\xa3\x95"
FMT_TYPE = 0x80
# DataFlash format characters used in these tests and their struct codes (no scaled types).
CODES = {
    "b": "b",
    "B": "B",
    "h": "h",
    "H": "H",
    "i": "i",
    "I": "I",
    "f": "f",
    "d": "d",
    "n": "4s",
    "N": "16s",
    "Z": "64s",
    "q": "q",
    "Q": "Q",
}


class DataFlashBuilder:
    def __init__(self) -> None:
        self._out = bytearray()
        self._types: dict[str, tuple[int, str]] = {}

    def define(self, name: str, fmt: str, columns: str) -> DataFlashBuilder:
        type_id = 0x81 + len(self._types)
        struct_fmt = "<" + "".join(CODES[c] for c in fmt)
        length = 3 + struct.calcsize(struct_fmt)
        self._types[name] = (type_id, struct_fmt)
        payload = struct.pack(
            "<BB4s16s64s", type_id, length, name.encode(), fmt.encode(), columns.encode()
        )
        self._out += HEAD + bytes([FMT_TYPE]) + payload
        return self

    def msg(self, name: str, *values: int | float | str) -> DataFlashBuilder:
        type_id, struct_fmt = self._types[name]
        encoded = [v.encode() if isinstance(v, str) else v for v in values]
        self._out += HEAD + bytes([type_id]) + struct.pack(struct_fmt, *encoded)
        return self

    def write(self, path: Path) -> Path:
        path.write_bytes(bytes(self._out))
        return path
