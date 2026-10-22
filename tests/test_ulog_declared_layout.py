"""The count the reader bounds is what pyulog lays out (issue #125).

For each definition below, pyulog's own subscription object is built, and its elements and the
characters of their names are compared with what the count says before pyulog lays anything
out: scalars and arrays, definitions nested with and without arrays, two levels deep, and
array sizes across each width of index.
"""

from __future__ import annotations

import struct

import pytest
from pyulog import ULog

from uas_workbench.flight import ulog_declared

DEFINITIONS = {
    "inner": ["uint64_t timestamp", "float[3] xyz", "uint8_t[11] flags"],
    "pair": ["inner a", "inner[2] b", "int16_t c"],
    "wide": ["uint8_t[9] n9", "uint8_t[10] n10", "uint8_t[101] n101", "char[1001] text"],
    "deep": ["uint64_t timestamp", "pair[12] p", "wide w", "double[3] d"],
    "padded": ["uint64_t timestamp", "uint8_t[3] _padding0"],
}


def formats() -> dict[str, ULog.MessageFormat]:
    out = {}
    for name, fields in DEFINITIONS.items():
        parsed = ULog.MessageFormat(f"{name}:{';'.join(fields)};".encode(), None)
        out[parsed.name] = parsed
    return out


class Unpadded(ULog._MessageAddLogged):  # type: ignore[misc]
    """pyulog's subscription with the trailing padding left in, as it is laid out."""

    def _parse_format(self, message_formats: dict[str, ULog.MessageFormat]) -> None:
        self._parse_nested_type("", self.message_name, message_formats)


@pytest.mark.parametrize("name", list(DEFINITIONS))
def test_the_count_is_what_pyulog_lays_out(name: str) -> None:
    laid_out = Unpadded(struct.pack("<BH", 0, 1) + name.encode(), None, formats()).field_data
    elements, characters, _ = ulog_declared._Counts().layout(name, formats())
    assert (elements, characters) == (
        len(laid_out),
        sum(len(f.field_name) for f in laid_out),
    )


def test_depth_counts_each_definition_on_the_way_down() -> None:
    assert ulog_declared._Counts().layout("deep", formats())[2] == 3
    assert ulog_declared._Counts().layout("inner", formats())[2] == 1
