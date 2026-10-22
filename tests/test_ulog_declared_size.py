"""A ULog whose definitions declare more than the workbench reads is refused before pyulog lays
them out (issue #125).

pyulog lays out every element of a definition once per subscription to it, a definition nested
in another once per element of the array that holds it, each with a name of its own, before the
workbench sees the log and whether or not it keeps the topic. pyulog 1.3.0 bounds each array
to 65 535 elements, not the log: measured on main (d45e5cf, Windows, Python 3.12, pyulog 1.3.0)
50 fields of 65 535 took 13.8 s and 1.8 GiB, a 117-byte file of one field holding 64 of a
65 535-element definition 19.2 s and 2.3 GiB; 10 such fields with 10 subscriptions ran out of a
3 GiB cap after 26.4 s, and a 65 KB file of one field with a 65 000-character name after 1.4 s.
A definition that holds itself, or a chain of 3 000, ended the read in a RecursionError. A file
of 1 MiB of 0xFF bytes after its first messages took 15.0 s: pyulog reads each byte as a damaged
header 65 535 bytes long, reads those bytes, and steps on by one.

Each case that would cost that much runs in a process of its own, capped in time and memory
(tests/capped.py), so main fails these tests by the cap, not by taking the run down with it.
"""

from __future__ import annotations

import tempfile
from dataclasses import replace
from pathlib import Path

import pytest
from capped import run_capped
from fastapi.testclient import TestClient
from test_flight_px4 import ATTR, flown_log
from test_ulog_damaged import DAMAGED, PREFIX, damaged

from builders.ulog import ULogBuilder
from uas_workbench.census.excerpt import excerpt_ulog
from uas_workbench.flight import ulog_declared
from uas_workbench.flight.px4 import read_ulog
from uas_workbench.service.app import create_app
from uas_workbench.service.cli import main
from uas_workbench.service.store import Store

KEY = "declared-ulog-01"
NOT_COUNTED = DAMAGED.split("; ", 1)[1]
TOO_MANY = (
    "its format definitions declare more than 1,048,576 definitions, fields and elements for "
    f"pyulog to lay out, the most the workbench reads; {NOT_COUNTED}"
)
TOO_LONG = (
    "its format definitions declare more than 33,554,432 characters of element names for "
    f"pyulog to lay out, the most the workbench reads; {NOT_COUNTED}"
)
TOO_DEEP = (
    "its format definitions nest more than 16 deep, or hold themselves, deeper than the "
    f"workbench reads; {NOT_COUNTED}"
)
NO_DATA = "holds a header but no recorded data"
MIB, SECONDS = 1024, 60


def shape(path: Path, definitions: list[tuple[str, list[str]]], topics: list[str]) -> Path:
    """A header, the definitions and one subscription to each of `topics`, no data."""
    log = ULogBuilder()
    for name, fields in definitions:
        log.format(name, fields)
    for topic in topics:
        log.subscribe(topic)
    return log.write(path)


def chain(depth: int) -> list[tuple[str, list[str]]]:
    """c1 holds one c0, c2 one c1, and so on: `depth` definitions nested in one another."""
    return [("c0", ["uint64_t timestamp"])] + [(f"c{i}", [f"c{i - 1} y"]) for i in range(1, depth)]


def doubling(depth: int) -> list[tuple[str, list[str]]]:
    """d1 holds two d0, d2 two d1: 2**(depth-1) elements in `depth` definitions."""
    return [("d0", ["uint8_t x"])] + [(f"d{i}", [f"d{i - 1}[2] y"]) for i in range(1, depth)]


INNER = ("inner", ["uint8_t[65535] x"])
HEAVY = [
    # 50 fields of 65 535 elements, one subscription: 3.3 million elements.
    pytest.param([("t", [f"uint8_t[65535] a{i}" for i in range(50)])], ["t"], TOO_MANY, id="flat"),
    # 10 fields of 65 535, subscribed 10 times: 6.6 million.
    pytest.param(
        [("t", [f"uint8_t[65535] a{i}" for i in range(10)])], ["t"] * 10, TOO_MANY, id="subs"
    ),
    # One field of 64 definitions of 65 535: 4.2 million in 117 bytes.
    pytest.param([INNER, ("outer", ["inner[64] y"])], ["outer"], TOO_MANY, id="nested"),
    # One field of 65 535 such definitions: 4.3 billion.
    pytest.param([INNER, ("outer", ["inner[65535] y"])], ["outer"], TOO_MANY, id="nested-max"),
    # Forty definitions, each holding two of the one before: 2**39 elements in 705 bytes.
    pytest.param(doubling(40), ["d39"], TOO_DEEP, id="deeply-nested"),
    # One field of 65 535 elements whose name is 65 000 characters: 4.3 billion characters.
    pytest.param([("t", [f"uint8_t[65535] {'n' * 65000}"])], ["t"], TOO_LONG, id="long-name"),
]

