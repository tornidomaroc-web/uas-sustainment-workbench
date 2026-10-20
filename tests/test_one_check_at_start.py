"""`serve` and `seed` on a store that holds data check the file once, not twice (#117).

Every command checks an existing store whole with SQLite's `PRAGMA integrity_check` as it
opens it (#104, #105, #107). `serve` and `seed` then ran it a second time: their seed opens
`Store.batch()`, finds the store full and writes nothing, and the batch still counted one own
commit and ran `_checkpoint()`, whose check runs whenever it had own commits to fold. On a
store whose index keys were written out of order the check takes about 6 s, so both took
twice that to start.

A batch that wrote nothing, by SQLite's own count of the rows the writing connection changed
(`total_changes`), now folds nothing and checks nothing; a batch that wrote anything still
checkpoints and checks as before. What the second check also covered stays covered: the store
compares the file with the one `stat` taken before the check at rest, so a change landing
while that check ran, or before the store was open, is checked at the store's first look.

Counted through real `uasw` processes, each check counted where SQLite runs it. Assertions are
about counts, exit codes, statuses and the store's own answers, never an operating system's
error text, a path or a line ending.
"""

from __future__ import annotations

import contextlib
import importlib
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from test_damaged_at_rest import FIXTURES, edit_index_key

from uas_workbench.fleet import load_config
from uas_workbench.fleet.synthetic import generate
from uas_workbench.service import cli
from uas_workbench.service.store import Store, StoreDamaged

STORE = importlib.import_module("uas_workbench.service.store")

# Runs `uasw` as `python -m uas_workbench.service.cli` does, with each `integrity_check` the
# process runs counted as one line in the file named first.
COUNTED = """
import runpy, sys
from pathlib import Path
from uas_workbench.service import store

tally = Path(sys.argv[1])
check = store._sqlite_check

def counted(connection, pragma):
    if pragma == "integrity_check":
        with tally.open("a", encoding="utf-8") as f:
            f.write("check\\n")
    return check(connection, pragma)

store._sqlite_check = counted
sys.argv = ["uasw", *sys.argv[2:]]
runpy.run_module("uas_workbench.service.cli", run_name="__main__", alter_sys=True)
"""


def counted(tmp_path: Path, tally: Path, *args: str) -> subprocess.Popen[bytes]:
    env = {k: v for k, v in os.environ.items() if k != "UASW_WRITE_TOKEN"}
    with (tmp_path / "stderr.log").open("ab") as stderr:
        return subprocess.Popen(
            [sys.executable, "-c", COUNTED, str(tally), *args],
            cwd=tmp_path,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=stderr,
        )


def checks(tally: Path) -> int:
    return len(tally.read_text(encoding="utf-8").splitlines()) if tally.exists() else 0


def refusals(tmp_path: Path) -> list[str]:
    err = (tmp_path / "stderr.log").read_text(encoding="utf-8", errors="replace")
    return [ln for ln in err.strip().splitlines() if not ln.startswith("{")]


def at_rest(db: Path) -> None:
    """The store file last changed an hour ago, as a store left alone is."""
    old = time.time_ns() - 3600 * 10**9
    os.utime(db, ns=(old, old))


@pytest.fixture
def held(tmp_path: Path) -> Path:
    """A seeded store, one file, at rest."""
    db = tmp_path / "fleet.sqlite"
    seed = counted(
        tmp_path, tmp_path / "made.txt", "--db", str(db), "seed", "--fixtures", str(FIXTURES)
    )
    assert seed.wait(timeout=180) == 0, refusals(tmp_path)
    assert not Path(f"{db}-wal").exists()
    at_rest(db)
    return db


def test_seed_on_a_store_that_holds_data_checks_it_once(tmp_path: Path, held: Path) -> None:
    before = held.read_bytes()
    tally = tmp_path / "checks.txt"
    seed = counted(tmp_path, tally, "--db", str(held), "seed", "--fixtures", str(FIXTURES))
    assert seed.wait(timeout=180) == 1
    assert [ln for ln in refusals(tmp_path) if ln.startswith("refused (409)")], refusals(tmp_path)
    assert checks(tally) == 1, f"{checks(tally)} checks of a store the seed left as it was"
    assert held.read_bytes() == before


def test_serve_on_a_store_that_holds_data_checks_it_once_before_it_answers(
    tmp_path: Path, held: Path
) -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    tally = tmp_path / "checks.txt"
    serve = counted(
        tmp_path,
        tally,
        "--db",
        str(held),
        "serve",
        "--port",
        str(port),
        "--fixtures",
        str(FIXTURES),
    )
    answered, started = None, 0
    try:
        deadline = time.monotonic() + 120
        while serve.poll() is None and time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as r:
                    answered = r.status
                    started = checks(tally)
                break
            except (urllib.error.URLError, OSError):
                time.sleep(0.1)
    finally:
        if serve.poll() is None:
            serve.terminate()
        serve.wait(timeout=60)
    assert answered == 200, refusals(tmp_path)
    assert started == 1, f"{started} checks before the service answered"


