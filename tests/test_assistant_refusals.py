"""A tool call the service refuses with an error status never ends `uasw ask` in a traceback
(issue #46), and is never shown to the model as data.

Since #45 a read that depends on a stored record the store cannot read answers 503 with the
record named, and every other `uasw` command prints `refused (503): <that sentence>` and exits
1. `uasw ask` did not: the assistant's tool call goes through `json_http`, `urlopen` raises
`urllib.error.HTTPError`, and nothing on the way up caught it, so the operator got a traceback
and the sentence naming the record was lost. 404 (an aircraft key the model made up) took the
same path; 422, 401 and 403 would, if a route the tools reach ever answered one.

The contract: a refused call ends the run. The model is not asked again, so it never holds a
refusal it could phrase as a value or build an answer on; no `Answer` exists, so nothing can
cite the call as grounding and no recording can carry it; the operator sees the service's own
sentence, as every other command shows it. Every test runs the agent on recorded turns
(`ReplayBackend`); no model runs. The 503 tests go through a live `uasw serve` on a crafted
store, with the caller `uasw ask` builds, so the path is the one the issue describes.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest
import uvicorn
from test_unreadable_records import craft_nested_entry, named

from uas_workbench.assistant import RefusedCall, ReplayBackend, ToolCall, Turn, ask
from uas_workbench.assistant.agent import service_caller
from uas_workbench.service.app import create_app
from uas_workbench.service.cli import seed
from uas_workbench.service.store import Store

FIXTURES = Path(__file__).parent / "fixtures"
AS_OF = datetime(2026, 10, 1, tzinfo=UTC)
AS_OF_TEXT = "2026-10-01T00:00:00Z"
# A model that lists the fleet, then would answer with a number: the second turn must never
# be reached when the first call is refused.
LISTS_THEN_ANSWERS = [
    Turn("", (ToolCall("list_aircraft", {}),)),
    Turn("Nine aircraft, and SYN-01 is serviceable.", ()),
]
# A model that asks about an aircraft that is not in the store, then would repeat the
# service's sentence as its own answer.
UNKNOWN_KEY_THEN_ANSWERS = [
    Turn("", (ToolCall("aircraft_due", {"key": "SYN-99"}),)),
    Turn("SYN-99 is not in the store.", ()),
]


@pytest.fixture
def db(tmp_path: Path) -> str:
    path = str(tmp_path / "fleet.sqlite")
    store = Store(path)
    seed(store, FIXTURES)
    store.close()
    return path


def _serve(db: str) -> Iterator[str]:
    """`uasw serve` on the store, in a thread, on a free loopback port."""
    store = Store(db)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(16)
    port = sock.getsockname()[1]
    config = uvicorn.Config(create_app(store), log_config=None, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        thread.join(0.05)
    assert server.started, "the service did not start"
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(10)
        store.close()


@pytest.fixture
def clean_service(db: str) -> Iterator[str]:
    yield from _serve(db)


@pytest.fixture
def crafted_service(db: str) -> Iterator[tuple[str, tuple[str, ...]]]:
    """A live service on the store of issue #43 as filed: entry 4 nested 100,000 deep, so
    every ledger read, `/aircraft` first, answers 503 with the record named."""
    words = craft_nested_entry(db)
    for url in _serve(db):
        yield url, words


def service_detail(url: str, path: str) -> tuple[int, str]:
    """The status and the `detail` sentence the service itself answers for a path."""
    try:
        with urlopen(f"{url}{path}", timeout=30) as response:
            return response.status, json.loads(response.read())["detail"]
    except HTTPError as exc:
        with exc:  # the error holds the response file; closed with it
            return exc.code, json.loads(exc.read())["detail"]


# ---- the library path -------------------------------------------------------------------


def test_a_503_on_a_tool_call_ends_the_run_with_the_record_named(
    crafted_service: tuple[str, tuple[str, ...]],
) -> None:
    url, words = crafted_service
    status, detail = service_detail(url, f"/aircraft?as_of={AS_OF_TEXT}")
    assert status == 503
    named(detail, words)
    backend = ReplayBackend(LISTS_THEN_ANSWERS)
    with pytest.raises(RefusedCall) as refused:
        ask("How many aircraft?", backend=backend, caller=service_caller(url), as_of=AS_OF)
    exc = refused.value
    assert exc.status == 503
    assert exc.detail == detail  # the service's own sentence, whole, not urllib's reason
    assert exc.tool == "list_aircraft" and exc.path == "/aircraft"
    assert str(exc) == f"refused (503): {detail}"
    named(str(exc), words)


def test_the_model_is_not_asked_again_after_a_refusal_so_nothing_can_cite_the_call(
    crafted_service: tuple[str, tuple[str, ...]],
) -> None:
    """The refused call reaches no one as data: the model saw one message list, the one with
    the question, and never a tool message holding the refusal; no answer exists, so no
    grounding check ran over the call and no recording can hold it."""
    url, _ = crafted_service
    backend = ReplayBackend(LISTS_THEN_ANSWERS)
    with pytest.raises(RefusedCall):
        ask("How many aircraft?", backend=backend, caller=service_caller(url), as_of=AS_OF)
    assert len(backend.seen) == 1, "the model was asked again after the refusal"
    assert [m["role"] for m in backend.seen[0]] == ["system", "user"]
    assert not backend.exhausted, "the recorded answer turn was served"
    assert all("cannot be read" not in json.dumps(m) for m in backend.seen[0])


def test_a_404_for_a_key_the_model_made_up_is_refused_the_same_way(clean_service: str) -> None:
    status, detail = service_detail(clean_service, f"/aircraft/SYN-99/due?as_of={AS_OF_TEXT}")
    assert status == 404 and detail == "aircraft 'SYN-99' is not in the store"
    backend = ReplayBackend(UNKNOWN_KEY_THEN_ANSWERS)
    with pytest.raises(RefusedCall) as refused:
        ask("Is SYN-99 due?", backend=backend, caller=service_caller(clean_service), as_of=AS_OF)
    assert refused.value.status == 404 and refused.value.detail == detail
    assert refused.value.tool == "aircraft_due" and refused.value.path == "/aircraft/SYN-99/due"
    assert len(backend.seen) == 1 and not backend.exhausted


def _http_error(status: int, body: bytes | None, reason: str = "reason") -> HTTPError:
    """What `urlopen` raises for an error status, with the body the service sent."""
    return HTTPError("http://127.0.0.1:1/x", status, reason, None, BytesIO(body or b""))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("status", "body", "detail"),
    [
        (401, b'{"detail": "writes need the token set in UASW_WRITE_TOKEN as a bearer token"}',
         "writes need the token set in UASW_WRITE_TOKEN as a bearer token"),
        (403, b'{"detail": "forbidden"}', "forbidden"),
        (404, b'{"detail": "aircraft \'SYN-99\' is not in the store"}',
         "aircraft 'SYN-99' is not in the store"),
        # FastAPI's validation detail is a list of objects: shown as its JSON, not as Python.
        (422, b'{"detail": [{"type": "bool_parsing", "loc": ["query", "include_superseded"], '
              b'"msg": "Input should be a valid boolean"}]}',
         '[{"type": "bool_parsing", "loc": ["query", "include_superseded"], '
         '"msg": "Input should be a valid boolean"}]'),
        # No usable body (a proxy's page, an empty reply): the status line's reason.
        (500, b"<html>Internal Server Error</html>", "Internal Server Error"),
        (503, None, "Service Unavailable"),
    ],
)  # fmt: skip
def test_every_error_status_ends_the_run_with_the_service_sentence(
    status: int, body: bytes | None, detail: str
) -> None:
    reasons = {500: "Internal Server Error", 503: "Service Unavailable"}

    def caller(path: str, query: dict[str, str]) -> Any:
        raise _http_error(status, body, reasons.get(status, "reason"))

    backend = ReplayBackend(LISTS_THEN_ANSWERS)
    with pytest.raises(RefusedCall) as refused:
        ask("How many aircraft?", backend=backend, caller=caller, as_of=AS_OF)
    assert refused.value.status == status
    assert refused.value.detail == detail
    assert str(refused.value) == f"refused ({status}): {detail}"
    assert len(backend.seen) == 1


def test_a_refusal_after_a_successful_call_still_ends_the_run(clean_service: str) -> None:
    """One turn, two calls, the second refused: the run ends; the first call's record is not
    answered on either, since no answer is made."""
    backend = ReplayBackend(
        [
            Turn("", (ToolCall("aircraft_due", {"key": "SYN-04"}),
                      ToolCall("list_flights", {"key": "SYN-99"}))),
            Turn("SYN-04 is unserviceable and SYN-99 has no flights.", ()),
        ]
    )  # fmt: skip
    caller = service_caller(clean_service)
    with pytest.raises(RefusedCall) as refused:
        ask("SYN-04 and SYN-99?", backend=backend, caller=caller, as_of=AS_OF)
    assert refused.value.status == 404 and refused.value.tool == "list_flights"
    assert len(backend.seen) == 1 and not backend.exhausted


def test_a_model_error_is_still_reported_to_the_model_not_the_operator(
    clean_service: str,
) -> None:
    """Unchanged: an unknown tool or a bad argument is the model's mistake, shown to it as a
    tool message so it can correct itself; only the service's refusals end the run."""
    backend = ReplayBackend(
        [
            Turn("", (ToolCall("aircraft_due", {"key": "not a key!"}),)),
            Turn("I need the key as list_aircraft returns it.", ()),
        ]
    )
    answer = ask("due?", backend=backend, caller=service_caller(clean_service), as_of=AS_OF)
    assert answer.calls == ()
    assert "invalid arguments" in backend.seen[-1][3]["content"]