READ = """
import json, sys
from pathlib import Path
from uas_workbench.flight.px4 import read_ulog
try:
    read_ulog(Path(sys.argv[1]), licence="CC BY 4.0", attribution="test")
    said = "read"
except MemoryError:
    said = "MemoryError"
except Exception as exc:
    said = str(exc)
print(json.dumps(said))
"""


def capped_read(log: Path, seconds: float = SECONDS) -> str:
    outcome = run_capped(READ, mib=MIB, seconds=seconds, args=[str(log)])
    assert isinstance(outcome.said, str), outcome
    return outcome.said


@pytest.mark.parametrize(("definitions", "topics", "refusal"), HEAVY)
def test_a_log_declaring_too_much_is_refused_before_it_is_laid_out(
    definitions: list[tuple[str, list[str]]], topics: list[str], refusal: str, tmp_path: Path
) -> None:
    log = shape(tmp_path / "declared.ulg", definitions, topics)
    assert log.stat().st_size < 70_000
    assert capped_read(log) == refusal


def test_a_log_declaring_too_much_and_ending_in_zero_bytes_is_refused_the_same(
    tmp_path: Path,
) -> None:
    log = shape(tmp_path / "declared.ulg", [INNER, ("outer", ["inner[65535] y"])], ["outer"])
    log.write_bytes(log.read_bytes() + bytes(512))
    assert capped_read(log) == TOO_MANY


@pytest.mark.parametrize(
    ("over", "said"), [pytest.param(0, NO_DATA, id="at"), pytest.param(1, TOO_MANY, id="past")]
)
def test_the_bound_is_1_048_576_definitions_fields_and_elements(
    over: int, said: str, tmp_path: Path
) -> None:
    """Three definitions of one field each (6), 16 x 65 535 elements in `outer` and 10 + over
    in `p`: 1 048 576 + over. At the bound the log reads as before, here refused for holding
    no data; one more and it is refused before pyulog lays it out."""
    definitions = [INNER, ("outer", ["inner[16] y"]), ("p", [f"uint8_t[{10 + over}] z"])]
    log = shape(tmp_path / "bound.ulg", definitions, ["outer", "p"])
    outcome = run_capped(READ, mib=3072, seconds=SECONDS, args=[str(log)])
    assert outcome.said == said, outcome


@pytest.mark.filterwarnings("error::ResourceWarning")
@pytest.mark.filterwarnings("error::pytest.PytestUnraisableExceptionWarning")
@pytest.mark.parametrize(
    "definitions",
    [
        pytest.param([("loop", ["uint64_t timestamp", "loop me"])], id="self-nested"),
        pytest.param([("loop", ["uint64_t timestamp", "loop[2] me"])], id="self-nested-array"),
        pytest.param([("a", ["uint64_t timestamp", "b x"]), ("b", ["a y"])], id="mutual"),
        pytest.param(chain(17), id="chain-of-17"),
        pytest.param(chain(3000), id="chain-of-3000"),
    ],
)
def test_definitions_nested_too_deep_or_in_themselves_are_refused(
    definitions: list[tuple[str, list[str]]], tmp_path: Path
) -> None:
    """main ended these in a RecursionError, but the chain of 17, which it read."""
    log = shape(tmp_path / "nested.ulg", definitions, [definitions[-1][0]])
    with pytest.raises(ValueError) as refused:
        read_ulog(log, **ATTR)
    assert str(refused.value) == TOO_DEEP


def test_definitions_nested_16_deep_read_as_before(tmp_path: Path) -> None:
    log = shape(tmp_path / "nested.ulg", chain(16), ["c15"])
    with pytest.raises(ValueError) as refused:
        read_ulog(log, **ATTR)
    assert str(refused.value) == NO_DATA


