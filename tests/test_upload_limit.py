"""A log over the size limit is refused on POST /ingest and `uasw ingest`, and an upload is
never held whole in memory (issue #89).

`POST /ingest` read the whole upload into memory to copy it to a temporary file, and neither
the route nor the command refused a log for its size. FastAPI parses the multipart body (and
Starlette spools a file part to disk) before the route's dependencies run, so a size check in
the handler would come after the whole body was received, and so did the write check: an
upload without the token was spooled whole before its 401.

The tests here run against a live `uasw serve` where the defect shows on the wire (a refusal
that waits for a body never sent, a peak in the server's own memory read from the operating
system) and in process where the answer alone is the contract. Each live service writes its
temporary files to a folder of its own, which must be empty once a request is answered.
"""

from __future__ import annotations

import contextlib
import errno
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from uas_workbench.service.app import create_app
from uas_workbench.service.cli import main
from uas_workbench.service.store import Store

FIXTURES = Path(__file__).parent / "fixtures"
ALFA = sorted((FIXTURES / "alfa").glob("*.bin"))
TOKEN = "issue-89-token"
BOUNDARY = "----issue89"
LIMIT_MIB = 1
LIMIT = LIMIT_MIB << 20
# Read past the limit before a body is refused: the form's own fields and framing, which a
# file at the limit arrives with (FORM_ALLOWANCE in service/limits.py).
ALLOWANCE = 1 << 20
DETAIL = (
    "the log is larger than the 1 MiB limit for one log, so it is not read; "
    "UASW_MAX_LOG_MIB sets the limit"
)
ANSWER_WITHIN_S = 20


def form_head(key: str, filename: str) -> bytes:
    return (
        f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="aircraft_key"\r\n\r\n{key}\r\n'
        f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="log"; filename="{filename}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode()


FORM_TAIL = f"\r\n--{BOUNDARY}--\r\n".encode()


def request_head(length: int | None, token: str | None = TOKEN) -> bytes:
    """POST /ingest's request line and headers: a declared length, or none (chunked)."""
    lines = [
        "POST /ingest HTTP/1.1",
        "Host: 127.0.0.1",
        f"Content-Type: multipart/form-data; boundary={BOUNDARY}",
    ]
    if token:
        lines.append(f"Authorization: Bearer {token}")
    lines.append(
        f"Content-Length: {length}" if length is not None else "Transfer-Encoding: chunked"
    )
    return ("\r\n".join(lines) + "\r\n\r\n").encode()


def chunk(data: bytes) -> bytes:
    return b"%x\r\n%s\r\n" % (len(data), data)


def read_answer(s: socket.socket) -> tuple[int, str]:
    """The status and body of the one answer on `s`; fails the test if none comes in time."""
    data = b""
    try:
        while b"\r\n\r\n" not in data:
            got = s.recv(65536)
            if not got:
                break
            data += got
        head, _, body = data.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        length = next(
            int(line.split(":", 1)[1])
            for line in lines
            if line.lower().startswith("content-length:")
        )
        while len(body) < length:
            got = s.recv(65536)
            if not got:
                break
            body += got
    except TimeoutError:
        pytest.fail(f"no answer within {ANSWER_WITHIN_S} s: the service waited for the body")
    return int(lines[0].split()[1]), body.decode()


def connect(port: int) -> socket.socket:
    return socket.create_connection(("127.0.0.1", port), timeout=ANSWER_WITHIN_S)


@dataclass
class Live:
    port: int
    temp: Path
    process: subprocess.Popen[bytes]
    log: Path

    def get(self, path: str) -> int:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=30) as r:
                return int(r.status)
        except urllib.error.HTTPError as e:
            with e:
                return e.code

    def temp_left(self) -> list[str]:
        """The service's temporary files once the request is over; a file part rolled to disk
        is closed (and so deleted) by the form's cleanup, which may run just after the answer.
        On Linux a spooled part has no name at all; the route's own copy has one everywhere."""
        deadline = time.monotonic() + 30
        while True:
            left = sorted(p.name for p in self.temp.iterdir())
            if not left or time.monotonic() > deadline:
                return left
            time.sleep(0.1)


