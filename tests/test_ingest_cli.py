"""uasw ingest: the same refusals as POST /ingest, one line per refused file, and the rest read."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from builders.dataflash import DataFlashBuilder
from uas_workbench.service.app import create_app
from uas_workbench.service.cli import main
from uas_workbench.service.store import Store

ALFA = sorted((Path(__file__).parent / "fixtures" / "alfa").glob("*.bin"))
KEY = "cli-ingest-01"


@pytest.fixture
def logs(tmp_path: Path) -> list[Path]:
    """first.bin, junk.bin, defs_only.bin, second.bin: two real logs around two unreadable."""
    first, second = tmp_path / "first.bin", tmp_path / "second.bin"
    shutil.copy(ALFA[0], first)
    shutil.copy(ALFA[1], second)
    junk = tmp_path / "junk.bin"
    junk.write_bytes(b"not a log at all")
    defs_only = DataFlashBuilder().define("ATT", "Qff", "TimeUS,Roll,Pitch")
    defs_only.define("ARM", "QBH", "TimeUS,ArmState,ArmChecks")
    return [first, junk, defs_only.write(tmp_path / "defs_only.bin"), second]


def api_detail(path: Path) -> str:
    """What POST /ingest answers for the same file: the wording the command line must keep."""
    client = TestClient(create_app(Store(":memory:"), write_token=None), client=("127.0.0.1", 1))
    response = client.post(
        "/ingest",
        data={"aircraft_key": KEY},
        files={"log": (path.name, path.read_bytes(), "application/octet-stream")},
    )
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert isinstance(detail, str)
    return detail


def stored(db: Path) -> list[str]:
    return [f.log_ref for f in Store(str(db)).flights(KEY)]


def test_ingest_reads_past_unreadable_files_and_names_each_refusal(
    tmp_path: Path, logs: list[Path], capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "fleet.sqlite"
    with pytest.raises(SystemExit) as exit_:
        main(["--db", str(db), "ingest", KEY, *map(str, logs)])
    assert exit_.value.code == 1
    assert stored(db) == ["first", "second"]
    err = capsys.readouterr().err
    refusals = [line for line in err.splitlines() if line.startswith("refused")]
    assert refusals == [f"refused (422): {p}: {api_detail(p)}" for p in logs[1:3]]
    assert refusals[0].endswith(": no DataFlash messages found")
    assert refusals[1].endswith(": holds format definitions but no recorded data")
    assert "Traceback" not in err


def test_ingest_of_the_four_files_as_a_command_prints_no_traceback(
    tmp_path: Path, logs: list[Path]
) -> None:
    """The real process: its exit status and its stderr, as a user at a shell sees them."""
    db = tmp_path / "fleet.sqlite"
    done = subprocess.run(
        [sys.executable, "-m", "uas_workbench.service.cli", "--db", str(db), "ingest", KEY]
        + [str(p) for p in logs],
        capture_output=True,
        text=True,
        env=os.environ | {"PYTHONWARNINGS": "ignore"},
        timeout=120,
    )
    assert done.returncode == 1, done.stderr
    assert "Traceback" not in done.stderr
    assert [line for line in done.stderr.splitlines() if line.startswith("refused")] == [
        f"refused (422): {p}: {api_detail(p)}" for p in logs[1:3]
    ]
    assert stored(db) == ["first", "second"]


def test_a_refused_file_stores_nothing_and_registers_no_aircraft(
    tmp_path: Path, logs: list[Path]
) -> None:
    db = tmp_path / "fleet.sqlite"
    with pytest.raises(SystemExit) as exit_:
        main(["--db", str(db), "ingest", KEY, str(logs[1]), str(logs[2])])
    assert exit_.value.code == 1
    store = Store(str(db))
    assert store.get_aircraft(KEY) is None
    assert store.flight_count() == 0


def test_readable_files_and_duplicates_exit_zero_as_before(
    tmp_path: Path, logs: list[Path], capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "fleet.sqlite"
    first, second = logs[0], logs[3]
    main(["--db", str(db), "ingest", KEY, str(first), str(second), str(first)])
    assert stored(db) == ["first", "second"]
    err = capsys.readouterr().err
    assert f"{KEY}/first is already stored" in err
    assert "refused" not in err
