"""`uasw ask` ends with one refused line and exit 1 when the service is not running or the
model backend fails, never a traceback (issue #49).

Two paths beside the refused tool call of issue #46 still ended in a traceback. When nothing
listens at `--url`, `urlopen` raises `urllib.error.URLError`, which `service_caller` did not
catch; so does a host that does not resolve, and a connection that times out or is reset
while the service is being read. When the model backend answers an error status, the tag
list request in `OllamaBackend.__init__` or a chat turn raises `HTTPError`, and a backend
that is not running raises `URLError`, neither caught by `cmd_ask` or `main`; a model that is
not pulled ended in a bare `LookupError`.

The contract is the one of issue #46: the run ends where the failure is, so the model is
never handed a failed or partial read to answer from and no answer, grounding or recording
can carry it; the operator sees one `refused (<status>): <sentence>` line on stderr naming
what did not answer or what the backend answered, exit 1, nothing on stdout, and `--record`
writes nothing. Every test runs on recorded turns (`ReplayBackend`) or on a stub in place of
the backend's HTTP; no model runs, and nothing listens on the ports these tests use except
the stub listeners they open themselves on loopback.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError

import pytest
from test_assistant_refusals import AS_OF, AS_OF_TEXT, LISTS_THEN_ANSWERS, uasw_ask

from uas_workbench.assistant import RefusedCall, ReplayBackend, ask
from uas_workbench.assistant.agent import service_caller
from uas_workbench.assistant.backends import OllamaBackend, json_http

BACKEND = "http://127.0.0.1:11434"
TAGS = {"models": [{"name": "m:1", "digest": "abc123"}]}
Listener = Callable[[Callable[[socket.socket], None]], str]


def closed_port() -> int:
    """A loopback port nothing listens on: bound, read and released."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def listener() -> Iterator[Listener]:
    """Starts a loopback listener that hands each accepted connection to `serve` on a
    thread, and returns its base URL; stops after a few connections or at teardown."""
    threads: list[threading.Thread] = []
    socks: list[socket.socket] = []

    def start(serve: Callable[[socket.socket], None]) -> str:
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sock.listen(4)
        sock.settimeout(10)
        socks.append(sock)

        def run() -> None:
            for _ in range(4):
                try:
                    conn, _addr = sock.accept()
                except OSError:
                    return
                with conn:
                    serve(conn)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        threads.append(thread)
        return f"http://127.0.0.1:{sock.getsockname()[1]}"

    yield start
    for sock in socks:
        sock.close()
    for thread in threads:
        thread.join(2)


def _backend_error(status: int, reason: str, url: str = f"{BACKEND}/api/tags") -> HTTPError:
    return HTTPError(url, status, reason, None, BytesIO(b"model backend error"))  # type: ignore[arg-type]


class StubHttp:
    """The backend's HTTP: an answer per endpoint, or an exception to raise for it."""

    def __init__(self, **by_endpoint: Any) -> None:
        self.by_endpoint = by_endpoint
        self.calls: list[str] = []

    def __call__(self, url: str, body: dict[str, Any] | None) -> Any:
        endpoint = url.rsplit("/", 1)[-1]
        self.calls.append(endpoint)
        answer = self.by_endpoint[endpoint]
        if isinstance(answer, BaseException):
            raise answer
        return answer


# ---- the library path: the service does not answer --------------------------------------


def _refused(backend: ReplayBackend, caller: Any) -> RefusedCall:
    with pytest.raises(RefusedCall) as refused:
        ask("How many aircraft?", backend=backend, caller=caller, as_of=AS_OF)
    assert len(backend.seen) == 1, "the model was asked again after the failure"
    assert not backend.exhausted
    return refused.value


def test_a_service_that_is_not_running_is_refused_with_the_url_and_the_reason() -> None:
    url = f"http://127.0.0.1:{closed_port()}"
    exc = _refused(ReplayBackend(LISTS_THEN_ANSWERS), service_caller(url))
    assert exc.status == 503
    assert exc.detail.startswith(f"the service at {url} did not answer: ")
    assert "Connection refused" in exc.detail
    assert exc.tool == "list_aircraft" and exc.path == "/aircraft"
    assert str(exc) == f"refused (503): {exc.detail}"


