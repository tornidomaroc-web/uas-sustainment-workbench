"""A `.bin` that holds no DataFlash format definition is refused before pymavlink opens it.

pymavlink's indexer prints one `bad header` line on stderr for every byte it cannot frame. In
the wheels published for Linux and Windows the indexer is compiled C and prints with fprintf,
so the lines never pass through Python's `sys.stderr`: a redirect there sees nothing, and only
the file descriptor shows them (capfd here, never capsys). On 0.5.1 a 16-byte junk file wrote
13 lines and a 16 MiB one 16.7 million, through POST /ingest as through `uasw ingest`.

The refusal, its wording, what is stored (nothing) and the record of every readable log are the
0.5.1 ones; the junk simply never reaches pymavlink. A log that begins behind noise still parses.
"""

from __future__ import annotations

import json
import os
import random
import struct
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from builders.dataflash import DataFlashBuilder
from uas_workbench.flight.ardupilot import read_dataflash
from uas_workbench.service.store import Store

ATTR = {"licence": "CC0", "attribution": "synthetic"}
REFUSAL = "not a readable PX4 ULog or ArduPilot DataFlash log: no DataFlash messages found"
KEY = "junk-bin-01"
MiB = 1 << 20
FMT_SYNC = b"\xa3\x95\x80"