def test_seed_on_nothing_still_checks_at_the_checkpoint_after_its_batch(tmp_path: Path) -> None:
    db = tmp_path / "fleet.sqlite"
    tally = tmp_path / "checks.txt"
    seed = counted(tmp_path, tally, "--db", str(db), "seed", "--fixtures", str(FIXTURES))
    assert seed.wait(timeout=180) == 0, refusals(tmp_path)
    assert checks(tally) == 1, f"{checks(tally)} checks of a seed that wrote the store"


class Counted:
    """Checkpoints and file checks made by one store."""

    def __init__(self, store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
        self.checkpoints = self.checks = 0
        checkpoint, check = store._checkpoint, store._damage_in

        def counted_checkpoint() -> None:
            self.checkpoints += 1
            checkpoint()

        def counted_check(now: Any) -> Any:
            self.checks += 1
            return check(now)

        monkeypatch.setattr(store, "_checkpoint", counted_checkpoint)
        monkeypatch.setattr(store, "_damage_in", counted_check)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    db = tmp_path / "fleet.sqlite"
    seeding = Store(str(db))
    seeding.add_fleet(generate(load_config()))
    seeding.close()
    opened = Store(str(db))
    try:
        yield opened
    finally:
        opened.close()


def wal_size(store: Store) -> int:
    wal = Path(f"{store._path}-wal")
    return wal.stat().st_size if wal.exists() else 0


@pytest.mark.parametrize(
    "nothing",
    ["a read", "a statement that matches no row", "an insert that is ignored"],
)
def test_a_batch_that_writes_nothing_folds_nothing_and_checks_nothing(
    store: Store, monkeypatch: pytest.MonkeyPatch, nothing: str
) -> None:
    held = store.holds()
    counted = Counted(store, monkeypatch)
    look, wal = os.stat(store._path), wal_size(store)
    with store.batch():
        if nothing == "a read":
            store.holds()
        elif nothing == "a statement that matches no row":
            store._db.execute("UPDATE aircraft SET label = label WHERE 0")
        else:
            key = store.aircraft()[0].key
            store._db.execute(
                "INSERT OR IGNORE INTO aircraft (key, label, source, synthetic, licence, "
                "attribution) VALUES (?, '', 'px4', 1, '', '')",
                (key,),
            )
    assert (counted.checkpoints, counted.checks) == (0, 0)
    after = os.stat(store._path)
    assert (after.st_size, after.st_mtime_ns) == (look.st_size, look.st_mtime_ns)
    assert wal_size(store) == wal
    assert store.holds() == held


def test_a_batch_that_writes_still_folds_and_checks(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    aircraft = store.aircraft()[0]
    counted = Counted(store, monkeypatch)
    look = os.stat(store._path)
    with store.batch():
        store.add_aircraft(aircraft)  # a row replaced by itself is still a row written
    assert (counted.checkpoints, counted.checks) == (1, 1)
    after = os.stat(store._path)
    assert (after.st_size, after.st_mtime_ns) != (look.st_size, look.st_mtime_ns)  # folded in


def opened_with_change_during_its_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, keep_stat: bool, recent: bool
) -> Store:
    """A seeded store opened by `cli.open_store`, with one key of its flights index edited in
    place after the check at rest passed and before the store is open: the window the second
    check covered. With `keep_stat`, the file's size and time of last change are put back as
    they were before the check, as a change within the clock's tick of the last one leaves
    them; with `recent`, that last change was made just before the check."""
    db = tmp_path / "fleet.sqlite"
    seeding = Store(str(db))
    seeding.add_fleet(generate(load_config()))
    seeding.close()
    if recent:
        now = time.time_ns()
        os.utime(db, ns=(now, now))
    else:
        at_rest(db)
    looked = os.stat(db)
    checked_at_rest = cli.checked_at_rest

    @contextlib.contextmanager
    def changed_after(path: str) -> Iterator[Any]:
        with checked_at_rest(path) as found:
            assert found is None
            with db.open("r+b") as f:  # in place, as an editor writes it
                f.write(edit_index_key(db.read_bytes(), db))
            if keep_stat:
                os.utime(db, ns=(looked.st_atime_ns, looked.st_mtime_ns))
            yield found

    monkeypatch.setattr(cli, "checked_at_rest", changed_after)
    monkeypatch.setattr(cli, "_checked", set())
    store = cli.open_store(str(db))
    return store


@pytest.mark.parametrize("recent", [False, True], ids=["at rest", "changed just before"])
def test_a_change_landing_during_the_check_at_rest_is_still_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recent: bool
) -> None:
    store = opened_with_change_during_its_check(
        tmp_path, monkeypatch, keep_stat=False, recent=recent
    )
    try:
        with contextlib.suppress(StoreDamaged):
            cli.seed(store, FIXTURES)
        with pytest.raises(StoreDamaged):
            store.holds()
    finally:
        store.close()


def test_a_change_in_the_clock_tick_of_a_change_just_before_the_check_is_still_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = opened_with_change_during_its_check(tmp_path, monkeypatch, keep_stat=True, recent=True)
    try:
        with contextlib.suppress(StoreDamaged):
            cli.seed(store, FIXTURES)
        with pytest.raises(StoreDamaged):
            store.holds()
    finally:
        store.close()