def test_a_host_that_does_not_resolve_is_refused_the_same_way() -> None:
    url = "http://no-such-host.invalid:9"
    exc = _refused(ReplayBackend(LISTS_THEN_ANSWERS), service_caller(url))
    assert exc.status == 503
    prefix = f"the service at {url} did not answer: "
    assert exc.detail.startswith(prefix) and len(exc.detail) > len(prefix)


def test_a_service_that_accepts_and_never_answers_is_refused_on_the_timeout(
    listener: Listener,
) -> None:
    gate = threading.Event()

    def hold(conn: socket.socket) -> None:
        gate.wait(5)

    url = listener(hold)

    def http(url: str, body: dict[str, Any] | None) -> Any:
        return json_http(url, body, timeout=0.2)

    try:
        exc = _refused(ReplayBackend(LISTS_THEN_ANSWERS), service_caller(url, http=http))
    finally:
        gate.set()
    assert exc.status == 503
    assert exc.detail.startswith(f"the service at {url} did not answer: ")
    assert "timed out" in exc.detail


def test_a_service_that_closes_the_connection_is_refused_on_the_reset(
    listener: Listener,
) -> None:
    def drop(conn: socket.socket) -> None:
        conn.recv(1)

    url = listener(drop)
    exc = _refused(ReplayBackend(LISTS_THEN_ANSWERS), service_caller(url))
    assert exc.status == 503
    assert exc.detail.startswith(f"the service at {url} did not answer: ")


def test_a_service_that_answers_something_that_is_not_http_is_refused(
    listener: Listener,
) -> None:
    def babble(conn: socket.socket) -> None:
        conn.recv(1)
        conn.sendall(b"not http at all\r\n\r\n")

    url = listener(babble)
    exc = _refused(ReplayBackend(LISTS_THEN_ANSWERS), service_caller(url))
    assert exc.status == 503
    assert exc.detail.startswith(f"the service at {url} did not answer: ")


def test_the_url_error_is_converted_where_it_is_raised_not_in_the_agent() -> None:
    """A caller of `service_caller` alone, no agent, gets the typed refusal too."""
    url = f"http://127.0.0.1:{closed_port()}"
    with pytest.raises(RefusedCall) as refused:
        service_caller(url)("/aircraft", {"as_of": AS_OF_TEXT})
    assert refused.value.status == 503 and refused.value.tool == "" and refused.value.path == ""


# ---- the library path: the model backend fails ------------------------------------------


def test_a_backend_error_status_on_the_tag_list_is_refused_naming_the_status() -> None:
    http = StubHttp(tags=_backend_error(500, "Internal Server Error"))
    with pytest.raises(RefusedCall) as refused:
        OllamaBackend("m:1", host=BACKEND, http=http)
    exc = refused.value
    assert exc.status == 502
    assert exc.detail == f"the model backend at {BACKEND} answered 500 Internal Server Error"
    assert str(exc) == f"refused (502): {exc.detail}"


def test_a_backend_error_status_on_a_chat_turn_is_refused_and_nothing_is_answered() -> None:
    error = _backend_error(503, "Service Unavailable", f"{BACKEND}/api/chat")
    http = StubHttp(tags=TAGS, chat=error)
    backend = OllamaBackend("m:1", host=BACKEND, http=http)
    calls: list[str] = []

    def caller(path: str, query: dict[str, str]) -> Any:
        calls.append(path)
        return []

    with pytest.raises(RefusedCall) as refused:
        ask("How many aircraft?", backend=backend, caller=caller, as_of=AS_OF)
    assert refused.value.status == 502
    assert refused.value.detail == (
        f"the model backend at {BACKEND} answered 503 Service Unavailable"
    )
    assert calls == [] and http.calls == ["tags", "chat"]


