"""A command that only reads refuses a store that does not exist, and children run this code
(part of issue #73).

`uasw verify` given a mistyped `--db` created the file and answered "journal intact: 0 links"
with exit 0: an integrity check that passed on a store that never existed. Every command that
only reads now refuses a store path with nothing at it, one `refused (404)` line and exit 1,
and creates no file and no folder. The commands that write keep making the store on first
use: `seed`, `serve`, `ingest` and `record` (a component can be registered in a new store);
`record history` only reads, and `record retract` needs an entry, so both refuse.

Two more things found while verifying #74. The default `--fixtures` was the relative path
`tests/fixtures`, so a seed run from another folder left the two showcase aircraft out (7
aircraft, not 9); it now resolves beside the code when that folder exists. And tests that
launch `uasw` processes ran whatever `uas_workbench` the child found: with a relative
`PYTHONPATH` and the child in another working folder, that was the main checkout's code, and
nothing said so. `tests/child_code.py` pins the children to the code under test and the suite
checks it before any test runs.

Every command runs in a process of its own, from a working folder that is not the repository.
Assertions are about exit codes, refusal statuses and files, never an operating system's
error text, a path or a line ending.
"""

from __future__ import annotations

import os
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
LOG = next((FIXTURES / "px4").glob("*.ulg"))
SEEDED_HEAD = "c7726f26f1d1fddd45c679b1207d1eb0cebdf9c14b682f06c28d2d48d4384991"
WHO = ["--by", "A. Tester, maintenance", "--statement", "work done as described"]


def uasw(cwd: Path, *args: str, env: dict[str, str] | None = None) -> tuple[int, list[str], str]:
    base = {k: v for k, v in os.environ.items() if k not in ("UASW_WRITE_TOKEN", "UASW_FIXTURES")}
    r = subprocess.run(
        [sys.executable, "-m", "uas_workbench.service.cli", *args],
        cwd=cwd,
        env=base | (env or {}),
        capture_output=True,
        text=True,
        timeout=180,
    )
    lines = [ln for ln in r.stderr.strip().splitlines() if not ln.startswith("{")]
    return r.returncode, lines, r.stderr


def files(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*")}


def rows(db: Path) -> tuple[int, int, int, str | None]:
    c = sqlite3.connect(str(db))
    try:
        n = [
            int(c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
            for t in ("aircraft", "flights", "entries")
        ]
        last = c.execute("SELECT link_hash FROM journal ORDER BY seq DESC LIMIT 1").fetchone()
    finally:
        c.close()
    return n[0], n[1], n[2], (str(last[0]) if last else None)


# ---- commands that only read refuse a store that does not exist ------------------------------

READERS: list[tuple[str, list[str]]] = [
    ("verify", ["verify"]),
    ("verify --head", ["verify", "--head", "0" * 64]),
    ("records", ["records", "SYN-01", "--out", "{out}/records.html"]),
    ("evidence", ["evidence", "SYN-04", "--out", "{out}/evidence.html"]),
    ("fleet-records", ["fleet-records", "--json", "--out", "{out}/fleet.html"]),
    ("export-static", ["export-static", "--out", "{out}/site"]),
    ("record history", ["record", "history", "SYN-01"]),
    ("record retract", ["record", "retract", "1", "--by", "A. Tester", "--reason", "typo"]),
]


@pytest.mark.parametrize("where", ["beside", "in a folder that does not exist"])
@pytest.mark.parametrize(("name", "args"), READERS, ids=[r[0] for r in READERS])
def test_a_command_that_only_reads_refuses_a_store_that_does_not_exist(
    tmp_path: Path, name: str, args: list[str], where: str
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    out = tmp_path / "out"
    out.mkdir()
    db = work / "fleet-typo.sqlite" if where == "beside" else work / "nowhere" / "fleet.sqlite"
    before = files(tmp_path)
    code, lines, err = uasw(work, "--db", str(db), *[a.format(out=out) for a in args])
    assert "Traceback" not in err, err[-400:]
    assert code == 1
    assert len(lines) == 1 and lines[0].startswith("refused (404): "), lines
    assert files(tmp_path) == before  # no store, no side file, no folder, no output


# ---- commands that write make the store on first use, as before -------------------------------


def test_seed_ingest_and_record_still_make_a_new_store(tmp_path: Path) -> None:
    seeded = tmp_path / "a" / "fleet.sqlite"
    code, _, err = uasw(tmp_path, "--db", str(seeded), "seed", "--fixtures", str(FIXTURES))
    assert code == 0, err
    assert rows(seeded)[3] == SEEDED_HEAD

    ingested = tmp_path / "b" / "fleet.sqlite"
    code, _, err = uasw(tmp_path, "--db", str(ingested), "ingest", "OWN-1", str(LOG))
    assert code == 0, err
    assert rows(ingested)[:2] == (1, 1)

    registered = tmp_path / "c" / "fleet.sqlite"
    code, _, err = uasw(
        tmp_path,
        "--db",
        str(registered),
        *["record", "component", "register", "BAT-NEW", "--kind", "battery pack"],
        *["--since", "2026-01-01", *WHO],
    )
    assert code == 0, err
    assert rows(registered)[2] == 1


def test_serve_on_a_new_path_makes_and_seeds_the_store(tmp_path: Path) -> None:
    db = tmp_path / "new" / "fleet.sqlite"
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = {k: v for k, v in os.environ.items() if k not in ("UASW_WRITE_TOKEN", "UASW_FIXTURES")}
    log = (tmp_path / "serve.log").open("w")
    p = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uas_workbench.service.cli",
            "--db",
            str(db),
            "serve",
            "--port",
            str(port),
            "--fixtures",
            str(FIXTURES),
        ],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=log,
    )
    try:
        deadline = time.monotonic() + 120
        while True:
            assert p.poll() is None, (tmp_path / "serve.log").read_text()[-400:]
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2).close()
                break
            except OSError:
                assert time.monotonic() < deadline, "the service never answered"
                time.sleep(0.2)
    finally:
        p.terminate()
        p.wait(30)
        log.close()
    assert rows(db)[3] == SEEDED_HEAD


