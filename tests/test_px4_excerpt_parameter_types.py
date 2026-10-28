"""`uasw-census px4-excerpt` refuses, in one line and writing nothing, a ULog holding a parameter
of a type the ULog format does not allow (#141, #143).

The format restricts a parameter message to `int32_t` and `float`: an initial parameter (P), a
default (Q) and a parameter changed in flight (P in the data section) alike. pyulog reads a
parameter of any declared type without marking damage and keeps no record of the type: a
`double` as a Python float, an `int64_t` or `uint32_t` as an int, text or an array as text or
raw bytes. Its writer packs an int as `int32_t` and a float as `float`, so up to 0.6.6 the
cutter wrote a `double` back as `float` (0.1 read again as 0.10000000149011612) and an
`int64_t` or `uint32_t` in the int32 range as `int32_t`, exit 0, with no word (#143), and ended
in pyulog's TypeError or struct.error traceback, exit 1, on text, an array or an integer past
int32 (#141). The declared type is read where it is at hand, in the bounded reader, and the
cutter refuses by it; the flight reader reads such a log as before. Each command here runs in a
process of its own.
"""

from __future__ import annotations

import struct
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from test_flight_px4 import ATTR, flown_log

from uas_workbench.flight.px4 import read_ulog

CUTTER = [sys.executable, "-m", "uas_workbench.census.cli", "px4-excerpt"]
Parameter = tuple[str, str, bytes]  # where ("P", "Q" or "changed"), typed key, bytes
I64 = "int64_t SOME_PARAM"
U32 = "uint32_t SOME_PARAM"
REFUSED: dict[str, list[Parameter]] = {
    # Up to 0.6.6 a traceback, exit 1 (#141).
    "P char[3]": [("P", "char[3] SOME_PARAM", b"abc")],
    "P uint32_t[2]": [("P", "uint32_t[2] SOME_PARAM", struct.pack("<2I", 1, 2))],
    "P uint32_t 2^31": [("P", U32, struct.pack("<I", 2**31))],
    "P int64_t 2^31": [("P", I64, struct.pack("<q", 2**31))],
    "Q char[3]": [("Q", "char[3] SOME_PARAM", b"abc")],
    "changed char[3]": [("changed", "char[3] SOME_PARAM", b"abc")],
    "changed int64_t 2^31": [("changed", I64, struct.pack("<q", 2**31))],
    # Up to 0.6.6 written back as float or int32_t, exit 0, with no word (#143).
    "P double 0.1": [("P", "double SOME_PARAM", struct.pack("<d", 0.1))],
    "P int64_t 2^31 - 1": [("P", I64, struct.pack("<q", 2**31 - 1))],
    "P int64_t -2^31": [("P", I64, struct.pack("<q", -(2**31)))],
    "P uint32_t 7": [("P", U32, struct.pack("<I", 7))],
    "Q double 0.1": [("Q", "double SOME_PARAM", struct.pack("<d", 0.1))],
    "Q int64_t 7": [("Q", I64, struct.pack("<q", 7))],
    "changed double 0.1": [("changed", "double SOME_PARAM", struct.pack("<d", 0.1))],
    "changed uint32_t 7": [("changed", U32, struct.pack("<I", 7))],
    # Named in file order: definitions first, then the data section.
    "three of them": [
        ("P", "double FIRST_PARAM", struct.pack("<d", 0.1)),
        ("Q", "int64_t SECOND_PARAM", struct.pack("<q", 7)),
        ("changed", "char[3] THIRD_PARAM", b"abc"),
    ],
}
KEPT: dict[str, list[Parameter]] = {
    "none": [],
    "P int32_t": [("P", "int32_t SOME_PARAM", struct.pack("<i", 7))],
    "P float": [("P", "float SOME_PARAM", struct.pack("<f", 0.5))],
    "Q int32_t": [("Q", "int32_t SOME_PARAM", struct.pack("<i", 7))],
    "Q float": [("Q", "float SOME_PARAM", struct.pack("<f", 0.5))],
    "changed int32_t": [("changed", "int32_t SOME_PARAM", struct.pack("<i", 7))],
    "changed float": [("changed", "float SOME_PARAM", struct.pack("<f", 0.5))],
}