def _text(n: int) -> bytes:
    return (b"not a flight log\n" * (n // 17 + 1))[:n]


def _every_byte(n: int) -> bytes:
    return (bytes(range(256)) * (n // 256 + 1))[:n]


def _random(n: int) -> bytes:
    """Fixed seed: the same bytes every run, sync pairs among them by chance."""
    return random.Random(2026).randbytes(n)


def _sync_then_zeros(n: int) -> bytes:
    """A format-definition header followed by nothing a definition could be made of."""
    return (FMT_SYNC + bytes(n))[:n]


KINDS: dict[str, Callable[[int], bytes]] = {
    "zeros": bytes,
    "text": _text,
    "every-byte": _every_byte,
    "random": _random,
    "sync-then-zeros": _sync_then_zeros,
}
SIZES = [16, 64 * 1024, MiB, 16 * MiB]


def _size_id(n: int) -> str:
    return f"{n}B" if n < MiB else f"{n // MiB}MiB"


def stderr_lines(err: str) -> int:
    return err.count("\n")


def bad_header_lines(err: str) -> int:
    return sum(1 for line in err.splitlines() if line.startswith("bad header"))


def _formats() -> DataFlashBuilder:
    return (
        DataFlashBuilder()
        .define("PARM", "QNf", "TimeUS,Name,Value")
        .define("STAT", "QBBBBBBBB", "TimeUS,isFlying,isFlyProb,Armed,Safety,Crash,Still,Stage,Hit")
        .define("ARM", "QBH", "TimeUS,ArmState,ArmChecks")
    )


def small_log() -> DataFlashBuilder:
    """One armed flight of 100 s, a parameter and a lifetime counter."""
    b = _formats()
    s = 1_000_000
    b.msg("PARM", 10 * s, "STAT_FLTTIME", 3600.0)
    b.msg("ARM", 20 * s, 1, 0)
    for t in range(20, 200, 10):
        b.msg("STAT", t * s, int(30 <= t < 130), 0, 1, 0, 0, 0, 0, 0)
    b.msg("ARM", 200 * s, 0, 0)
    return b


# ---- the reader: nothing reaches stderr, whatever the size -----------------------------


@pytest.mark.parametrize("size", SIZES, ids=_size_id)
@pytest.mark.parametrize("kind", KINDS)
def test_junk_of_any_size_is_refused_with_nothing_on_stderr(
    tmp_path: Path, capfd: pytest.CaptureFixture[str], kind: str, size: int
) -> None:
    junk = tmp_path / "junk.bin"
    junk.write_bytes(KINDS[kind](size))
    with pytest.raises(ValueError, match=r"^no DataFlash messages found$"):
        read_dataflash(junk, **ATTR)
    err = capfd.readouterr().err
    assert stderr_lines(err) == 0, f"{stderr_lines(err)} lines on stderr for {size} B of {kind}"


def test_a_definition_header_without_a_whole_definition_is_no_message(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """The sync bytes and a definition cut short: 0.5.1 already answered this; the answer
    and the silence stay."""
    cut = tmp_path / "cut.bin"
    cut.write_bytes(FMT_SYNC + struct.pack("<BB4s", 0x81, 31, b"PARM"))
    with pytest.raises(ValueError, match=r"^no DataFlash messages found$"):
        read_dataflash(cut, **ATTR)
    assert capfd.readouterr().err == ""


def test_a_definition_pymavlink_could_not_decode_is_no_definition(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """A file whose only definition names a format character pymavlink does not know, or no
    name, is not a log it could read a message from; it is refused before the flood, with the
    reader's own wording, where 0.5.1 let pymavlink print per byte and then raise its own."""
    unknown_char = struct.pack("<BB4s16s64s", 0x81, 15, b"ATT", b"Qxf", b"TimeUS,Roll,Pitch")
    no_name = struct.pack("<BB4s16s64s", 0x81, 15, b"", b"Qff", b"TimeUS,Roll,Pitch")
    for name, definition in [("unknown-char", unknown_char), ("no-name", no_name)]:
        junk = tmp_path / f"{name}.bin"
        junk.write_bytes(FMT_SYNC + definition + bytes(4096))
        with pytest.raises(ValueError, match=r"^no DataFlash messages found$"):
            read_dataflash(junk, **ATTR)
        err = capfd.readouterr().err
        assert stderr_lines(err) == 0, (name, stderr_lines(err))


# ---- what the check must not touch ---------------------------------------------------


def test_a_log_behind_leading_noise_still_parses_to_the_same_record(tmp_path: Path) -> None:
    """pymavlink skips what it cannot frame before the first definition, and so does the
    check: a log with noise before it is the same record as the log alone. (pymavlink still
    prints one line per noise byte, as on 0.5.1; LIMITS.md says so.)"""
    (tmp_path / "clean").mkdir()
    (tmp_path / "noisy").mkdir()
    clean = small_log().write(tmp_path / "clean" / "log.bin")
    expected = read_dataflash(clean, **ATTR)
    assert expected.flight_time_s == pytest.approx(100.0)
    noisy = tmp_path / "noisy" / "log.bin"
    noisy.write_bytes(bytes(600) + clean.read_bytes())
    assert read_dataflash(noisy, **ATTR) == expected


def test_a_false_definition_before_a_log_is_still_pymavlink_s_call(tmp_path: Path) -> None:
    """A definition header that defines nothing, then a whole log: the check finds the log's
    own definitions and hands the file to pymavlink, whose indexer stops at the false one and
    refuses the file as it did on 0.5.1. The check never answers for a file pymavlink would
    read any part of."""
    false_first = tmp_path / "false_first.bin"
    false_first.write_bytes(
        FMT_SYNC + bytes(600) + small_log().write(tmp_path / "log.bin").read_bytes()
    )
    with pytest.raises(ValueError, match=r"^holds format definitions but no recorded data$"):
        read_dataflash(false_first, **ATTR)


def test_definitions_without_data_are_still_refused_in_the_same_words(tmp_path: Path) -> None:
    """The check lets a plausible definition through to pymavlink; what the reader says about
    definitions with no data is unchanged."""
    with pytest.raises(ValueError, match=r"^holds format definitions but no recorded data$"):
        read_dataflash(_formats().write(tmp_path / "defs.bin"), **ATTR)


# ---- through the API and the command, where the flood was reachable ---------------------
#
# Each runs in its own process: its stderr is the file descriptor, where the C indexer prints,
# and no logging handler of the service is bound to a capture stream of this process.

API_CHILD = """
import json, sys, tempfile
from pathlib import Path
from fastapi.testclient import TestClient
from uas_workbench.service.app import create_app
from uas_workbench.service.store import Store
uploads, junk, key = sys.argv[1:4]
tempfile.tempdir = uploads
store = Store(":memory:")
client = TestClient(create_app(store, write_token=None), client=("127.0.0.1", 1))
with open(junk, "rb") as f:
    response = client.post(
        "/ingest",
        data={"aircraft_key": key},
        files={"log": ("junk.bin", f, "application/octet-stream")},
    )
print(json.dumps({
    "status": response.status_code,
    "body": response.json(),
    "flights": client.get(f"/aircraft/{key}/flights").status_code,
    "count": store.flight_count(),
    "leftover": [p.name for p in Path(uploads).iterdir()],
}))
"""


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *args],
        capture_output=True,
        text=True,
        env=os.environ | {"PYTHONWARNINGS": "ignore"},
        timeout=600,
    )


def test_post_ingest_of_a_large_junk_bin_writes_only_the_service_log_lines(
    tmp_path: Path,
) -> None:
    junk = tmp_path / "junk.bin"
    junk.write_bytes(_random(4 * MiB))
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    done = _run("-c", API_CHILD, str(uploads), str(junk), KEY)
    assert done.returncode == 0, done.stderr[-2000:]
    result = json.loads(done.stdout)
    assert result["status"] == 422 and result["body"] == {"detail": REFUSAL}
    assert result["flights"] == 404 and result["count"] == 0 and result["leftover"] == []
    err = done.stderr
    foreign = [line for line in err.splitlines() if not line.startswith("{")]
    assert foreign == [], f"{stderr_lines(err)} lines on stderr, {bad_header_lines(err)} bad header"


def test_uasw_ingest_of_a_large_junk_bin_prints_one_refusal_line_and_exits_1(
    tmp_path: Path,
) -> None:
    junk = tmp_path / "junk.bin"
    junk.write_bytes(bytes(4 * MiB))
    db = tmp_path / "fleet.sqlite"
    done = _run("-m", "uas_workbench.service.cli", "--db", str(db), "ingest", KEY, str(junk))
    assert done.returncode == 1
    store = Store(str(db))
    assert store.flight_count() == 0 and store.get_aircraft(KEY) is None
    err = done.stderr
    assert err.splitlines() == [f"refused (422): {junk}: {REFUSAL}"], (
        f"{stderr_lines(err)} lines on stderr, {bad_header_lines(err)} bad header"
    )