def serve(folder: Path, **settings: str) -> Iterator[Live]:
    folder.mkdir()
    temp = folder / "temp"
    temp.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith("UASW_")}
    env.update(TEMP=str(temp), TMP=str(temp), TMPDIR=str(temp), UASW_WRITE_TOKEN=TOKEN)
    env.update(settings)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    log = folder / "serve.log"
    with log.open("w") as err:
        p = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uas_workbench.service.cli",
                "--db",
                str(folder / "fleet.sqlite"),
                "serve",
                "--port",
                str(port),
                "--fixtures",
                str(FIXTURES),
            ],
            cwd=folder,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=err,
        )
    live = Live(port, temp, p, log)
    try:
        deadline = time.monotonic() + 120
        while True:
            assert p.poll() is None, log.read_text()[-800:]
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2).close()
                break
            except OSError:
                assert time.monotonic() < deadline, "the service never answered"
                time.sleep(0.2)
        yield live
    finally:
        p.terminate()
        p.wait(30)


@pytest.fixture(scope="module")
def limited(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Live]:
    """A service with a 1 MiB limit."""
    yield from serve(tmp_path_factory.mktemp("limit") / "served", UASW_MAX_LOG_MIB=str(LIMIT_MIB))


@pytest.fixture(scope="module")
def default(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Live]:
    """A service with the default limit."""
    yield from serve(tmp_path_factory.mktemp("default") / "served")


# --- the live service --------------------------------------------------------------------


def test_a_declared_length_over_the_limit_is_refused_before_any_body_is_sent(
    limited: Live,
) -> None:
    head = form_head("over-declared", "big.bin")
    length = len(head) + LIMIT + ALLOWANCE + len(FORM_TAIL)
    with connect(limited.port) as s:
        s.sendall(request_head(length))  # the headers only: no byte of the body follows
        status, body = read_answer(s)
    assert (status, body) == (413, f'{{"detail":"{DETAIL}"}}')
    assert limited.get("/aircraft/over-declared") == 404
    assert limited.temp_left() == []


def test_a_body_with_no_declared_length_is_refused_once_it_passes_the_limit(
    limited: Live,
) -> None:
    head = form_head("over-chunked", "big.bin")
    # One byte past what the service reads, then nothing more and no closing chunk: the
    # refusal must come from counting, and every byte sent is read, so the service closes
    # the connection cleanly after its answer.
    rest = LIMIT + ALLOWANCE + 1 - len(head)
    with connect(limited.port) as s:
        s.sendall(request_head(None) + chunk(head))
        for start in range(0, rest, 256 << 10):
            s.sendall(chunk(bytes(min(256 << 10, rest - start))))
        status, body = read_answer(s)
    assert (status, body) == (413, f'{{"detail":"{DETAIL}"}}')
    assert limited.get("/aircraft/over-chunked") == 404
    assert limited.temp_left() == []


def test_a_declared_length_cannot_carry_more_than_it_declares(limited: Live) -> None:
    head = form_head("understated", "big.bin")
    with connect(limited.port) as s:
        s.sendall(request_head(len(head) + 1000))
        # The service may close the connection once its declared body is read.
        with contextlib.suppress(OSError):
            s.sendall(head + bytes(LIMIT + ALLOWANCE + 1) + FORM_TAIL)
        status, body = read_answer(s)
    # Only the declared bytes are read: a file part cut short, so no log. Had the bytes past
    # the declared length been handed on, the count would have passed the limit: 413.
    assert (status, body) == (
        422,
        '{"detail":[{"type":"missing","loc":["body","log"],"msg":"Field required","input":null}]}',
    )
    assert limited.get("/aircraft/understated") == 404
    assert limited.get("/health") == 200
    assert limited.temp_left() == []