def test_a_definition_naming_a_type_never_defined_is_refused_as_damaged(tmp_path: Path) -> None:
    """pyulog 1.3.0 marks the file corrupt; main refused it so too."""
    log = shape(tmp_path / "undefined.ulg", [("o", ["uint64_t timestamp", "nosuch x"])], ["o"])
    with pytest.raises(ValueError) as refused:
        read_ulog(log, **ATTR)
    assert str(refused.value) == DAMAGED


def flood(path: Path, *, in_definitions: bool, mib: int) -> Path:
    """A log whose first messages frame, then `mib` MiB of 0xFF: every byte the start of a
    header pyulog reads as damage, 65 535 bytes long."""
    log = ULogBuilder().format("t", ["uint64_t timestamp"])
    if not in_definitions:
        log.subscribe("t")
    path.write_bytes(log.build() + b"\xff" * (mib << 20))
    return path


@pytest.mark.parametrize("in_definitions", [False, True], ids=["data", "definitions"])
def test_a_log_that_stops_framing_is_refused_without_a_walk_through_the_rest(
    in_definitions: bool, tmp_path: Path
) -> None:
    """main read 1 MiB of it in 15 s, a byte at a time, and then refused it as damaged."""
    log = flood(tmp_path / "flood.ulg", in_definitions=in_definitions, mib=8)
    assert capped_read(log, seconds=20) == DAMAGED


@pytest.fixture
def flag_never_fires(monkeypatch: pytest.MonkeyPatch) -> None:
    """pyulog as it would be if it stopped setting the private `_file_corrupt` the read watches
    to end early: the watch never fires."""
    bounded = ulog_declared._bounded
    monkeypatch.setattr(ulog_declared, "_bounded", lambda counts, _stop: bounded(counts, False))


@pytest.mark.usefixtures("flag_never_fires")
@pytest.mark.parametrize("zeros", [0, 4096])
def test_a_damaged_log_is_refused_if_the_watched_flag_never_fires(
    zeros: int, tmp_path: Path
) -> None:
    """pyulog's public `file_corruption` still refuses it: on a copy of pyulog 1.3.0 with only
    that attribute renamed, this log was stored, with and without zeros after it."""
    log = damaged(tmp_path / "framing.ulg", "framing")
    log.write_bytes(log.read_bytes() + bytes(zeros))
    with pytest.raises(ValueError) as refused:
        read_ulog(log, **ATTR)
    assert str(refused.value) == DAMAGED


@pytest.mark.usefixtures("flag_never_fires")
def test_a_log_ending_in_zero_bytes_reads_as_before_if_the_watched_flag_never_fires(
    tmp_path: Path,
) -> None:
    whole = flown_log(tmp_path / "whole.ulg")
    padded = tmp_path / "padded.ulg"
    padded.write_bytes(whole.read_bytes() + bytes(4096))
    assert replace(read_ulog(padded, **ATTR), log_ref=whole.stem) == read_ulog(whole, **ATTR)