# ---- the command line -------------------------------------------------------------------

# `uasw ask` in a real process, as a user at a shell sees it, with the recorded turns in place
# of Ollama and nothing else of the command changed; the service it talks to is the one the
# test serves on loopback.
DRIVER = """
import json, sys
import uas_workbench.assistant as assistant
from uas_workbench.assistant import ReplayBackend, ToolCall, Turn
turns = [
    Turn(t["content"], tuple(ToolCall(c["name"], c["arguments"]) for c in t["tool_calls"]))
    for t in json.loads(sys.argv[1])
]
assistant.OllamaBackend = lambda model, host: ReplayBackend(turns)
from uas_workbench.service.cli import main
main(sys.argv[2:])
"""


def uasw_ask(turns: list[Turn], *args: str) -> subprocess.CompletedProcess[str]:
    recorded = json.dumps(
        [
            {
                "content": t.content,
                "tool_calls": [{"name": c.name, "arguments": c.arguments} for c in t.tool_calls],
            }
            for t in turns
        ]
    )
    return subprocess.run(
        [sys.executable, "-c", DRIVER, recorded, "ask", *args],
        capture_output=True,
        text=True,
        env=os.environ | {"PYTHONWARNINGS": "ignore"},
        timeout=120,
    )


def test_uasw_ask_prints_the_refusal_and_exits_1_never_a_traceback(
    crafted_service: tuple[str, tuple[str, ...]], tmp_path: Path
) -> None:
    url, words = crafted_service
    _, detail = service_detail(url, f"/aircraft?as_of={AS_OF_TEXT}")
    recording = tmp_path / "run.json"
    done = uasw_ask(LISTS_THEN_ANSWERS, "How many aircraft?", "--url", url, "--as-of", AS_OF_TEXT,
                    "--record", str(recording), "--json")  # fmt: skip
    assert done.returncode == 1, done.stderr
    assert done.stderr == f"refused (503): {detail}\n"  # the line every other command prints
    named(done.stderr, words)
    assert "Traceback" not in done.stderr and "HTTPError" not in done.stderr
    assert done.stdout == ""  # no answer, no call list, no "Grounded:" line, no records
    assert not recording.exists()  # nothing to replay was recorded