def refusal(src: Path, parameters: list[Parameter]) -> str:
    named = ", ".join(f"'{typed_key}'" for _, typed_key, _ in parameters)
    return (
        f"refused: {src}: it holds a parameter of a type the ULog format does not allow "
        f"({named}); nothing is written"
    )


def parameter_messages(path: Path) -> list[str]:
    """Every parameter message in the file, as "P type key" or "Q type key", in file order: the
    messages framed one after another from the 16-byte file header, as pyulog frames them."""
    raw = path.read_bytes()
    at, found = 16, []
    while at + 3 <= len(raw):
        size, kind = struct.unpack_from("<HB", raw, at)
        body = raw[at + 3 : at + 3 + size]
        if kind in (ord("P"), ord("Q")):
            if kind == ord("Q"):
                body = body[1:]  # the default-type bits
            found.append(f"{chr(kind)} {body[1 : 1 + body[0]].decode()}")
        at += 3 + size
    return found


def cut(src: Path, dest: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*CUTTER, "--src", str(src), "--dest", str(dest)],
        capture_output=True,
        text=True,
        timeout=120,
    )


@pytest.mark.parametrize("shape", REFUSED)
def test_it_is_refused_in_one_line_and_nothing_is_made(tmp_path: Path, shape: str) -> None:
    src = flown_log(tmp_path / "flown.ulg", parameters=REFUSED[shape])
    dest = tmp_path / "cut" / "px4" / "excerpt.ulg"

    done = cut(src, dest)

    assert done.returncode == 1, done.stderr
    assert done.stderr.splitlines() == [refusal(src, REFUSED[shape])]
    assert not (tmp_path / "cut").exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["flown.ulg"]


@pytest.mark.parametrize("shape", REFUSED)
def test_an_excerpt_already_at_dest_is_left_as_it_was(tmp_path: Path, shape: str) -> None:
    src = flown_log(tmp_path / "flown.ulg", parameters=REFUSED[shape])
    dest = tmp_path / "cut" / "excerpt.ulg"
    dest.parent.mkdir()
    dest.write_bytes(b"an excerpt cut before")

    done = cut(src, dest)

    assert done.returncode == 1, done.stderr
    assert dest.read_bytes() == b"an excerpt cut before"
    assert sorted(p.name for p in dest.parent.iterdir()) == ["excerpt.ulg"]


@pytest.mark.parametrize("shape", KEPT)
def test_an_int32_t_or_float_parameter_is_cut_with_its_type(tmp_path: Path, shape: str) -> None:
    src = flown_log(tmp_path / "flown.ulg", parameters=KEPT[shape])
    dest = tmp_path / "cut" / "excerpt.ulg"

    done = cut(src, dest)

    assert done.returncode == 0, done.stderr
    assert sorted(p.name for p in dest.parent.iterdir()) == ["excerpt.ulg"]
    assert parameter_messages(dest) == parameter_messages(src)
    excerpt, whole = (read_ulog(p, **ATTR) for p in (dest, src))
    assert excerpt.flight_time_s == whole.flight_time_s == 150.0
    assert excerpt.lifetime == whole.lifetime


@pytest.mark.parametrize("shape", REFUSED)
def test_the_flight_reader_reads_such_a_log_as_before(tmp_path: Path, shape: str) -> None:
    plain = read_ulog(flown_log(tmp_path / "plain.ulg"), **ATTR)
    odd = read_ulog(flown_log(tmp_path / "odd.ulg", parameters=REFUSED[shape]), **ATTR)

    assert odd.flight_time_s == plain.flight_time_s == 150.0
    assert replace(odd, log_ref="plain") == plain