# ---- the default --fixtures does not depend on the working folder ------------------------------


def test_a_seed_from_another_folder_finds_the_fixtures_beside_the_code(tmp_path: Path) -> None:
    db = tmp_path / "fleet.sqlite"
    code, _, err = uasw(tmp_path, "--db", str(db), "seed")
    assert code == 0, err
    assert rows(db)[:2] == (9, 34)
    assert rows(db)[3] == SEEDED_HEAD


def test_uasw_fixtures_still_names_the_fixtures_when_set(tmp_path: Path) -> None:
    empty = tmp_path / "no-fixtures"
    empty.mkdir()
    db = tmp_path / "fleet.sqlite"
    code, _, err = uasw(tmp_path, "--db", str(db), "seed", env={"UASW_FIXTURES": str(empty)})
    assert code == 0, err
    assert rows(db)[:2] == (7, 30)  # the synthetic fleet alone: the setting was obeyed


# ---- children run the code under test -----------------------------------------------------


def test_a_child_started_in_another_folder_runs_the_code_under_test(tmp_path: Path) -> None:
    from child_code import check_children_run_this_code, child_package, under_test

    # The suite pinned it before any test ran: the code under test's folder first, absolute.
    first = os.environ["PYTHONPATH"].split(os.pathsep)[0]
    assert Path(first).is_absolute() and Path(first) == under_test().parent
    assert child_package(cwd=tmp_path) == under_test()
    check_children_run_this_code()


def test_the_guard_fires_when_a_child_would_run_other_code(tmp_path: Path) -> None:
    from child_code import ChildCodeMismatch, check_children_run_this_code

    other = tmp_path / "other" / "uas_workbench"
    other.mkdir(parents=True)
    (other / "__init__.py").write_text("")
    env = dict(os.environ) | {"PYTHONPATH": str(other.parent)}
    with pytest.raises(ChildCodeMismatch):
        check_children_run_this_code(env=env)


def test_a_relative_pythonpath_is_pinned_so_a_child_elsewhere_still_runs_this_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The case that misled a verification: `PYTHONPATH=src` given relative to the checkout,
    and the child started in another working folder."""
    from child_code import child_package, pinned_pythonpath, under_test

    monkeypatch.chdir(under_test().parent.parent)
    pinned = pinned_pythonpath("src" + os.pathsep + "tests")
    first, *rest = pinned.split(os.pathsep)
    assert Path(first).is_absolute() and all(Path(p).is_absolute() for p in rest)
    env = dict(os.environ) | {"PYTHONPATH": pinned}
    assert child_package(cwd=tmp_path, env=env) == under_test()


def test_export_static_refuses_a_store_without_the_sample_aircraft(tmp_path: Path) -> None:
    """The static demo is built around the seeded fleet's SYN-04; a store of one's own logs,
    which does not hold it, ended in a KeyError traceback. One line, and no site written."""
    db = tmp_path / "own.sqlite"
    code, _, err = uasw(tmp_path, "--db", str(db), "ingest", "OWN-1", str(LOG))
    assert code == 0, err
    site = tmp_path / "site"
    code, lines, err = uasw(tmp_path, "--db", str(db), "export-static", "--out", str(site))
    assert "Traceback" not in err, err[-400:]
    assert code == 1
    assert len(lines) == 1 and lines[0].startswith("refused (409): "), lines
    assert not site.exists()
