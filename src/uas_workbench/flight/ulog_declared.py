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

The read also ends as soon as pyulog marks the file corrupt, and the log is refused, on every
path that reads one: the flight reader, the census and the excerpt cutter (#132). Nothing after
that point changes what the flight reader does with the log (it refuses a log pyulog marks
corrupt, or reads it again only to where its zero tail begins), and pyulog would read on through
bytes that do not frame one at a time: each read as a damaged header, the bytes that header
claims read with it (up to 65 535), then one byte further, about 15 s a MiB. Among the
definitions it can read the same headers again without end: near the end of the file it reads
fewer bytes than a damaged header claims, then seeks back by all it claims.
"""

from __future__ import annotations

import io
from collections.abc import Buffer
from pathlib import Path
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


class DamagedLog(ValueError):
    """A log pyulog marks corrupt, refused in the words every refused log is given."""

    def __init__(self) -> None:
        super().__init__(
            f"pyulog found messages it could not parse and skipped them; {NOT_COUNTED}"
        )


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


def read(handle: BinaryIO, topics: list[str] | None = None) -> ULog:
    """`handle` read by pyulog (only `topics`, if given), refused with ValueError once what it
    declares passes a bound, and with DamagedLog as soon as pyulog marks the file corrupt:
    pyulog reads no further, where it would step through the rest a byte at a time. There is
    no reading on: no caller does anything with what pyulog finds after damage but refuse it.
    pyulog's public `file_corruption` is checked too, so a pyulog that stops setting the
    attribute watched here still has a log it marks corrupt refused, only read to its end."""
    counts = _Counts()
    try:
        ulog = _bounded(counts, True)(
            handle, message_name_filter_list=topics, disable_str_exceptions=True
        )
    except _Damaged:
        damaged = True
    except DeclaredTooMuch as too_much:
        raise ValueError(f"its format definitions {too_much}; {NOT_COUNTED}") from None
    else:
        damaged = ulog.file_corruption
    if damaged:
        raise DamagedLog
    return ulog


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


def open_to(path: Path, end: int | None) -> BinaryIO:
    """The file, or the file as if it ended at `end`."""
    return path.open("rb") if end is None else io.BufferedReader(_EndingAt(path, end), 1 << 16)


def _parse(path: Path, end: int | None, topics: list[str] | None) -> ULog | None:
    """The log as pyulog reads it, refused past what its definitions may declare (#125), or
    None as soon as pyulog marks it corrupt, where the read ends: the log is then refused, or
    read again to where its zero tail begins, whatever pyulog would find further on."""
    # Given a path, pyulog opens the file itself and leaves it open when parsing fails; given
    # a handle, it reads from it, and this block closes it either way.
    with open_to(path, end) as handle:
        try:
            return read(handle, topics)
        except DamagedLog:
            return None


def read_file(path: Path, topics: list[str] | None = None) -> tuple[ULog, int | None]:
    """The log at `path` (only `topics`, if given) and where it was read to (None: the whole
    file), refused as `read` refuses one. The flight reader, the census and the excerpt cutter
    all read a file by this (#132), each for its own topics: pyulog checks the size of a data
    message only for a topic it is asked for, so damage there is found only by a reader that
    asks for that topic."""
    # A log still being written when the power went can end in zero bytes, which pyulog reads
    # as message headers and marks the file corrupt for. The log is then read as if it ended
    # where they begin, as a log cut there reads: to its last whole message. A message that
    # runs into the zeros is dropped as a cut one is, since a cut message followed by zeros has
    # the bytes of a whole message whose last bytes are zero, and no record is built from
    # zeros pyulog reads as damage. A file pyulog reads whole without damage is read as it
    # is: the zeros it ends in are then its last message's own, or the exact bytes a message
    # cut after its header lacks, plus one or two, which no reader can tell apart. A zero run
    # longer than one message can hold is not read at all, which spares pyulog its walk
    # through it, byte by byte.
    tail = _zero_tail(path)
    if tail is not None and tail[0] < FILE_HEADER:  # pyulog refuses the file on its header
        tail = None
    end = None if tail is None or tail[1] <= LONGEST_MESSAGE else tail[0]
    ulog = _parse(path, end, topics)
    if tail is not None and end is None and ulog is None:
        end = tail[0]
        ulog = _parse(path, end, topics)
    # pyulog skips what it cannot parse and says so: a data message whose framing breaks, a
    # definition or a subscription it cannot read, a definition declaring an array of more
    # than 65 535 elements (PX4/pyulog#128). What is left can lack a topic, the aircraft key or
    # most of the span, so the log is refused, as a damaged DataFlash log is, and the operator
    # is told what that leaves out of the counts.
    if ulog is None:
        raise DamagedLog
    return ulog, end
