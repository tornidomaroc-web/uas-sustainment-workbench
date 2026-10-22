"""A ULog damaged inside is refused, as a damaged DataFlash log is.

pyulog skips what it cannot parse and sets `file_corruption`: a data message whose framing
breaks, a definition or a subscription (PX4/pyulog#128, in 1.3.0, the lowest version the
package allows; 1.2.4 raised on those two). The flight read from what is left can lose a
topic, the aircraft key or most of its span, and a missing topic then reads as one the log
never held. Measured on 2026-10-21 over 448 damaged copies of two logs: before the check,
1.3.0 gave a flight for 62 that pyulog called corrupt; none of the 45 real logs in
tests/fixtures and data/raw is called corrupt.
"""

from __future__ import annotations

import struct
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_flight_px4 import ATTR, flown_log

from uas_workbench.flight.px4 import read_ulog
from uas_workbench.service.app import create_app
from uas_workbench.service.cli import main
from uas_workbench.service.store import Store

KEY = "damaged-ulog-01"
FIXTURES = Path(__file__).parent / "fixtures"
PX4_FIXTURE = FIXTURES / "px4" / "flight_review_board_validation_2026-06-12_excerpt.ulg"
PREFIX = "not a readable PX4 ULog or ArduPilot DataFlash log: "
DAMAGED = (
    "pyulog found messages it could not parse and skipped them; nothing from this log is "
    "stored, so its flight counts toward no cycle limit, and toward hours only as far as a "
    "later log's lifetime counter covers it"
)


def messages(log: bytes) -> list[tuple[int, str, bytes]]:
    """Offset, kind and body of every message after the 16-byte header, as written."""
    found: list[tuple[int, str, bytes]] = []
    at = 16
    while at + 3 <= len(log):
        size, kind = struct.unpack_from("<HB", log, at)
        found.append((at, chr(kind), log[at + 3 : at + 3 + size]))
        at += 3 + size
    return found


def damaged(path: Path, where: str) -> Path:
    """The flown log of tests/test_flight_px4.py with one byte inverted.

    `framing`: the size of the first battery_status data message; `definition`: a letter of
    battery_status's name in its format definition; `subscription`: a letter of
    vehicle_land_detected's name in its subscription."""
    log = bytearray(flown_log(path).read_bytes())
    found = messages(bytes(log))
    battery_id = next(b[1:3] for _, kind, b in found if kind == "A" and b[3:] == b"battery_status")
    at = {
        "framing": next(o for o, kind, b in found if kind == "D" and b[:2] == battery_id),
        "definition": next(
            o + 3 + 3 for o, kind, b in found if kind == "F" and b.startswith(b"battery_status:")
        ),
        "subscription": next(
            o + 3 + 3 + 11
            for o, kind, b in found
            if kind == "A" and b[3:] == b"vehicle_land_detected"
        ),
    }[where]
    log[at] ^= 0xFF
    path.write_bytes(bytes(log))
    return path


WHERE = ["framing", "definition", "subscription"]


@pytest.mark.filterwarnings("error::ResourceWarning")
@pytest.mark.filterwarnings("error::pytest.PytestUnraisableExceptionWarning")
def test_a_log_with_a_broken_message_is_refused_with_no_file_open(tmp_path: Path) -> None:
    log = damaged(tmp_path / "framing.ulg", "framing")
    with pytest.raises(ValueError) as refused:
        read_ulog(log, **ATTR)
    assert str(refused.value) == DAMAGED


@pytest.mark.parametrize("where", WHERE)
def test_post_ingest_refuses_a_damaged_log_and_stores_nothing(
    where: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = damaged(tmp_path / f"{where}.ulg", where).read_bytes()
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(uploads))
    store = Store(":memory:")
    client = TestClient(create_app(store, write_token=None), client=("127.0.0.1", 1))
    response = client.post(
        "/ingest",
        data={"aircraft_key": KEY},
        files={"log": (f"{where}.ulg", log, "application/octet-stream")},
    )
    assert response.status_code == 422, response.text
    assert response.json() == {"detail": PREFIX + DAMAGED}
    assert store.get_aircraft(KEY) is None
    assert store.flight_count() == 0
    assert list(uploads.iterdir()) == []