def test_a_backend_that_is_not_running_is_refused_with_the_host_and_the_reason() -> None:
    http = StubHttp(tags=URLError(ConnectionRefusedError(111, "Connection refused")))
    with pytest.raises(RefusedCall) as refused:
        OllamaBackend("m:1", host=BACKEND, http=http)
    assert refused.value.status == 502
    assert refused.value.detail == (
        f"the model backend at {BACKEND} did not answer: [Errno 111] Connection refused"
    )


RESET = ConnectionResetError(104, "Connection reset by peer")


@pytest.mark.parametrize(
    ("error", "reason"),
    [(TimeoutError("timed out"), "timed out"), (RESET, "[Errno 104] Connection reset by peer")],
    ids=["timeout", "reset"],
)
def test_a_backend_that_times_out_or_resets_on_a_chat_turn_is_refused(
    error: Exception, reason: str
) -> None:
    backend = OllamaBackend("m:1", host=BACKEND, http=StubHttp(tags=TAGS, chat=error))
    with pytest.raises(RefusedCall) as refused:
        backend.chat([], [])
    assert refused.value.status == 502
    assert refused.value.detail == f"the model backend at {BACKEND} did not answer: {reason}"


def test_a_model_that_is_not_pulled_is_refused_with_the_sentence() -> None:
    http = StubHttp(tags={"models": [{"name": "other:1", "digest": "d"}]})
    with pytest.raises(RefusedCall) as refused:
        OllamaBackend("m:1", host=BACKEND, http=http)
    assert refused.value.status == 502
    assert refused.value.detail == "model 'm:1' is not pulled; the backend has ['other:1']"


def test_the_backend_http_error_is_closed() -> None:
    error = _backend_error(500, "Internal Server Error")
    with pytest.raises(RefusedCall):
        OllamaBackend("m:1", host=BACKEND, http=StubHttp(tags=error))
    assert error.closed


# ---- the command line -------------------------------------------------------------------

# `uasw ask` in a real process with the backend's HTTP replaced by a stub that answers the
# tag list with 500; the service is never reached.
BACKEND_500_DRIVER = """
import sys
from io import BytesIO
from urllib.error import HTTPError
import uas_workbench.assistant as assistant
from uas_workbench.assistant.backends import OllamaBackend

def http(url, body):
    raise HTTPError(url, 500, "Internal Server Error", None, BytesIO(b"model backend error"))

assistant.OllamaBackend = lambda model, host: OllamaBackend(model, host=host, http=http)
from uas_workbench.service.cli import main
main(sys.argv[1:])
"""


def test_uasw_ask_refuses_when_the_service_is_not_running(tmp_path: Path) -> None:
    url = f"http://127.0.0.1:{closed_port()}"
    recording = tmp_path / "run.json"
    done = uasw_ask(LISTS_THEN_ANSWERS, "How many aircraft?", "--url", url, "--as-of", AS_OF_TEXT,
                    "--record", str(recording), "--json")  # fmt: skip
    assert done.returncode == 1, done.stderr
    assert done.stderr.startswith(f"refused (503): the service at {url} did not answer: ")
    assert "Connection refused" in done.stderr and done.stderr.count("\n") == 1
    assert "Traceback" not in done.stderr and "URLError" not in done.stderr
    assert done.stdout == ""
    assert not recording.exists()


def test_uasw_ask_refuses_when_the_model_backend_answers_an_error(tmp_path: Path) -> None:
    url = f"http://127.0.0.1:{closed_port()}"
    recording = tmp_path / "run.json"
    done = subprocess.run(
        [sys.executable, "-c", BACKEND_500_DRIVER, "ask", "How many aircraft?", "--url", url,
         "--ollama", BACKEND, "--as-of", AS_OF_TEXT, "--record", str(recording)],
        capture_output=True,
        text=True,
        env=os.environ | {"PYTHONWARNINGS": "ignore"},
        timeout=120,
    )  # fmt: skip
    assert done.returncode == 1, done.stderr
    assert done.stderr == (
        f"refused (502): the model backend at {BACKEND} answered 500 Internal Server Error\n"
    )
    assert "Traceback" not in done.stderr and "HTTPError" not in done.stderr
    assert done.stdout == ""
    assert not recording.exists()
