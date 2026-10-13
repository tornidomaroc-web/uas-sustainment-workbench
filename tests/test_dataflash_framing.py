"""A `.bin` whose bytes do not frame as DataFlash messages is refused before pymavlink reads it
(issue #85).

pymavlink's indexer starts at byte 0 and steps from message to message by the length each
format definition gives. Where it finds no `A3 95` sync pair it prints one `bad header` line and
moves on by one byte; at a type no definition names, or a definition shorter than the 3-byte
header, it prints one line and stops indexing, leaving the rest of the file out. It says
nothing about the last 528 bytes of a file of 528 bytes or more, the room it allows for a
block-logging page left partly written. The lines come from compiled C, so only the file
descriptor shows them (capfd and child processes here, never capsys).

So a file with one definition and then megabytes it cannot frame printed a line per byte
before its 422; noise before a log, or damaged messages inside one, printed a line per byte
and then stored a flight read from wherever the indexer found the next sync pair, which can
sit inside another message's data. Each is now refused in one line, naming the offset where
the framing breaks, and nothing reaches pymavlink. Every real log in the fixtures and the ten
ALFA logs they were cut from frame from their first byte to their last (measured, issue #85).
"""

from __future__ import annotations

import json
import os
import random
import re
import struct
import subprocess
import sys
import time
from pathlib import Path

import pytest
from pymavlink import DFReader

from uas_workbench.flight.ardupilot import _check_framing, read_dataflash
from uas_workbench.service.store import Store

ATTR = {"licence": "CC0", "attribution": "synthetic"}
PREFIX = "not a readable PX4 ULog or ArduPilot DataFlash log: "
FIXTURES = Path(__file__).parent / "fixtures"
REAL = sorted((FIXTURES / "alfa").glob("*.bin"))
LOG = REAL[0]
MiB = 1 << 20
# One format definition: the definition of FMT itself, as an autopilot writes it first.
DEFINITION = b"\xa3\x95\x80" + struct.pack(
    "<BB4s16s64s", 0x80, 89, b"FMT", b"BBnNZ", b"Type,Length,Name,Format,Columns"
)
NOT_FRAMED = r"^bytes at offset (\d+) do not frame as a DataFlash message"
UNNAMED = r"^a message at offset (\d+) has type (\d+), which no format definition names; "
TOO_SHORT = r"^the format definition at offset (\d+) gives a length of (\d+) bytes"
# Every such refusal tells the operator what a refused log leaves out of the counts (#86).
NOT_COUNTED = (
    "; nothing from this log is stored, so its flight counts toward no cycle limit, and toward "
    "hours only as far as a later log's lifetime counter covers it"
)


def _noise(n: int) -> bytes:
    """Fixed seed: the same bytes every run, sync pairs among them by chance."""
    return random.Random(85).randbytes(n)


def _damaged(at: int, n: int) -> bytes:
    """The real log with `n` bytes from `at` overwritten by zeros: damaged messages inside it.
    More than 255 bytes, the longest message there can be, so at least one header is hit."""
    log = LOG.read_bytes()
    return log[:at] + bytes(n) + log[at + n :]


def _unnamed_type() -> bytes:
    """The real log with one message whose type no definition names, inserted at the first
    message boundary after 50,000 bytes (found by stepping from the log's own lengths)."""
    log = LOG.read_bytes()
    lengths = {0x80: 89}
    i = 0
    while i < 50_000:
        if log[i + 2] == 0x80:
            lengths[log[i + 3]] = log[i + 4]
        i += lengths[log[i + 2]]
    unused = next(t for t in range(256) if t not in lengths)
    return log[:i] + b"\xa3\x95" + bytes([unused]) + bytes(20) + log[i:]


def _short_definition() -> bytes:
    """One definition whose length is 2, shorter than the header, then the real log."""
    bad = b"\xa3\x95\x80" + struct.pack("<BB4s16s64s", 0xFE, 2, b"BAD", b"B", b"x")
    return DEFINITION + bad + LOG.read_bytes()


# (name, bytes, pattern of the reader's refusal, lowest and highest offset it may name)
REFUSED: list[tuple[str, bytes, str, int, int]] = [
    ("one definition then 4 MiB of zeros", DEFINITION + bytes(4 * MiB), NOT_FRAMED, 89, 89),
    ("one definition then 4 MiB of noise", DEFINITION + _noise(4 * MiB), NOT_FRAMED, 89, 89),
    ("64 KiB of noise then a real log", _noise(64 * 1024) + LOG.read_bytes(), NOT_FRAMED, 0, 0),
    ("600 zero bytes then a real log", bytes(600) + LOG.read_bytes(), NOT_FRAMED, 0, 0),
    ("a real log damaged in the middle", _damaged(50_000, 300), NOT_FRAMED, 49_998, 50_299),
    ("a real log with a type no definition names", _unnamed_type(), UNNAMED, 50_000, 50_255),
    ("a definition shorter than its header", _short_definition(), TOO_SHORT, 89, 89),
]
IDS = [r[0] for r in REFUSED]