def test_uasw_ingest_refuses_a_log_declaring_too_much_and_reads_the_next(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    log = shape(tmp_path / "nested.ulg", [("loop", ["uint64_t timestamp", "loop me"])], ["loop"])
    db = tmp_path / "fleet.sqlite"
    with pytest.raises(SystemExit) as exit_:
        main(["--db", str(db), "ingest", KEY, str(log)])
    assert exit_.value.code == 1
    err = capsys.readouterr().err
    assert [line for line in err.splitlines() if line.startswith("refused")] == [
        f"refused (422): {log}: {PREFIX}{TOO_DEEP}"
    ]
    assert "Traceback" not in err
    assert not db.exists()


CLI = """
import json, subprocess, sys
done = subprocess.run(
    [sys.executable, "-m", "uas_workbench.service.cli", "--db", sys.argv[1], "ingest",
     "declared-ulog-01", sys.argv[2]],
    capture_output=True, text=True,
)
refused = [line for line in done.stderr.splitlines() if line.startswith("refused")]
print(json.dumps([done.returncode, refused]))
"""


def test_uasw_ingest_of_a_log_declaring_billions_exits_1_with_one_line(tmp_path: Path) -> None:
    """The real command, its own process (inside the capped one, which a job object or the
    address space limit covers too)."""
    log = shape(tmp_path / "nested.ulg", [INNER, ("outer", ["inner[65535] y"])], ["outer"])
    db = tmp_path / "fleet.sqlite"
    outcome = run_capped(CLI, mib=MIB, seconds=SECONDS, args=[str(db), str(log)])
    assert outcome.said == [1, [f"refused (422): {log}: {PREFIX}{TOO_MANY}"]], outcome
    assert not db.exists()


API = """
import json, sys, tempfile
from pathlib import Path
from fastapi.testclient import TestClient
from uas_workbench.service.app import create_app
from uas_workbench.service.store import Store
tempfile.tempdir = sys.argv[2]
store = Store(":memory:")
client = TestClient(create_app(store, write_token=None), client=("127.0.0.1", 1))
r = client.post(
    "/ingest",
    data={"aircraft_key": "declared-ulog-01"},
    files={"log": ("nested.ulg", Path(sys.argv[1]).read_bytes(), "application/octet-stream")},
)
print(json.dumps([r.status_code, r.json(), store.flight_count(),
                  store.get_aircraft("declared-ulog-01") is None]))
"""


def test_post_ingest_refuses_a_log_declaring_billions_and_stores_nothing(tmp_path: Path) -> None:
    log = shape(tmp_path / "nested.ulg", [INNER, ("outer", ["inner[65535] y"])], ["outer"])
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    outcome = run_capped(API, mib=MIB, seconds=SECONDS, args=[str(log), str(uploads)])
    assert outcome.said == [422, {"detail": PREFIX + TOO_MANY}, 0, True], outcome
    assert list(uploads.iterdir()) == []


def test_post_ingest_refuses_definitions_that_hold_themselves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = shape(
        tmp_path / "nested.ulg", [("a", ["uint64_t timestamp", "b x"]), ("b", ["a y"])], ["a"]
    )
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(uploads))
    store = Store(":memory:")
    client = TestClient(create_app(store, write_token=None), client=("127.0.0.1", 1))
    response = client.post(
        "/ingest",
        data={"aircraft_key": KEY},
        files={"log": ("nested.ulg", log.read_bytes(), "application/octet-stream")},
    )
    assert response.status_code == 422, response.text
    assert response.json() == {"detail": PREFIX + TOO_DEEP}
    assert store.get_aircraft(KEY) is None
    assert store.flight_count() == 0
    assert list(uploads.iterdir()) == []


CENSUS = """
import json, sys
from pathlib import Path
from uas_workbench.census.cli import _px4_job
print(json.dumps(_px4_job((Path(sys.argv[1]), "quadrotor")).error))
"""


def test_the_census_counts_a_log_declaring_billions_as_unreadable(tmp_path: Path) -> None:
    log = shape(tmp_path / "nested.ulg", [INNER, ("outer", ["inner[65535] y"])], ["outer"])
    outcome = run_capped(CENSUS, mib=MIB, seconds=SECONDS, args=[str(log)])
    assert outcome.said == f"ValueError: {TOO_MANY}", outcome


def test_an_excerpt_is_not_cut_from_definitions_that_hold_themselves(tmp_path: Path) -> None:
    log = shape(tmp_path / "nested.ulg", [("loop", ["uint64_t timestamp", "loop me"])], ["loop"])
    with pytest.raises(ValueError) as refused:
        excerpt_ulog(log)
    assert str(refused.value) == TOO_DEEP


def test_capped_children_report_what_they_print(tmp_path: Path) -> None:
    """The harness itself: a child that prints is read, one that runs over is cut off."""
    assert run_capped("print(1 + 1)", mib=MIB, seconds=SECONDS, args=[]).said == 2
    late = run_capped("import time; time.sleep(30)", mib=MIB, seconds=2, args=[])
    assert late.said == "timeout"
    big = run_capped(
        "import json\ntry:\n    b = bytearray(2 << 30)\n    print(json.dumps('allocated'))\n"
        "except MemoryError:\n    print(json.dumps('refused'))",
        mib=MIB,
        seconds=SECONDS,
        args=[],
    )
    assert big.said == "refused"
