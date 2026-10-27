"""`uasw-census px4-excerpt` refuses, in one line and writing nothing, a ULog it cannot write back.

pyulog reads an information message of a type it does not know (`uint32_t[2] some_key`) without
marking damage and keeps its value as raw bytes, but its writer looks every information type up
to pack the value and raises KeyError. Up to 0.6.5 the cutter opened `--dest` first, so it ended
in that traceback, exit 1, with the header and definitions written before the failure left at
`--dest` (389 bytes for the flown log), where a later step could take it for a fixture (#138).
Each command here runs in a process of its own.
"""

from __future__ import annotations

import struct
import subprocess
import sys
from pathlib import Path

import pytest
from test_flight_px4 import flown_log

from uas_workbench.flight.px4 import read_ulog

CUTTER = [sys.executable, "-m", "uas_workbench.census.cli", "px4-excerpt"]
PAIR = struct.pack("<2I", 1, 2)
UNWRITABLE = {
    "uint32_t[2] some_key": [("uint32_t[2] some_key", PAIR)],
    "uint32_t[2] ver_sw_release": [("uint32_t[2] ver_sw_release", PAIR)],
    "float[3] some_key": [("float[3] some_key", struct.pack("<3f", 1, 2, 3))],
    "a type of no name pyulog knows": [("vector3 some_key", bytes(12))],
    "two of them": [("uint32_t[2] first_key", PAIR), ("int16_t[2] second_key", bytes(4))],
}
WRITABLE = {
    "none": [],
    "uint32_t ver_sw_release": [("uint32_t ver_sw_release", struct.pack("<I", 0x010F02FF))],
    "char[7] ver_sw_release": [("char[7] ver_sw_release", b"v1.15.2")],
    "uint8_t[2] some_key": [("uint8_t[2] some_key", b"\x01\x02")],
}


def refusal(src: Path, typed_keys: list[str]) -> str:
    named = ", ".join(f"'{k}'" for k in typed_keys)
    return (
        f"refused: {src}: it holds an information message pyulog cannot write back ({named}); "
        "nothing is written"
    )


def cut(src: Path, dest: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*CUTTER, "--src", str(src), "--dest", str(dest)],
        capture_output=True,
        text=True,
        timeout=120,
    )


@pytest.mark.parametrize("shape", UNWRITABLE)
def test_it_is_refused_in_one_line_and_nothing_is_made(tmp_path: Path, shape: str) -> None:
    src = flown_log(tmp_path / "flown.ulg", *UNWRITABLE[shape])
    dest = tmp_path / "cut" / "px4" / "excerpt.ulg"

    done = cut(src, dest)

    assert done.returncode == 1, done.stderr
    assert done.stderr.splitlines() == [refusal(src, [k for k, _ in UNWRITABLE[shape]])]
    assert not (tmp_path / "cut").exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["flown.ulg"]


@pytest.mark.parametrize("shape", UNWRITABLE)
def test_an_excerpt_already_at_dest_is_left_as_it_was(tmp_path: Path, shape: str) -> None:
    src = flown_log(tmp_path / "flown.ulg", *UNWRITABLE[shape])
    dest = tmp_path / "cut" / "excerpt.ulg"
    dest.parent.mkdir()
    dest.write_bytes(b"an excerpt cut before")

    done = cut(src, dest)

    assert done.returncode == 1, done.stderr
    assert dest.read_bytes() == b"an excerpt cut before"
    assert sorted(p.name for p in dest.parent.iterdir()) == ["excerpt.ulg"]


@pytest.mark.parametrize("shape", WRITABLE)
def test_a_log_pyulog_can_write_back_is_cut_as_before(tmp_path: Path, shape: str) -> None:
    src = flown_log(tmp_path / "flown.ulg", *WRITABLE[shape])
    dest = tmp_path / "cut" / "excerpt.ulg"

    done = cut(src, dest)

    assert done.returncode == 0, done.stderr
    assert sorted(p.name for p in dest.parent.iterdir()) == ["excerpt.ulg"]
    excerpt, whole = (read_ulog(p, licence="CC0", attribution="synthetic") for p in (dest, src))
    assert excerpt.flight_time_s == whole.flight_time_s == 150.0