def _fd_lines(capfd: pytest.CaptureFixture[str]) -> list[str]:
    return capfd.readouterr().err.splitlines()


# ---- the reader --------------------------------------------------------------------------


@pytest.mark.parametrize(("name", "content", "pattern", "low", "high"), REFUSED, ids=IDS)
def test_a_file_that_does_not_frame_is_refused_before_pymavlink_prints(
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
    name: str,
    content: bytes,
    pattern: str,
    low: int,
    high: int,
) -> None:
    path = tmp_path / "log.bin"
    path.write_bytes(content)
    with pytest.raises(ValueError) as refused:
        read_dataflash(path, **ATTR)
    match = re.match(pattern, str(refused.value))
    assert match, str(refused.value)
    assert low <= int(match.group(1)) <= high, str(refused.value)
    assert str(refused.value).endswith(NOT_COUNTED), str(refused.value)
    assert _fd_lines(capfd) == []


def test_every_real_log_frames_and_reads_with_nothing_on_stderr(
    capfd: pytest.CaptureFixture[str],
) -> None:
    assert len(REAL) == 3
    for log in REAL:
        assert read_dataflash(log, **ATTR).log_ref == log.stem
    assert _fd_lines(capfd) == []


def test_a_real_log_cut_short_still_reads(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """A log that ends inside a message (power lost while it was written) frames to its last
    whole message; pymavlink reads it as before, and prints nothing."""
    log = LOG.read_bytes()
    cut = tmp_path / "cut.bin"
    cut.write_bytes(log[: len(log) // 2 + 37])
    assert read_dataflash(cut, **ATTR).log_span_s > 0
    assert _fd_lines(capfd) == []


def test_bytes_in_pymavlink_s_quiet_tail_are_left_to_it(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """Up to 527 bytes that do not frame at the end of a log are what pymavlink allows for a
    partly written page and says nothing about: the log reads as the same record. One byte
    more is outside that allowance and is refused."""
    log = LOG.read_bytes()
    expected = read_dataflash(LOG, **ATTR)
    for n in (1, 300, 527):
        tail = tmp_path / f"tail{n}" / LOG.name
        tail.parent.mkdir()
        tail.write_bytes(log + b"\xff" * n)
        assert read_dataflash(tail, **ATTR) == expected, n
    assert _fd_lines(capfd) == []
    over = tmp_path / "over" / LOG.name
    over.parent.mkdir()
    over.write_bytes(log + b"\xff" * 528)
    with pytest.raises(ValueError, match=NOT_FRAMED):
        read_dataflash(over, **ATTR)
    assert _fd_lines(capfd) == []


# ---- the command and the API, each in a process of its own ---------------------------------
#
# Lines are counted from the child's stderr written to a file, so a flood costs disk, not this
# process's memory. Times are bounded against the same command refusing a file it has no
# reader for, measured in the same run: what is bounded is the work on the file, not how
# fast a machine starts Python.


def _child_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in ("UASW_WRITE_TOKEN", "UASW_FIXTURES")}
    return env | {"PYTHONWARNINGS": "ignore"}


def _uasw(cwd: Path, *args: str) -> tuple[int, int, list[str], float]:
    """Exit code, stderr line count, the first five stderr lines, seconds taken."""
    err = cwd / "stderr.txt"
    started = time.monotonic()
    with err.open("wb") as sink:
        code = subprocess.run(
            [sys.executable, "-m", "uas_workbench.service.cli", *args],
            cwd=cwd,
            env=_child_env(),
            stdout=subprocess.DEVNULL,
            stderr=sink,
            timeout=600,
        ).returncode
    taken = time.monotonic() - started
    count, first = 0, []
    with err.open("rb") as lines:
        for line in lines:
            count += 1
            if count <= 5:
                first.append(line.decode("utf-8", "replace").rstrip("\r\n"))
    err.unlink()
    return code, count, first, taken


@pytest.fixture(scope="module")
def startup(tmp_path_factory: pytest.TempPathFactory) -> float:
    """Seconds the command takes to refuse a file it has no reader for: start-up and imports."""
    cwd = tmp_path_factory.mktemp("startup")
    (cwd / "notes.txt").write_text("not a log")
    args = ("--db", str(cwd / "s.sqlite"), "ingest", "K", "notes.txt")
    return min(_uasw(cwd, *args)[3] for _ in range(2))


@pytest.mark.parametrize(("name", "content", "pattern", "low", "high"), REFUSED, ids=IDS)
def test_uasw_ingest_refuses_it_in_one_line_in_bounded_time(
    tmp_path: Path,
    startup: float,
    name: str,
    content: bytes,
    pattern: str,
    low: int,
    high: int,
) -> None:
    path = tmp_path / "log.bin"
    path.write_bytes(content)
    db = tmp_path / "new" / "fleet.sqlite"
    code, count, first, taken = _uasw(tmp_path, "--db", str(db), "ingest", "OWN-1", "log.bin")
    assert code == 1
    assert count == 1, (count, first)
    head = f"refused (422): log.bin: {PREFIX}"
    assert first[0].startswith(head), first
    assert re.match(pattern, first[0][len(head) :]), first
    assert first[0].endswith(NOT_COUNTED), first
    assert not db.parent.exists()
    # Twice the start-up and 10 s more; on main the 4 MiB cases print about four million
    # lines and take three to four times the start-up on a fast machine.
    assert taken < 2 * startup + 10, (taken, startup)


def test_uasw_ingest_of_a_real_log_with_a_quiet_tail_stores_it_and_prints_nothing(
    tmp_path: Path,
) -> None:
    (tmp_path / LOG.name).write_bytes(LOG.read_bytes() + b"\xff" * 300)
    db = tmp_path / "fleet.sqlite"
    code, count, first, _ = _uasw(tmp_path, "--db", str(db), "ingest", "OWN-1", LOG.name)
    # The one line is the command's own log of the ingest, as for any log it stores.
    assert (code, count) == (0, 1), first
    assert json.loads(first[0])["message"] == "ingested", first
    store = Store(str(db))
    try:
        assert store.flight_count() == 1
    finally:
        store.close()


API_CHILD = """
import json, sys, tempfile
from pathlib import Path
from fastapi.testclient import TestClient
from uas_workbench.service.app import create_app
from uas_workbench.flight.ardupilot import read_dataflash
from uas_workbench.service.store import Store
uploads = sys.argv[1]
tempfile.tempdir = uploads
store = Store(":memory:")
client = TestClient(create_app(store, write_token=None), client=("127.0.0.1", 1))
answers = []
for path in sys.argv[2:]:
    with open(path, "rb") as f:
        r = client.post(
            "/ingest",
            data={"aircraft_key": "OWN-1"},
            files={"log": ("log.bin", f, "application/octet-stream")},
        )
    try:
        read_dataflash(Path(path), licence="CC0", attribution="synthetic")
        said = None
    except ValueError as exc:
        said = str(exc)
    answers.append([r.status_code, r.json(), said])
print(json.dumps({"answers": answers, "count": store.flight_count(),
                  "leftover": [p.name for p in Path(uploads).iterdir()]}))
store.close()
"""


def test_post_ingest_gives_the_command_s_answer_with_nothing_on_stderr(
    tmp_path: Path, startup: float
) -> None:
    paths = []
    for n, (_, content, _, _, _) in enumerate(REFUSED):
        paths.append(tmp_path / f"{n}.bin")
        paths[-1].write_bytes(content)
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    err = tmp_path / "stderr.txt"
    started = time.monotonic()
    with err.open("wb") as sink:
        done = subprocess.run(
            [sys.executable, "-c", API_CHILD, str(uploads), *map(str, paths)],
            env=_child_env(),
            stdout=subprocess.PIPE,
            stderr=sink,
            timeout=600,
        )
    taken = time.monotonic() - started
    foreign = 0
    with err.open("rb") as lines:
        for line in lines:
            foreign += not line.startswith(b"{")
    assert done.returncode == 0
    assert foreign == 0, f"{foreign} lines on stderr that are not the service's log"
    result = json.loads(done.stdout)
    assert result["count"] == 0 and result["leftover"] == []
    for (name, _, pattern, low, high), (status, body, said) in zip(
        REFUSED, result["answers"], strict=True
    ):
        assert status == 422, (name, body)
        assert body["detail"].startswith(PREFIX), (name, body)
        detail = body["detail"][len(PREFIX) :]
        match = re.match(pattern, detail)
        assert match and low <= int(match.group(1)) <= high, (name, detail)
        assert detail.endswith(NOT_COUNTED), (name, detail)
        # The reader's own words, which the command's line carries after the same prefix.
        assert said == detail, name
    assert taken < 3 * startup + 20, (taken, startup)


# ---- the rule is the installed pymavlink's -------------------------------------------------
#
# The check copies the indexer of pymavlink 2.4.50 (dfindexer.c, scan_offsets), and the
# dependency allows any 2.x. So the copy is compared with the pymavlink installed, on damaged
# copies of a real log: the check must refuse exactly the copies on which pymavlink prints
# anything. A release that frames differently fails here instead of drifting unseen.


def _pymavlink_speaks(path: Path, capfd: pytest.CaptureFixture[str]) -> bool:
    capfd.readouterr()
    reader = DFReader.DFReader_binary(str(path), zero_time_base=True)
    try:
        while reader.recv_msg() is not None:
            pass
    finally:
        reader.close()
    return capfd.readouterr().err != ""


def _midway(log: bytes) -> bytes:
    """The log with one type's definition moved from the head to just before that type's first
    message, as the ALFA logs define some types partway through."""
    lengths: dict[int, int] = {0x80: 89}
    defined: dict[int, int] = {}
    first: dict[int, int] = {}
    i = 0
    while i + 3 < len(log):
        if log[i + 2] == 0x80:
            lengths[log[i + 3]] = log[i + 4]
            defined[log[i + 3]] = i
        first.setdefault(log[i + 2], i)
        i += lengths[log[i + 2]]
    kind = max((k for k in defined if k != 0x80 and k in first), key=lambda k: first[k])
    at = defined[kind]
    rest = log[:at] + log[at + 89 :]
    to = first[kind] - 89
    return rest[:to] + log[at : at + 89] + rest[to:]


def _whole(log: bytes, near: int) -> bytes:
    """The log cut at its last message boundary at or before `near`: bytes added after it begin
    exactly where the indexer looks next, so the quiet tail is compared to the byte."""
    lengths, i = {0x80: 89}, 0
    while True:
        if log[i + 2] == 0x80:
            lengths[log[i + 3]] = log[i + 4]
        if i + lengths[log[i + 2]] > near:
            return log[:i]
        i += lengths[log[i + 2]]


def _header_only() -> bytes:
    """A definition of the shortest length the indexer takes, 3, with no fields, and one such
    message: pymavlink reads it without a word."""
    three = b"\xa3\x95\x80" + struct.pack("<BB4s16s64s", 0xFE, 3, b"HDR", b"", b"")
    return DEFINITION + three + b"\xa3\x95\xfe" + LOG.read_bytes()


def _copies() -> list[bytes]:
    """Damage the indexer sees. A definition whose length disagrees with its own format string
    frames, and is pymavlink's parser's to report, not the indexer's (LIMITS.md)."""
    log = _whole(LOG.read_bytes(), 60_000)
    midway = _whole(_midway(LOG.read_bytes()), 60_000)
    rng = random.Random(86)
    out = [log, midway, bytes(3) + log, _header_only()[:60_000]]
    out += [base + b"\xff" * n for base in (log, midway) for n in (526, 527, 528, 529)]
    out += [_short_definition()[:60_000], _unnamed_type()[:60_000]]
    for base in (log, midway):
        for _ in range(12):
            at, n = rng.randrange(len(base)), rng.choice((1, 2, 7, 300))
            choice = rng.randrange(4)
            if choice == 0:
                out.append(base[:at] + rng.randbytes(n) + base[at + n :])
            elif choice == 1:
                out.append(base[:at] + rng.randbytes(n) + base[at:])
            elif choice == 2:
                out.append(base[:at] + base[at + n :])
            else:
                out.append(base[: len(base) - rng.randrange(400, 660)] + bytes(n) + base[-300:])
    return out


def test_the_check_refuses_exactly_where_the_installed_pymavlink_speaks(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    refused, spoke = [], []
    for n, content in enumerate(_copies()):
        path = tmp_path / f"{n}.bin"
        path.write_bytes(content)
        try:
            _check_framing(path)
            refused.append(False)
        except ValueError:
            refused.append(True)
        spoke.append(_pymavlink_speaks(path, capfd))
    assert refused == spoke, [
        n for n, (r, s) in enumerate(zip(refused, spoke, strict=True)) if r != s
    ]
    # Both answers occur, so the comparison is not vacuous.
    assert 5 < sum(refused) < len(refused) - 5, refused
