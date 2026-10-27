"""A `ver_sw_release` PX4 would not write is read as absent, and the flight with it (#136).

PX4 writes `ver_sw_release` as a `uint32_t` information message, and the workbench reads it only
for the firmware label. pyulog keeps another type's value as it reads it: text, a float, a bool,
a number past 32 bits, or raw bytes for a type it does not know. Up to 0.6.5 the label was taken
from whatever came: text, a float or bytes raised Python's TypeError in pyulog's version shift,
so `uasw ingest` refused the whole log with that text and the census counted it unreadable; a
bool or a number past 32 bits gave a version no firmware has ("v0.0.0 (dev)", "v255.255.255").
Each of those logs is now read as the same log without the message: firmware unknown, every
other value the same.
"""

from __future__ import annotations

import json
import struct
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from test_flight_px4 import ATTR, flown_log

from uas_workbench.census.px4 import probe_ulog
from uas_workbench.flight.px4 import read_ulog
from uas_workbench.service.store import Store

KEY = "firmware-field-01"
UASW = [sys.executable, "-m", "uas_workbench.service.cli"]
VERSION = 0x010F02FF  # v1.15.2, a release build

# Read as PX4 writes it, or as an integer PX4 could have written: the label stays.
KEPT = {
    "uint32_t": (("uint32_t ver_sw_release", struct.pack("<I", VERSION)), "v1.15.2"),
    "int32_t": (("int32_t ver_sw_release", struct.pack("<i", VERSION)), "v1.15.2"),
}
# Anything else is read as no `ver_sw_release` at all.
ABSENT = {
    "char[7]": ("char[7] ver_sw_release", b"v1.15.2"),
    "float": ("float ver_sw_release", struct.pack("<f", 1.0)),
    "double": ("double ver_sw_release", struct.pack("<d", 1.0)),
    "uint32_t[2]": ("uint32_t[2] ver_sw_release", struct.pack("<2I", 1, 2)),
    "bool": ("bool ver_sw_release", b"\x01"),
    "int64_t -1": ("int64_t ver_sw_release", struct.pack("<q", -1)),
    "uint64_t past 32 bits": ("uint64_t ver_sw_release", struct.pack("<Q", (1 << 32) | VERSION)),
}


def log_with(folder: Path, *info: tuple[str, bytes]) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    return flown_log(folder / "flown.ulg", *info)


@pytest.mark.parametrize("shape", ABSENT)
def test_the_flight_reader_reads_it_as_a_log_without_the_field(tmp_path: Path, shape: str) -> None:
    plain = read_ulog(log_with(tmp_path / "plain"), **ATTR)
    odd = read_ulog(log_with(tmp_path / "odd", ABSENT[shape]), **ATTR)

    assert plain.firmware == "unknown"
    assert odd == plain


@pytest.mark.parametrize("shape", ABSENT)
def test_the_census_probe_reads_it_as_a_log_without_the_field(tmp_path: Path, shape: str) -> None:
    plain = probe_ulog(log_with(tmp_path / "plain"), "fixed_wing")
    odd = probe_ulog(log_with(tmp_path / "odd", ABSENT[shape]), "fixed_wing")

    assert plain.error is None and plain.firmware == "unknown"
    assert odd == plain


@pytest.mark.parametrize("shape", KEPT)
def test_a_field_px4_could_have_written_keeps_its_label(tmp_path: Path, shape: str) -> None:
    info, label = KEPT[shape]
    log = log_with(tmp_path, info)
    plain = read_ulog(log_with(tmp_path / "plain"), **ATTR)

    record = read_ulog(log, **ATTR)
    assert record.firmware == label
    assert record == replace(plain, firmware=label)
    assert probe_ulog(log, "fixed_wing").firmware == label


def ingest(db: Path, log: Path) -> subprocess.CompletedProcess[str]:
    """`uasw ingest` in a process of its own."""
    return subprocess.run(
        [*UASW, "--db", str(db), "ingest", KEY, str(log)],
        capture_output=True,
        text=True,
        timeout=120,
    )


@pytest.mark.parametrize("shape", ABSENT)
def test_uasw_ingest_stores_the_flight_firmware_unknown(tmp_path: Path, shape: str) -> None:
    plain_db, odd_db = tmp_path / "plain.sqlite", tmp_path / "odd.sqlite"
    assert ingest(plain_db, log_with(tmp_path / "plain")).returncode == 0

    done = ingest(odd_db, log_with(tmp_path / "odd", ABSENT[shape]))

    assert done.returncode == 0, done.stderr
    assert "refused" not in done.stderr, done.stderr
    stored = Store(str(odd_db)).flights(KEY)
    assert [f.firmware for f in stored] == ["unknown"]
    assert [f.flight_time_s for f in stored] == [150.0]
    assert stored == Store(str(plain_db)).flights(KEY)


def test_a_census_run_counts_each_and_names_the_firmware_it_read(tmp_path: Path) -> None:
    """One `uasw-census run`, in a process of its own, over a log of every shape."""
    px4 = tmp_path / "px4"
    shapes = {"absent": None} | {f"kept {s}": v[0] for s, v in KEPT.items()} | ABSENT
    names = {shape: f"log{i:02d}" for i, shape in enumerate(shapes)}
    for shape, info in shapes.items():
        log_with(px4 / "made", *([info] if info else [])).rename(px4 / f"{names[shape]}.ulg")
    groups = {"fixed_wing": [{"log_id": name} for name in names.values()]}
    (px4 / "selected.json").write_text(json.dumps(groups), encoding="utf-8")
    (tmp_path / "alfa").mkdir()
    args = ["run", "--px4", "px4", "--alfa", "alfa", "--out", "out", "--jobs", "1"]

    done = subprocess.run(
        [sys.executable, "-m", "uas_workbench.census.cli", *args],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=300,
    )

    assert done.returncode == 0, done.stderr
    lines = (tmp_path / "data" / "local" / "census_per_log.jsonl").read_text("utf-8")
    read = {r["log_ref"]: (r["error"], r["firmware"]) for r in map(json.loads, lines.splitlines())}
    labels = {f"kept {s}": v[1] for s, v in KEPT.items()}
    assert read == {names[s]: (None, labels.get(s, "unknown")) for s in shapes}
    summary = (tmp_path / "out" / "census.md").read_text("utf-8")
    assert f"{len(shapes)} logs, 0 unreadable, {len(shapes)} airborne" in summary
