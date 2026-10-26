"""Every read of a PX4 ULog stops where pyulog marks it corrupt, and the log is refused with the
damaged-log sentence: the census, the excerpt cutter and the flight reader's look for data of
any topic, as the flight reader's first read already did (issue #132).

On 0.6.4 the census and the excerpt cutter read through `ulog_declared.read` without stopping at
damage. 64 KiB of 0xFF bytes among a log's definitions then never finish: near the end of the
file pyulog reads fewer bytes than a damaged header claims, seeks back by all it claims and reads
the same headers again (tests/test_ulog_declared_size.py). Measured on 0.6.4 (83fb3da, Windows,
Python 3.12, pyulog 1.3.0), `probe_ulog` and `excerpt_ulog` were each still reading at 30 s, and
a census run stops there until it is stopped by hand. Each such read here runs in a process of
its own under a time cap (tests/capped.py), so 0.6.4 fails by the cap.

The flight reader reads a log again whole when it holds no data of the topics it reads, to tell
a log of other topics from one with no data at all. A data message shorter than its definition
is damage pyulog sees only for a topic it is asked to read, so that second read can find damage
the first did not, and 0.6.4 read on through it and stored the log.
"""

from __future__ import annotations

import json
import re
import struct
from dataclasses import replace
from pathlib import Path

import pytest
from capped import run_capped
from test_flight_px4 import ATTR
from test_px4 import flown_log
from test_ulog_damaged import DAMAGED
from test_ulog_declared_size import flood

from builders.ulog import ULogBuilder
from uas_workbench.census.excerpt import excerpt_ulog
from uas_workbench.census.px4 import probe_ulog
from uas_workbench.flight.px4 import read_ulog

MIB, SECONDS = 1024, 20
SRC = Path(__file__).parents[1] / "src" / "uas_workbench"

CALL = """
import json, sys
from pathlib import Path
from uas_workbench.census.excerpt import excerpt_ulog
from uas_workbench.census.px4 import probe_ulog
calls = {"probe": lambda path: probe_ulog(path, "fixed_wing"), "excerpt": excerpt_ulog}
try:
    calls[sys.argv[1]](Path(sys.argv[2]))
    said = "read"
except Exception as exc:
    said = [isinstance(exc, ValueError), str(exc)]
print(json.dumps({"said": said, "optimize": sys.flags.optimize}))
"""

# The body of the `uasw-census` command, so the run is the command's own, workers and all.
COMMAND = """
import json, os, sys
os.chdir(sys.argv[1])
from uas_workbench.census.cli import main
try:
    main(sys.argv[2:])
    said = 0
except SystemExit as stop:
    said = stop.code
print(json.dumps(said))
"""


@pytest.fixture
def flooded(tmp_path: Path) -> Path:
    """The shape of #132: one definition, no subscription, then 64 KiB of 0xFF (65,619 bytes)."""
    log = tmp_path / "px4" / "flood.ulg"
    log.parent.mkdir()
    flood(log, in_definitions=True, mib=0)
    log.write_bytes(log.read_bytes() + b"\xff" * (64 << 10))
    assert log.stat().st_size == 65_619
    return log