def test_the_http_server_hands_on_no_more_than_a_body_declares() -> None:
    """UploadGuard counts what the HTTP server hands it, so a body longer than it declares
    relies on the server stopping at the declared length; held here for the installed
    uvicorn, not assumed."""
    import asyncio

    import uvicorn

    async def counting(scope: Any, receive: Any, send: Any) -> None:
        assert scope["type"] == "http"
        total, more = 0, True
        while more:
            message = await receive()
            total += len(message.get("body", b""))
            more = message.get("more_body", False)
        text = str(total).encode()
        headers = [(b"content-length", str(len(text)).encode())]
        await send({"type": "http.response.start", "status": 200, "headers": headers})
        await send({"type": "http.response.body", "body": text})

    server = uvicorn.Server(
        uvicorn.Config(counting, host="127.0.0.1", port=0, lifespan="off", log_config=None)
    )
    thread = threading.Thread(None, asyncio.run, args=(server.serve(),), daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 30
        while not server.started:
            assert time.monotonic() < deadline, "uvicorn never started"
            time.sleep(0.05)
        port = server.servers[0].sockets[0].getsockname()[1]
        with connect(port) as s:
            s.sendall(b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: 1000\r\n\r\n")
            with contextlib.suppress(OSError):
                s.sendall(bytes(1000 + LIMIT))
            status, body = read_answer(s)
        # The app was handed the 1000 bytes declared; the rest is read as the next request,
        # which the server refuses (400) on the same connection, if it arrived in time.
        handed, _, after = body.partition("HTTP/1.1 ")
        assert (status, handed) == (200, "1000")
        assert after[:3] in ("", "400"), after
    finally:
        server.should_exit = True
        thread.join(30)


def test_an_upload_without_the_token_is_refused_before_its_body_is_read(limited: Live) -> None:
    head = form_head("no-token", "small.bin")
    with connect(limited.port) as s:
        s.sendall(request_head(len(head) + LIMIT // 2 + len(FORM_TAIL), token=None))
        status, body = read_answer(s)
    assert status == 401, body
    assert "UASW_WRITE_TOKEN" in body
    assert limited.get("/aircraft/no-token") == 404
    assert limited.temp_left() == []


def test_an_entry_without_the_token_is_refused_before_its_body_is_read(limited: Live) -> None:
    """FastAPI reads a JSON body whole into memory before the write check runs: 300 MiB sent
    to POST /entries without the token raised the service's peak memory about 950 MB before
    its 401. The headers alone are answered now."""
    with connect(limited.port) as s:
        s.sendall(
            b"POST /entries HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n"
            b"Content-Length: %d\r\n\r\n" % (300 << 20)
        )
        status, body = read_answer(s)
    assert status == 401, body
    assert "UASW_WRITE_TOKEN" in body


def test_every_write_that_takes_a_body_is_checked_before_its_body_is_read() -> None:
    from fastapi.routing import APIRoute

    from uas_workbench.service.app import WRITE_BODIES

    app = create_app(Store(":memory:"), write_token=TOKEN)
    writes = {
        route.path
        for route in app.routes
        if isinstance(route, APIRoute)
        and route.body_field is not None
        and any(
            d.call is not None and d.call.__name__ == "writer" for d in route.dependant.dependencies
        )
    }
    assert writes == WRITE_BODIES


def test_a_client_that_goes_away_mid_upload_leaves_no_temporary_file(limited: Live) -> None:
    head = form_head("gone", "half.bin")
    with connect(limited.port) as s:
        s.sendall(request_head(len(head) + LIMIT + len(FORM_TAIL)) + head)
        s.sendall(bytes(LIMIT * 3 // 4))  # past the 1 MB Starlette keeps in memory
    assert limited.get("/aircraft/gone") == 404
    assert limited.temp_left() == []


def test_an_accepted_log_leaves_no_temporary_file(limited: Live) -> None:
    head = form_head("accepted-01", ALFA[0].name)
    data = ALFA[0].read_bytes()
    with connect(limited.port) as s:
        s.sendall(request_head(len(head) + len(data) + len(FORM_TAIL)) + head + data + FORM_TAIL)
        status, body = read_answer(s)
    assert status == 201, body
    assert limited.get("/aircraft/accepted-01") == 200
    assert limited.temp_left() == []


def server_pid(p: subprocess.Popen[bytes]) -> int:
    """The process that serves: on Windows a virtual environment's python.exe is a launcher
    that runs the interpreter as its child, which holds the memory."""
    if sys.platform != "win32":
        return p.pid
    import ctypes
    from ctypes import wintypes

    class Entry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_char * 260),
        ]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    entry = Entry()
    entry.dwSize = ctypes.sizeof(Entry)
    children: list[int] = []
    more = k32.Process32First(wintypes.HANDLE(snap), ctypes.byref(entry))
    while more:
        if entry.th32ParentProcessID == p.pid:
            children.append(int(entry.th32ProcessID))
        more = k32.Process32Next(wintypes.HANDLE(snap), ctypes.byref(entry))
    k32.CloseHandle(wintypes.HANDLE(snap))
    return children[0] if children else p.pid


def peak_memory(pid: int) -> int:
    """The process's peak memory in bytes, from the operating system: peak private bytes on
    Windows, peak resident set (VmHWM) on Linux."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        assert handle, ctypes.get_last_error()
        counters = Counters()
        counters.cb = ctypes.sizeof(Counters)
        try:
            ok = k32.K32GetProcessMemoryInfo(
                wintypes.HANDLE(handle), ctypes.byref(counters), counters.cb
            )
            assert ok, ctypes.get_last_error()
        finally:
            k32.CloseHandle(wintypes.HANDLE(handle))
        return int(counters.PeakPagefileUsage)
    status = Path(f"/proc/{pid}/status").read_text()
    return int(next(ln.split()[1] for ln in status.splitlines() if ln.startswith("VmHWM:"))) * 1024


def test_an_upload_is_copied_without_being_held_in_memory(default: Live, tmp_path: Path) -> None:
    """128 MiB named .ulg: the ULog reader refuses it on its header, so the server's peak
    memory shows the upload's own handling. Held whole, it rose by the upload's size (about
    134 MB here); copied in pieces it rises by a few MB. Half the upload is the margin."""
    size = 128 << 20
    pid = server_pid(default.process)
    before = peak_memory(pid)
    # A launcher's own peak is a few MB; the interpreter that serves holds far more.
    assert before > 40 << 20, f"measured a process of {before} bytes, not the service"
    head = form_head("held", "zeros.ulg")
    with connect(default.port) as s:
        s.settimeout(120)
        s.sendall(request_head(len(head) + size + len(FORM_TAIL)) + head)
        piece = bytes(1 << 20)
        for _ in range(size >> 20):
            s.sendall(piece)
        s.sendall(FORM_TAIL)
        status, body = read_answer(s)
    assert status == 422, body
    rise = peak_memory(pid) - before
    assert rise < size // 2, (
        f"peak memory rose {rise / 1e6:.0f} MB for a {size / 1e6:.0f} MB upload"
    )
    assert default.temp_left() == []


# --- in process: the answers -----------------------------------------------------------------


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[TestClient]:
    """A loopback writer of a service with a 1 MiB limit, its temporary files in tmp_path/temp."""
    temp = tmp_path / "temp"
    temp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp))
    monkeypatch.setenv("UASW_MAX_LOG_MIB", str(LIMIT_MIB))
    monkeypatch.delenv("UASW_WRITE_TOKEN", raising=False)
    yield TestClient(create_app(Store(":memory:"), write_token=None), client=("127.0.0.1", 1))


def upload(client: TestClient, key: str, name: str, data: bytes) -> Any:
    return client.post(
        "/ingest",
        data={"aircraft_key": key},
        files={"log": (name, data, "application/octet-stream")},
    )


@pytest.mark.parametrize(
    "size",
    [LIMIT + 1, LIMIT + ALLOWANCE + 1],
    ids=["within-the-form-allowance", "past-the-form-allowance"],
)
def test_a_log_over_the_limit_is_refused_with_nothing_stored(
    client: TestClient, tmp_path: Path, size: int
) -> None:
    response = upload(client, "over-01", "big.bin", bytes(size))
    assert (response.status_code, response.json()) == (413, {"detail": DETAIL})
    assert client.get("/aircraft/over-01").status_code == 404
    assert sorted((tmp_path / "temp").iterdir()) == []


def test_a_log_at_the_limit_is_read(client: TestClient) -> None:
    response = upload(client, "at-01", "at.bin", bytes(LIMIT))
    assert response.status_code == 422  # read, and refused as no log: not for its size
    assert response.json()["detail"].startswith("not a readable")


def test_the_command_refuses_a_log_over_the_limit_with_the_same_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("UASW_MAX_LOG_MIB", str(LIMIT_MIB))
    big = tmp_path / "big.bin"
    big.write_bytes(bytes(LIMIT + 1))
    db = tmp_path / "fleet.sqlite"
    with pytest.raises(SystemExit) as exit_:
        main(["--db", str(db), "ingest", "over-01", str(big)])
    assert exit_.value.code == 1
    assert capsys.readouterr().err.splitlines() == [f"refused (413): {big}: {DETAIL}"]
    assert not db.exists()


def test_the_command_reads_a_log_at_the_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("UASW_MAX_LOG_MIB", str(LIMIT_MIB))
    log = tmp_path / ALFA[0].name
    log.write_bytes(ALFA[0].read_bytes())
    db = tmp_path / "fleet.sqlite"
    main(["--db", str(db), "ingest", "at-01", str(log)])
    assert [f.log_ref for f in Store(str(db)).flights("at-01")] == [log.stem]


@pytest.mark.parametrize("value", ["lots", "0", "-5", "1.5"])
def test_a_limit_that_is_not_a_whole_number_of_mib_is_refused_by_the_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], value: str
) -> None:
    monkeypatch.setenv("UASW_MAX_LOG_MIB", value)
    log = tmp_path / ALFA[0].name
    log.write_bytes(ALFA[0].read_bytes())
    db = tmp_path / "fleet.sqlite"
    with pytest.raises(SystemExit) as exit_:
        main(["--db", str(db), "ingest", "k-01", str(log)])
    assert exit_.value.code == 1
    assert capsys.readouterr().err.splitlines() == [
        f"refused (422): UASW_MAX_LOG_MIB is {value!r}; it takes a whole number of MiB above 0"
    ]
    assert not db.exists()


def test_a_limit_that_is_not_a_whole_number_of_mib_stops_the_service_before_it_starts(
    tmp_path: Path,
) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("UASW_")}
    env["UASW_MAX_LOG_MIB"] = "lots"
    db = tmp_path / "fleet.sqlite"
    p = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uas_workbench.service.cli",
            "--db",
            str(db),
            "serve",
            "--port",
            "0",
        ],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _, err = p.communicate(timeout=60)
    except subprocess.TimeoutExpired:
        p.terminate()
        p.communicate(timeout=30)
        pytest.fail("the service started with a limit it cannot read")
    assert p.returncode == 1
    assert err.splitlines()[-1] == (
        "refused (422): UASW_MAX_LOG_MIB is 'lots'; it takes a whole number of MiB above 0"
    )
    assert not db.exists()


def test_a_copy_that_fails_leaves_no_temporary_file(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = tempfile.NamedTemporaryFile

    def full_disk(*args: Any, **kwargs: Any) -> Any:
        f = real(*args, **kwargs)

        def write(data: Any) -> int:
            raise OSError(errno.ENOSPC, "No space left on device")

        f.write = write  # type: ignore[method-assign]
        return f

    monkeypatch.setattr(tempfile, "NamedTemporaryFile", full_disk)
    quiet = TestClient(client.app, client=("127.0.0.1", 1), raise_server_exceptions=False)
    response = upload(quiet, "full-01", ALFA[0].name, ALFA[0].read_bytes())
    left = sorted(p.name for p in (tmp_path / "temp").iterdir())
    assert (response.status_code, response.text, left) == (
        503,
        '{"detail":"the upload could not be copied to a temporary file: No space left on device"}',
        [],
    )
    assert client.get("/aircraft/full-01").status_code == 404


def test_two_uploads_at_once_are_each_refused_on_their_own(client: TestClient) -> None:
    answers: list[int] = []

    def one(key: str) -> None:
        answers.append(upload(client, key, "big.bin", bytes(LIMIT + ALLOWANCE + 1)).status_code)

    threads = [threading.Thread(None, one, args=(f"pair-{i}",)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert answers == [413, 413]