@pytest.mark.parametrize("where", WHERE)
def test_uasw_ingest_refuses_a_damaged_log_and_reads_the_next(
    where: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    log = damaged(tmp_path / f"{where}.ulg", where)
    whole = flown_log(tmp_path / "whole.ulg")
    db = tmp_path / "fleet.sqlite"
    with pytest.raises(SystemExit) as exit_:
        main(["--db", str(db), "ingest", KEY, str(log), str(whole)])
    assert exit_.value.code == 1
    err = capsys.readouterr().err
    assert [line for line in err.splitlines() if line.startswith("refused")] == [
        f"refused (422): {log}: {PREFIX}{DAMAGED}"
    ]
    assert "Traceback" not in err
    store = Store(str(db))
    assert [f.log_ref for f in store.flights(KEY)] == ["whole"]


@pytest.mark.filterwarnings("error::ResourceWarning")
@pytest.mark.filterwarnings("error::pytest.PytestUnraisableExceptionWarning")
@pytest.mark.parametrize("zeros", [3, 512, 4096, 70_000])
@pytest.mark.parametrize("source", ["flown", "fixture"])
def test_a_log_ending_in_zero_bytes_reads_as_the_log_without_them(
    source: str, zeros: int, tmp_path: Path
) -> None:
    """A log still being written when the power went can end in zero bytes: no message, but
    pyulog reads a header of zeros there and marks the file corrupt. It reads as the log cut
    where they begin. That is the log without them when its last byte is not zero, as the
    flown log's. The PX4 excerpt's last message ends in a zero byte, so with zeros after it
    the file is byte for byte the excerpt cut one byte short of its end and followed by
    zeros: that message may have been cut, and is dropped, as a cut one is."""
    whole = flown_log(tmp_path / "whole.ulg") if source == "flown" else PX4_FIXTURE
    written = whole.read_bytes().rstrip(b"\x00")
    assert len(written) == len(whole.read_bytes()) - (source == "fixture")
    cut = tmp_path / "cut.ulg"
    cut.write_bytes(written)
    padded = tmp_path / "padded.ulg"
    padded.write_bytes(whole.read_bytes() + bytes(zeros))
    assert padded.read_bytes() == written + bytes(zeros + (source == "fixture"))
    assert replace(read_ulog(padded, **ATTR), log_ref=cut.stem) == read_ulog(cut, **ATTR)


def cut_in(log: bytes, from_end: int, into: int) -> bytes:
    """`log` cut `into` bytes after the start of its `from_end`-th message from the end,
    counted from the first byte of that message's 3-byte header."""
    at = messages(log)[-from_end][0]
    return log[: at + into]


# Each case is stored, with 512 zero bytes after it, with a record that differs from the
# same cut without them on main (5bf52bc) on pyulog 1.2.4 and 1.3.0 (#122): the message that
# runs into the zeros was read whole, its missing bytes taken from them. `into` is counted
# from the message's header; the field named is one that differed.
CUT_THEN_ZEROS = [
    # A battery_status sample cut after 11 of its 22 body bytes: 475.0 mAh, -50.0 with zeros.
    pytest.param("flown", 12, 3 + 11, "battery_mah", id="battery-mah"),
    # The same sample one byte short: 7.6 Wh, 8.0 with zeros.
    pytest.param("flown", 12, 3 + 21, "battery_wh", id="battery-wh"),
    # A vehicle_land_detected sample cut inside its timestamp: 100 s of flight, 150 with zeros.
    pytest.param("flown", 33, 3 + 8, "flight_time_s", id="flight-time"),
    # The only failure_detector_status samples cut: faults unknown, none with zeros.
    pytest.param("flown", 4, 3 + 5, "fault_events", id="faults-unknown"),
    # A logged-text message cut right after its header: an error at -10 s whose text is NUL.
    pytest.param("flown", 1, 3, "fault_events", id="logged-text"),
    # The same on the PX4 excerpt: an error at -57.522763 s whose text is NUL.
    pytest.param("fixture", 12, 3, "fault_events", id="fixture-logged-text"),
    # A sample cut in the middle on the PX4 excerpt: the span ends later with zeros.
    pytest.param("fixture", 11, 3 + 38, "log_span_s", id="fixture-span"),
    # Cut inside a message header: stored as the cut log alone, refused with zeros.
    pytest.param("flown", 12, 1, "battery_mah", id="inside-header"),
]


@pytest.mark.filterwarnings("error::ResourceWarning")
@pytest.mark.filterwarnings("error::pytest.PytestUnraisableExceptionWarning")
@pytest.mark.parametrize(("source", "from_end", "into", "field"), CUT_THEN_ZEROS)
def test_a_log_cut_inside_a_message_then_zeros_reads_as_the_cut_log(
    source: str, from_end: int, into: int, field: str, tmp_path: Path
) -> None:
    """A log cut by power loss inside a message and followed by zero bytes reads as the same
    cut without them: to its last whole message. No value is read from the zeros."""
    whole = flown_log(tmp_path / "whole.ulg") if source == "flown" else PX4_FIXTURE
    cut = tmp_path / "cut.ulg"
    cut.write_bytes(cut_in(whole.read_bytes(), from_end, into))
    padded = tmp_path / "padded.ulg"
    padded.write_bytes(cut.read_bytes() + bytes(512))
    alone = read_ulog(cut, **ATTR)
    with_zeros = replace(read_ulog(padded, **ATTR), log_ref=cut.stem)
    assert getattr(with_zeros, field) == getattr(alone, field)
    assert with_zeros == alone


def test_the_battery_sample_cut_in_half_reads_475_mah_with_zeros_after_it(
    tmp_path: Path,
) -> None:
    """The repro of #122: main stored -50.0 mAh, the half-written sample's discharge read as 0."""
    padded = tmp_path / "padded.ulg"
    padded.write_bytes(cut_in(flown_log(tmp_path / "whole.ulg").read_bytes(), 12, 14) + bytes(512))
    r = read_ulog(padded, **ATTR)
    assert (r.battery_mah, r.battery_wh) == (475.0, 7.6)


def test_a_damaged_log_ending_in_zero_bytes_is_still_refused(tmp_path: Path) -> None:
    log = damaged(tmp_path / "framing.ulg", "framing")
    log.write_bytes(log.read_bytes() + bytes(4096))
    with pytest.raises(ValueError) as refused:
        read_ulog(log, **ATTR)
    assert str(refused.value) == DAMAGED


def test_post_ingest_stores_a_log_ending_in_zero_bytes(tmp_path: Path) -> None:
    log = flown_log(tmp_path / "whole.ulg").read_bytes() + bytes(4096)
    store = Store(":memory:")
    client = TestClient(create_app(store, write_token=None), client=("127.0.0.1", 1))
    response = client.post(
        "/ingest",
        data={"aircraft_key": KEY},
        files={"log": ("padded.ulg", log, "application/octet-stream")},
    )
    assert response.status_code == 201, response.text
    assert store.flight_count() == 1


def test_uasw_ingest_of_damaged_logs_alone_exits_1_and_makes_no_store(tmp_path: Path) -> None:
    """The real process, as a user at a shell sees it."""
    logs = [str(damaged(tmp_path / f"{where}.ulg", where)) for where in WHERE]
    db = tmp_path / "fleet.sqlite"
    done = subprocess.run(
        [sys.executable, "-m", "uas_workbench.service.cli", "--db", str(db), "ingest", KEY, *logs],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode == 1, done.stderr
    assert "Traceback" not in done.stderr
    assert [line for line in done.stderr.splitlines() if line.startswith("refused")] == [
        f"refused (422): {log}: {PREFIX}{DAMAGED}" for log in logs
    ]
    assert not db.exists()