@pytest.mark.parametrize("optimize", [False, True], ids=["plain", "python -O"])
@pytest.mark.parametrize("call", ["probe", "excerpt"])
def test_the_census_readers_refuse_a_log_pyulog_marks_corrupt(
    call: str, optimize: bool, flooded: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal is the flight reader's sentence, raised as a ValueError, whether or not
    Python runs with -O (an `assert` in its place would be skipped there)."""
    if optimize:
        monkeypatch.setenv("PYTHONOPTIMIZE", "1")
    else:
        monkeypatch.delenv("PYTHONOPTIMIZE", raising=False)
    outcome = run_capped(CALL, mib=MIB, seconds=SECONDS, args=[call, str(flooded)])
    assert outcome.said == {"said": [True, DAMAGED], "optimize": int(optimize)}, outcome


def test_a_census_run_records_the_log_as_unreadable_and_counts_the_rest(flooded: Path) -> None:
    px4 = flooded.parent
    flown_log(px4 / "flown.ulg")
    groups = {"fixed_wing": [{"log_id": "flood"}, {"log_id": "flown"}]}
    (px4 / "selected.json").write_text(json.dumps(groups), encoding="utf-8")
    (px4.parent / "alfa").mkdir()
    args = ["run", "--px4", "px4", "--alfa", "alfa", "--out", "out", "--jobs", "2"]

    outcome = run_capped(COMMAND, mib=MIB, seconds=SECONDS + 10, args=[str(px4.parent), *args])

    assert outcome.said == 0, outcome
    lines = (px4.parent / "data" / "local" / "census_per_log.jsonl").read_text("utf-8")
    errors = {r["log_ref"]: r["error"] for r in map(json.loads, lines.splitlines())}
    assert errors == {"flood": f"DamagedLog: {DAMAGED}", "flown": None}
    summary = (px4.parent / "out" / "census.md").read_text("utf-8")
    assert "2 logs, 1 unreadable, 1 airborne" in summary


def test_px4_excerpt_refuses_it_in_one_line_and_writes_nothing(flooded: Path) -> None:
    dest = flooded.parent / "cut" / "excerpt.ulg"
    args = ["px4-excerpt", "--src", str(flooded), "--dest", str(dest)]

    outcome = run_capped(COMMAND, mib=MIB, seconds=SECONDS, args=[str(flooded.parent), *args])

    assert outcome.said == 1, outcome
    assert outcome.stderr.splitlines() == [f"refused: {flooded}: {DAMAGED}"]
    assert not dest.exists()


@pytest.mark.parametrize("zeros", [3, 512, 70_000])
def test_the_census_readers_read_a_log_ending_in_zeros_as_the_flight_reader_does(
    zeros: int, tmp_path: Path
) -> None:
    """As the log cut where the zeros begin, the census counting what it counted before. A
    power cut can end a log in zero bytes, which pyulog reads as damage; stopping there and
    refusing would have the census count such a log unreadable where `uasw ingest` stores it."""
    sound = flown_log(tmp_path / "sound.ulg")
    tailed = tmp_path / "tailed.ulg"
    tailed.write_bytes(sound.read_bytes() + bytes(zeros))

    assert probe_ulog(tailed, "fixed_wing") == replace(
        probe_ulog(sound, "fixed_wing"), log_ref="tailed"
    )
    for log in (sound, tailed):
        excerpt_ulog(log).write_ulog(str(log.with_suffix(".cut")))
    assert tailed.with_suffix(".cut").read_bytes() == sound.with_suffix(".cut").read_bytes()


def other_topic(path: Path, *, sound_data: bool) -> Path:
    """A log of one topic the flight reader does not read, holding one data message two bytes
    short of its definition, with or without sound messages around it."""
    log = ULogBuilder().format("t", ["uint64_t timestamp", "float x"])
    topic = log.subscribe("t")
    if sound_data:
        log.data(topic, {"timestamp": 2_000_000, "x": 1.0})
    log._message(log._data, "D", struct.pack("<HQ", topic, 3_000_000) + bytes(2))
    if sound_data:
        log.data(topic, {"timestamp": 4_000_000, "x": 2.0})
    return log.write(path)


@pytest.mark.parametrize("sound_data", [True, False], ids=["with-sound-data", "damage-only"])
def test_damage_found_looking_for_data_of_any_topic_refuses_the_log(
    sound_data: bool, tmp_path: Path
) -> None:
    """0.6.4 stored the first (a flight of no known value), and refused the second for holding
    no recorded data."""
    log = other_topic(tmp_path / "other.ulg", sound_data=sound_data)
    with pytest.raises(ValueError) as refused:
        read_ulog(log, **ATTR)
    assert str(refused.value) == DAMAGED


def test_only_the_bounded_reader_hands_a_log_to_pyulog() -> None:
    """Every read goes through `ulog_declared.read`, which stops where pyulog marks damage."""
    direct = re.compile(r"\bULog\(|\bULog\.__init__|\bpyulog\.ULog\b|from pyulog\.core\b")
    found = [
        f"{py.relative_to(SRC)}:{n}"
        for py in sorted(SRC.rglob("*.py"))
        if py.name != "ulog_declared.py"
        for n, line in enumerate(py.read_text("utf-8").splitlines(), 1)
        if direct.search(line)
    ]
    assert found == []