def test_uasw_ask_prints_a_404_the_same_way(clean_service: str) -> None:
    done = uasw_ask(UNKNOWN_KEY_THEN_ANSWERS, "Is SYN-99 due?", "--url", clean_service,
                    "--as-of", AS_OF_TEXT)  # fmt: skip
    assert done.returncode == 1, done.stderr
    assert done.stderr == "refused (404): aircraft 'SYN-99' is not in the store\n"
    assert done.stdout == ""


def test_uasw_ask_still_answers_on_a_readable_store(clean_service: str) -> None:
    """The same command, the same caller, a store that reads: the answer as before."""
    turns = [Turn("", (ToolCall("aircraft_due", {"key": "SYN-04"}),)),
             Turn("SYN-04 is unserviceable.", ())]  # fmt: skip
    done = uasw_ask(turns, "SYN-04?", "--url", clean_service, "--as-of", AS_OF_TEXT)
    assert done.returncode == 0, done.stderr
    assert done.stderr == ""
    assert done.stdout.startswith(
        "SYN-04 is unserviceable.\n\nCalls (1): GET /aircraft/SYN-04/due\n"
    )
    assert "Grounded: yes" in done.stdout


def test_the_converted_http_error_is_closed() -> None:
    """`service_caller` turns urllib's `HTTPError` into `RefusedCall` after reading its body;
    the error holds the response file, and one left open is a `ResourceWarning` on 3.13+
    (issue #48)."""
    error = _http_error(404, b'{"detail": "aircraft \'SYN-99\' is not in the store"}')

    def http(url: str, body: Any) -> Any:
        raise error

    caller = service_caller("http://127.0.0.1:1", http=http)
    with pytest.raises(RefusedCall) as refused:
        caller("/aircraft/SYN-99/due", {})
    assert refused.value.detail == "aircraft 'SYN-99' is not in the store"
    assert error.closed
