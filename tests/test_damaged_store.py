"""A store file damaged while `uasw serve` runs is refused, never a 500 (part of issue #73).

SQLite does not see a file changed from outside it: a connection keeps serving the pages it
holds in memory. So a store file overwritten, truncated or corrupted while the service ran was
answered as if nothing had happened: every read 200, every write 201 (onto the damaged file),
and `/health` "ok"; the first page read from disk then gave a 500, and after the file was
restored from a good copy 16 of 22 reads still answered 500 from the pages read meanwhile.

The store now looks at its file (one `stat`: identity, size, time of last change, mode) before
every read and every write. When the file changed and SQLite did not change it, the store checks
it (`PRAGMA integrity_check` on a connection of its own, `quick_check` before #107): damaged,
every route that reads the store answers 503 naming the cause, `/health` included, and every
write is refused with nothing written; sound (a store restored in place), every connection is
opened afresh and every kept answer computed again. A store file replaced or removed while the
service runs is refused until the service is restarted.

Every case damages a real store file under a live `uasw serve`, which logs to a file. Windows
refuses to replace or remove a file the service holds open, so on Windows those two cases
check that the attempt was refused and the service still answers whole; elsewhere they check
the refusal. Assertions are about statuses, counts and the service's own answers, never an
operating system's error text, a path or a line ending.
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
LOG = next((FIXTURES / "px4").glob("*.ulg"))
# Routes that do not read the store: they answer whatever the store's state.
STORELESS = {"/metrics", "/openapi.json", "/docs", "/redoc", "/docs/oauth2-redirect"}


@dataclass
class Live:
    base: str
    db: Path
    original: bytes
    routes: list[str]


def call(
    base: str, method: str, path: str, body: bytes | None = None, ctype: str = "application/json"
) -> tuple[int, dict[str, object] | str]:
    req = urllib.request.Request(
        base + path, data=body, method=method, headers={"Content-Type": ctype} if body else {}
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            status, text = r.status, r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        with e:  # an error answer holds its response open until closed
            status, text = e.code, e.read().decode("utf-8")
    try:
        return status, json.loads(text)
    except ValueError:
        return status, text


def counts(db: Path) -> tuple[int, int, int]:
    c = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        return tuple(  # type: ignore[return-value]
            int(c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
            for t in ("aircraft", "flights", "entries")
        )
    finally:
        c.close()


def store_routes(base: str) -> list[str]:
    """Every GET route of the OpenAPI document that reads the store, with its keys filled."""
    status, spec = call(base, "GET", "/openapi.json")
    assert status == 200 and isinstance(spec, dict)
    _, components = call(base, "GET", "/components")
    assert isinstance(components, list) and components
    fill = {"{key}": "SYN-04", "{entry_id}": "5", "{component_id}": str(components[0]["id"])}
    out = []
    for path, ops in spec["paths"].items():
        if "get" not in ops or path in STORELESS:
            continue
        for k, v in fill.items():
            path = path.replace(k, v)
        out.append(path)
    return out


def entry_body() -> bytes:
    return json.dumps(
        {
            "subject": "SYN-03",
            "kind": "time_in_service.set",
            "occurred_utc": "2026-09-01T09:00:00Z",
            "entered_by": "A. Tester, maintenance",
            "statement": "hours read off the aircraft",
            "details": {"before_s": 3600.0},
        }
    ).encode()


def ingest(base: str, key: str) -> tuple[int, dict[str, object] | str]:
    b = "----damaged"
    form = (
        (
            f'--{b}\r\nContent-Disposition: form-data; name="aircraft_key"\r\n\r\n{key}\r\n'
            f'--{b}\r\nContent-Disposition: form-data; name="log"; filename="{LOG.name}"\r\n\r\n'
        ).encode()
        + LOG.read_bytes()
        + f"\r\n--{b}--\r\n".encode()
    )
    return call(base, "POST", "/ingest", form, f"multipart/form-data; boundary={b}")


@pytest.fixture
def live(tmp_path: Path) -> Iterator[Live]:
    env = {k: v for k, v in os.environ.items() if k not in ("UASW_WRITE_TOKEN", "UASW_FIXTURES")}
    db = tmp_path / "fleet.sqlite"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "uas_workbench.service.cli",
            "--db",
            str(db),
            "seed",
            "--fixtures",
            str(FIXTURES),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        check=True,
        timeout=180,
    )
    original = db.read_bytes()
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
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
        ],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=log,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 120
        while True:
            assert p.poll() is None, (tmp_path / "serve.log").read_text()[-600:]
            try:
                urllib.request.urlopen(base + "/health", timeout=2).close()
                break
            except OSError:
                assert time.monotonic() < deadline, "the service never answered"
                time.sleep(0.2)
        yield Live(base, db, original, store_routes(base))
    finally:
        p.terminate()
        p.wait(30)
        log.close()


def everything(live: Live) -> dict[str, tuple[int, dict[str, object] | str]]:
    return {path: call(live.base, "GET", path) for path in live.routes}


def assert_refused(answers: Mapping[str, tuple[int, object]]) -> None:
    wrong = {p: a[0] for p, a in answers.items() if a[0] != 503}
    assert not wrong, f"not refused with 503: {wrong}"
    for path, (_, body) in answers.items():
        assert isinstance(body, dict) and str(body["detail"]).startswith("the store"), (path, body)


def damage_in_place(db: Path, original: bytes, kind: str) -> None:
    with db.open("r+b") as f:
        if kind == "garbage over the header":
            f.write(b"\x00not a database\x00" * 32)
        elif kind == "truncated":
            f.truncate(3000)
        elif kind == "pages overwritten":
            for page in range(2, len(original) // 4096):
                f.seek(page * 4096)
                f.write(bytes(range(256)) * 16)


@pytest.mark.parametrize("kind", ["garbage over the header", "truncated", "pages overwritten"])
def test_a_store_damaged_in_place_is_refused_and_recovers_when_restored(
    live: Live, kind: str
) -> None:
    before = everything(live)
    assert {a[0] for a in before.values()} == {200}, before
    held = counts(live.db)

    damage_in_place(live.db, live.original, kind)
    assert_refused(everything(live))
    assert call(live.base, "GET", "/metrics")[0] == 200  # the process, not the store
    assert call(live.base, "POST", "/entries", entry_body())[0] == 503
    assert ingest(live.base, "NEW-1")[0] == 503

    live.db.write_bytes(live.original)  # restored in place from a good copy
    after = everything(live)
    assert {a[0] for a in after.values()} == {200}, {p: a[0] for p, a in after.items()}
    health = after["/health"][1]
    assert isinstance(health, dict) and health["status"] == "ok"
    assert counts(live.db) == held  # nothing written while it was damaged
    # The kept journal result (in the fleet summary) equals a fresh walk of the restored file.
    fresh = call(live.base, "GET", "/journal/verify")[1]
    summary = after["/fleet/records"][1]
    assert isinstance(fresh, dict) and isinstance(summary, dict)
    assert summary["integrity"]["head"] == fresh["head"]  # type: ignore[index]
    # And it writes again.
    assert call(live.base, "POST", "/entries", entry_body())[0] == 201
    assert ingest(live.base, "NEW-2")[0] == 201
    assert counts(live.db) == (held[0] + 1, held[1] + 1, held[2] + 1)


@pytest.mark.parametrize("kind", ["replaced", "removed"])
def test_a_store_replaced_or_removed_under_the_service_is_refused(live: Live, kind: str) -> None:
    held = counts(live.db)
    try:
        if kind == "replaced":
            other = live.db.with_name("other.sqlite")
            other.write_bytes(live.original)
            os.replace(other, live.db)
        else:
            os.remove(live.db)
    except PermissionError:
        # Windows: a file the service holds open cannot be replaced or removed. The service
        # still answers whole, and writes go on.
        assert sys.platform == "win32"
        answers = everything(live)
        assert {a[0] for a in answers.values()} == {200}, answers
        assert call(live.base, "POST", "/entries", entry_body())[0] == 201
        return
    assert_refused(everything(live))
    assert call(live.base, "POST", "/entries", entry_body())[0] == 503
    # Still refused after a copy is put back: a replaced store needs the service restarted.
    if kind == "removed":
        live.db.write_bytes(live.original)
    assert_refused(everything(live))
    if kind == "replaced":
        assert counts(live.db) == held


def test_a_store_made_read_only_refuses_writes_with_nothing_written(live: Live) -> None:
    """Made read-only (on POSIX, unreadable too) while the service runs: no 500 anywhere;
    a write is refused (503) with nothing written; reads answer 200 where the file can still
    be read and 503 where it cannot."""
    held = counts(live.db)
    os.chmod(live.db, 0 if sys.platform != "win32" else stat.S_IREAD)
    try:
        answers = everything(live)
        assert not {p: a[0] for p, a in answers.items() if a[0] not in (200, 503)}, answers
        assert call(live.base, "POST", "/entries", entry_body())[0] == 503
        assert ingest(live.base, "NEW-3")[0] == 503
    finally:
        os.chmod(live.db, stat.S_IREAD | stat.S_IWRITE)
    assert counts(live.db) == held
    answers = everything(live)
    assert {a[0] for a in answers.values()} == {200}, {p: a[0] for p, a in answers.items()}
    assert call(live.base, "POST", "/entries", entry_body())[0] == 201


def test_a_reader_meets_the_damage_without_waiting_for_a_write_and_kept_answers_follow(
    tmp_path: Path,
) -> None:
    """Through the store itself: while another thread holds the write lock, a read on a
    damaged file is refused at once (no read takes the write lock); restored, the kept
    answers equal answers computed afresh."""
    import importlib
    import threading

    from uas_workbench.fleet import load_config
    from uas_workbench.fleet.synthetic import generate
    from uas_workbench.ledger.project import project
    from uas_workbench.service.store import Store

    store_module = importlib.import_module("uas_workbench.service.store")
    db = tmp_path / "fleet.sqlite"
    seeding = Store(str(db))
    seeding.add_fleet(generate(load_config()))
    seeding.close()
    original = db.read_bytes()
    copy = tmp_path / "copy.sqlite"
    copy.write_bytes(original)
    other_store = Store(str(copy))
    held_entries = other_store.entry_count()
    first = other_store.entries()[0]
    other_store.append_entry(type(first)(**{**first.__dict__, "id": None, "statement": "copy"}))
    other_store.close()
    other = copy.read_bytes()
    store = Store(str(db))
    try:
        assert store.readable() is None and store.journal_state().ok
        store.entries()  # this thread's connection now holds pages of the file as it was
        inside, release = threading.Event(), threading.Event()

        def hold() -> None:
            with store.writing():
                inside.set()
                release.wait(30)

        holder = threading.Thread(None, hold)
        holder.start()
        assert inside.wait(30)
        damage_in_place(db, original, "garbage over the header")
        outcome: list[object] = []

        def read() -> None:
            try:
                outcome.append(store.readable())
            except Exception as exc:
                outcome.append(exc)

        reader = threading.Thread(None, read)
        reader.start()
        reader.join(10)
        release.set()
        holder.join(30)
        assert not reader.is_alive(), "a read waited for the write lock"
        assert len(outcome) == 1 and isinstance(outcome[0], store_module.StoreDamaged), outcome

        # Restored with its time of last change set back to the damaged file's (the same size
        # too): it looks unchanged, as a restore within one clock tick of the damage does, and
        # only the store's once-a-second second look finds it.
        damaged_at = os.stat(db).st_mtime_ns
        db.write_bytes(original)
        os.utime(db, ns=(damaged_at, damaged_at))
        deadline = time.monotonic() + 5
        while True:
            try:
                assert store.readable() is None
                break
            except store_module.StoreDamaged:
                assert time.monotonic() < deadline, "not recovered after the restore"
                time.sleep(0.1)
        assert store.journal_state() == store.verify_journal()
        assert store.projection() == project(store.entries())
        before = store.journal_state()

        # Then a different good copy put in place (one more entry): nothing kept from the
        # file before, and no page a connection held from it, is served.
        db.write_bytes(other)
        after = store.journal_state()
        assert after == store.verify_journal()
        assert after.length == before.length + 1 and after.head != before.head
        assert store.entry_count() == len(store.entries()) == held_entries + 1
        assert store.projection() == project(store.entries())
    finally:
        store.close()


def test_no_read_or_write_passes_while_a_changed_file_is_being_checked(tmp_path: Path) -> None:
    """While one thread checks a changed file, every other read and write waits for its
    verdict: a warm reading thread and a warm writing connection, which hold pages of the file
    as it was, neither answer from the damaged file nor write onto it. And a check that takes
    longer than the second between second looks is run once, not once per waiting request."""
    import importlib
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from uas_workbench.fleet import load_config
    from uas_workbench.fleet.synthetic import generate
    from uas_workbench.service.store import Store

    store_module = importlib.import_module("uas_workbench.service.store")
    db = tmp_path / "fleet.sqlite"
    seeding = Store(str(db))
    seeding.add_fleet(generate(load_config()))
    seeding.close()
    original = db.read_bytes()
    store = Store(str(db))
    pool = ThreadPoolExecutor(1)
    others = ThreadPoolExecutor(4)
    try:
        first = store.entries()[0]

        def one(statement: str) -> object:
            return store.append_entry(
                type(first)(**{**first.__dict__, "id": None, "statement": statement})
            ).id

        one("warm")  # the writing connection holds the pages a write needs
        pool.submit(lambda: (store.entries(), store.aircraft())).result(30)
        checks: list[int] = []
        checking, go = threading.Event(), threading.Event()
        real = store._damage_in

        def held(now: object) -> str | None:
            checks.append(1)
            checking.set()
            go.wait(30)
            return real(now)  # type: ignore[arg-type]

        store._damage_in = held  # type: ignore[method-assign]

        def outcome(fn: object) -> object:
            try:
                return fn()  # type: ignore[operator]
            except Exception as exc:
                return exc

        damage_in_place(db, original, "pages overwritten")
        checker = others.submit(outcome, store.readable)
        assert checking.wait(30)
        read = pool.submit(outcome, lambda: (store.entry_count(), store.aircraft()))
        write = others.submit(outcome, lambda: one("during the check"))
        time.sleep(0.5)
        waited = not read.done() and not write.done()
        go.set()
        answers = [f.result(30) for f in (checker, read, write)]
        assert all(isinstance(a, store_module.StoreDamaged) for a in answers), answers
        assert waited, "a read or a write went on while the changed file was being checked"
        assert len(checks) == 1

        # Damaged and due its second look, with a check that takes longer than the second:
        # four requests that meet it get the one verdict.
        checks.clear()
        checking.clear()
        go.clear()
        time.sleep(1.1)
        waiting = [others.submit(outcome, store.entry_count) for _ in range(4)]
        assert checking.wait(30)
        time.sleep(1.2)
        go.set()
        assert all(isinstance(f.result(30), store_module.StoreDamaged) for f in waiting)
        assert len(checks) == 1, f"{len(checks)} checks for one second look"
    finally:
        go.set()
        pool.shutdown()
        others.shutdown()
        store.close()
