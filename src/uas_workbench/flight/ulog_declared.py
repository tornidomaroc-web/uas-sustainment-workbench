"""pyulog bounded by what a ULog declares (issue #125).

pyulog builds an object for every field of every format definition, and for every subscription
it lays out each element of the definition subscribed, a definition nested in another once per
element of the array that holds it, each element with a name of its own, whether or not the
reader keeps the topic. A file of a few hundred bytes can declare billions of elements that way
(a field of 65 535 elements, each a definition of 65 535), and a definition that holds itself,
or a chain of a thousand, ends pyulog in a RecursionError.

`read` counts, as pyulog reads, what it is about to lay out: each definition and its fields as
pyulog parses them, and before each subscription is laid out, its elements and the characters of
their names, nested definitions multiplied out from the fields pyulog parsed. It refuses the log
once a count passes its bound, before pyulog lays that subscription out. The count is taken
from pyulog's own objects, on every path pyulog reads by, so it is what pyulog lays out, on
1.2.4 as on 1.3.0, and no byte of the file is read twice for it.

With `stop_at_damage`, the read also ends as soon as pyulog marks the file corrupt. Nothing
after that point changes what the flight reader does with the log (it refuses a log pyulog
marks corrupt, or reads it again only to where its zero tail begins), and pyulog would read on
through bytes that do not frame one at a time: each read as a damaged header, the bytes that
header claims read with it (up to 65 535), then one byte further, about 15 s a MiB.
"""

from __future__ import annotations

from typing import Any, BinaryIO

from pyulog import ULog

from .record import NOT_COUNTED

# The most definitions, fields and elements pyulog lays out in one read of a log, the most
# characters in the names it gives the elements, and the deepest one definition may hold
# another. The 45 real logs measured for #125 (44 distinct) reach at most 20,344, 440,552 and 2.
MAX_ELEMENTS = 1 << 20
MAX_NAME_CHARACTERS = 1 << 25
MAX_DEPTH = 16


_TOO_DEEP = f"nest more than {MAX_DEPTH} deep, or hold themselves, deeper than the workbench reads"


class DeclaredTooMuch(Exception):
    """Past a bound. Not a ValueError, KeyError or IndexError: pyulog catches those as damage
    and reads on."""


class _Damaged(Exception):
    """pyulog has marked the file corrupt."""


def _digits(n: int) -> int:
    """The characters of str(i) for every i in range(n)."""
    total, width, low = 0, 1, 0
    while low < n:
        high = min(n, 10**width)
        total += (high - low) * width
        low, width = high, width + 1
    return total


class _Counts:
    def __init__(self) -> None:
        self.elements = 0
        self.characters = 0
        self._laid_out: dict[str, tuple[int, int, int]] = {}
        self._open: set[str] = set()

    def add(self, elements: int, characters: int = 0) -> None:
        self.elements += elements
        self.characters += characters
        if self.elements > MAX_ELEMENTS:
            raise DeclaredTooMuch(
                f"declare more than {MAX_ELEMENTS:,} definitions, fields and elements for pyulog "
                "to lay out, the most the workbench reads"
            )
        if self.characters > MAX_NAME_CHARACTERS:
            raise DeclaredTooMuch(
                f"declare more than {MAX_NAME_CHARACTERS:,} characters of element names for "
                "pyulog to lay out, the most the workbench reads"
            )

    def layout(self, name: str, formats: dict[str, Any], depth: int = 1) -> tuple[int, int, int]:
        """Elements, name characters with no prefix, and depth of `name` laid out as pyulog's
        `_parse_nested_type` lays it out; a prefix of p characters adds p to every name. A
        name with no definition raises KeyError where pyulog does, which pyulog counts as
        damage."""
        if name in self._laid_out:
            return self._laid_out[name]
        if depth > MAX_DEPTH or name in self._open:
            raise DeclaredTooMuch(_TOO_DEEP)
        fields = formats[name].fields
        self._open.add(name)
        try:
            elements, characters, deepest = self._fields(fields, formats, depth)
        finally:
            self._open.discard(name)
        if depth + deepest - 1 > MAX_DEPTH:
            raise DeclaredTooMuch(_TOO_DEEP)
        self._laid_out[name] = (elements, characters, deepest)
        return self._laid_out[name]

    def _fields(
        self, fields: list[tuple[str, int, str]], formats: dict[str, Any], depth: int
    ) -> tuple[int, int, int]:
        elements = characters = 0
        deepest = 1
        for kind, size, field in fields:
            if kind in ULog._UNPACK_TYPES:
                inner_e, inner_c, joint = 1, 0, 2  # field[i]
            else:
                inner_e, inner_c, below = self.layout(kind, formats, depth + 1)
                deepest = max(deepest, 1 + below)
                joint = 3  # field[i].
            if size > 0:
                elements += size * inner_e
                characters += size * inner_c + inner_e * (
                    size * (len(field) + joint) + _digits(size)
                )
            else:
                elements += inner_e
                characters += inner_c + inner_e * (len(field) + joint - 2)
        return elements, characters, deepest


def _bounded(counts: _Counts, stop_at_damage: bool) -> type[ULog]:
    class Format(ULog.MessageFormat):  # type: ignore[misc]
        def __init__(self, data: bytes, header: Any) -> None:
            super().__init__(data, header)
            counts.add(1 + len(self.fields))

    class Subscription(ULog._MessageAddLogged):  # type: ignore[misc]
        def __init__(self, data: bytes, header: Any, message_formats: dict[str, Any]) -> None:
            if len(data) >= 3:  # shorter, pyulog's own unpacking fails first, and it stops
                elements, characters, _ = counts.layout(
                    ULog.parse_string(data[3:]), message_formats
                )
                counts.add(elements, characters)
            super().__init__(data, header, message_formats)

    class Bounded(ULog):  # type: ignore[misc]
        MessageFormat = Format
        _MessageAddLogged = Subscription

        # pyulog sets the flag False first, in __init__, so it is always set when read.
        @property
        def _file_corrupt(self) -> bool:
            return self._corrupt

        @_file_corrupt.setter
        def _file_corrupt(self, value: bool) -> None:
            self._corrupt = value
            if value and stop_at_damage:
                raise _Damaged

    return Bounded


def read(
    handle: BinaryIO, topics: list[str] | None = None, *, stop_at_damage: bool = False
) -> ULog | None:
    """`handle` read by pyulog (only `topics`, if given), refused with ValueError once what it
    declares passes a bound. With `stop_at_damage`, None as soon as pyulog marks the file
    corrupt: pyulog reads no further, where it would step through the rest a byte at a time.
    pyulog's public `file_corruption` is checked too, so a pyulog that stops setting the
    attribute watched here still has a log it marks corrupt refused, only read to its end."""
    counts = _Counts()
    try:
        ulog = _bounded(counts, stop_at_damage)(
            handle, message_name_filter_list=topics, disable_str_exceptions=True
        )
    except _Damaged:
        return None
    except DeclaredTooMuch as too_much:
        what = str(too_much)
    else:
        return None if stop_at_damage and ulog.file_corruption else ulog
    raise ValueError(f"its format definitions {what}; {NOT_COUNTED}")
