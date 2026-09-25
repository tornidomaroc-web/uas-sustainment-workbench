"""Write a synthetic ULog (PX4) file."""

from __future__ import annotations

import re
import struct
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

MAGIC = b"ULog\x01\x12\x35"
SCALARS = {
    "int8_t": "b",
    "uint8_t": "B",
    "int16_t": "h",
    "uint16_t": "H",
    "int32_t": "i",
    "uint32_t": "I",
    "int64_t": "q",
    "uint64_t": "Q",
    "float": "f",
    "double": "d",
    "bool": "?",
    "char": "c",
}
FIELD = re.compile(r"(?P<type>\w+)(?:\[(?P<count>\d+)\])? (?P<name>\w+)")


class ULogBuilder:
    def __init__(self, start_us: int = 1_000_000) -> None:
        self.start_us = start_us
        self._definitions = bytearray()
        self._data = bytearray()
        self._formats: dict[str, list[tuple[str, int, str]]] = {}
        self._topic_of: dict[int, str] = {}

    @staticmethod
    def _message(out: bytearray, kind: str, payload: bytes) -> None:
        out += struct.pack("<HB", len(payload), ord(kind)) + payload

    def format(self, name: str, fields: Sequence[str]) -> ULogBuilder:
        """fields like "uint64_t timestamp", "float[2] x", or a nested "esc_report[2] esc"."""
        parsed = []
        for spec in fields:
            m = FIELD.fullmatch(spec)
            assert m, spec
            parsed.append((m["type"], int(m["count"] or 1), m["name"]))
        self._formats[name] = parsed
        self._message(self._definitions, "F", f"{name}:{';'.join(fields)};".encode())
        return self

    def info(self, key: str, text: str) -> ULogBuilder:
        encoded = text.encode()
        typed_key = f"char[{len(encoded)}] {key}".encode()
        self._message(self._definitions, "I", bytes([len(typed_key)]) + typed_key + encoded)
        return self

    def param(self, name: str, value: int | float) -> ULogBuilder:
        typed_key, packed = (
            (f"int32_t {name}", struct.pack("<i", value))
            if isinstance(value, int)
            else (f"float {name}", struct.pack("<f", value))
        )
        key = typed_key.encode()
        self._message(self._definitions, "P", bytes([len(key)]) + key + packed)
        return self

    def subscribe(self, topic: str, multi_id: int = 0) -> int:
        msg_id = len(self._topic_of)
        self._topic_of[msg_id] = topic
        self._message(self._data, "A", struct.pack("<BH", multi_id, msg_id) + topic.encode())
        return msg_id

    def _pack(self, fmt_name: str, values: Mapping[str, Any]) -> bytes:
        out = bytearray()
        for ftype, count, name in self._formats[fmt_name]:
            value = values.get(name, [0] * count if count > 1 else 0)
            items = value if isinstance(value, list) else [value]
            if ftype in SCALARS:
                out += struct.pack(f"<{count}{SCALARS[ftype]}", *items)
            else:  # nested message type: a list of dicts
                for item in items:
                    out += self._pack(ftype, item)
        return bytes(out)

    def data(self, msg_id: int, values: Mapping[str, Any]) -> ULogBuilder:
        payload = struct.pack("<H", msg_id) + self._pack(self._topic_of[msg_id], values)
        self._message(self._data, "D", payload)
        return self

    def log(self, level: int, text: str, timestamp: int) -> ULogBuilder:
        """level 0-7 as in syslog; written as an ASCII digit, as PX4 does."""
        payload = bytes([ord("0") + level]) + struct.pack("<Q", timestamp) + text.encode()
        self._message(self._data, "L", payload)
        return self

    def build(self) -> bytes:
        header = MAGIC + b"\x01" + struct.pack("<Q", self.start_us)
        flags = bytearray()
        self._message(flags, "B", bytes(40))  # compat, incompat, appended offsets: all zero
        return header + bytes(flags) + bytes(self._definitions) + bytes(self._data)

    def write(self, path: Path) -> Path:
        path.write_bytes(self.build())
        return path
