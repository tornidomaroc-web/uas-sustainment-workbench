"""A ULog damaged inside is refused, as a damaged DataFlash log is, on pyulog 1.2.4 and 1.3.0.

pyulog skips what it cannot parse and sets `file_corruption`: a data message whose framing
breaks on both versions, and from 1.3.0 (PX4/pyulog#128) a definition or a subscription too,
where 1.2.4 raised. The flight read from what is left can lose a topic, the aircraft key or
most of its span, and a missing topic then reads as one the log never held. Measured on
2026-10-21 over 448 damaged copies of two logs: 1.2.4 gave a flight for 37 that pyulog called
corrupt, 1.3.0 for 62 (23 of them 1.2.4 refused); none of the 45 real logs in tests/fixtures
and data/raw is called corrupt by either version.
"""

from __future__ import annotations

import struct
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
import pyulog
from fastapi.testclient import TestClient
from test_flight_px4 import ATTR, flown_log

from uas_workbench.flight.px4 import read_ulog
from uas_workbench.service.app import create_app
from uas_workbench.service.cli import main
from uas_workbench.service.store import Store

KEY = "damaged-ulog-01"
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


def refusal(where: str) -> str:
    """The detail the ingest path gives. A broken framing is refused in the same words on both
    versions. On a damaged definition or subscription 1.2.4 raises pyulog's KeyError, naming
    the subscription it could not match (the inverted byte is not UTF-8 and is dropped), where
    1.3.0 reads past it and the log is refused as damaged."""
    if where == "framing" or hasattr(pyulog, "ULogException"):
        return PREFIX + DAMAGED
    return (
        PREFIX + {"definition": "'battery_status'", "subscription": "'vehicle_lan_detected'"}[where]
    )


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
    assert response.json() == {"detail": refusal(where)}
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
        f"refused (422): {log}: {refusal(where)}"
    ]
    assert "Traceback" not in err
    store = Store(str(db))
    assert [f.log_ref for f in store.flights(KEY)] == ["whole"]


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
        f"refused (422): {log}: {refusal(where)}" for log, where in zip(logs, WHERE, strict=True)
    ]
    assert not db.exists()
