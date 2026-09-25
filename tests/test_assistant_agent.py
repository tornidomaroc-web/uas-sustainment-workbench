"""The agent loop, the replay backend and the grounding check, with no model involved."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from uas_workbench.assistant.agent import SYSTEM_PROMPT, StepLimit, ask, ground
from uas_workbench.assistant.backends import ReplayBackend, ToolCall, Turn
from uas_workbench.fleet import load_config
from uas_workbench.fleet.showcase import showcase
from uas_workbench.fleet.synthetic import generate
from uas_workbench.service.app import create_app
from uas_workbench.service.store import Store

FIXTURES = Path(__file__).parent / "fixtures"
AS_OF = datetime(2026, 10, 1, tzinfo=UTC)
Caller = Callable[[str, dict[str, str]], Any]


@pytest.fixture(scope="module")
def caller() -> Iterator[Caller]:
    store = Store(":memory:")
    store.add_fleet(generate(load_config()))
    store.add_fleet(showcase(FIXTURES))
    with TestClient(create_app(store)) as client:

        def call(path: str, query: dict[str, str]) -> Any:
            response = client.get(path, params=query)
            response.raise_for_status()
            return response.json()

        yield call


def test_agent_calls_the_tools_the_model_asks_for_and_stops_at_the_answer(
    caller: Caller,
) -> None:
    backend = ReplayBackend(
        [
            Turn("", (ToolCall("aircraft_due", {"key": "SYN-04"}),)),
            Turn("SYN-04 is unserviceable: BAT-04A is 6 cycles past its 300-cycle life limit.", ()),
        ]
    )
    answer = ask("Why is SYN-04 unserviceable?", backend=backend, caller=caller, as_of=AS_OF)
    assert [c.name for c in answer.calls] == ["aircraft_due"]
    assert answer.calls[0].path == "/aircraft/SYN-04/due"
    assert answer.calls[0].query == {"as_of": "2026-10-01T00:00:00Z"}
    assert answer.calls[0].result["status"] == "unserviceable"
    assert answer.text.startswith("SYN-04 is unserviceable")
    assert answer.grounding.verified and answer.grounding.unsupported == ()
    assert answer.as_of == AS_OF and answer.steps == 2
    assert answer.model_tag == backend.tag and answer.model_digest == backend.digest
    assert backend.exhausted


def test_tool_results_are_fed_back_to_the_model_as_tool_messages(caller: Caller) -> None:
    backend = ReplayBackend(
        [
            Turn("", (ToolCall("list_aircraft", {}),)),
            Turn("Nine aircraft.", ()),
        ]
    )
    ask("How many aircraft?", backend=backend, caller=caller, as_of=AS_OF)
    messages = backend.seen[-1]
    assert messages[0] == {"role": "system", "content": SYSTEM_PROMPT}
    assert messages[1] == {"role": "user", "content": "How many aircraft?"}
    assert messages[2]["role"] == "assistant" and messages[2]["tool_calls"]
    assert messages[3]["role"] == "tool" and "alfa-fixed-wing" in messages[3]["content"]
    assert messages[3]["tool_name"] == "list_aircraft"


def test_the_loop_is_capped(caller: Caller) -> None:
    backend = ReplayBackend([Turn("", (ToolCall("list_aircraft", {}),))] * 10)
    with pytest.raises(StepLimit):
        ask("loop", backend=backend, caller=caller, as_of=AS_OF, max_steps=3)


def test_an_unknown_tool_is_reported_to_the_model_not_executed(caller: Caller) -> None:
    backend = ReplayBackend(
        [
            Turn("", (ToolCall("ingest", {"aircraft_key": "x"}),)),
            Turn("I cannot do that.", ()),
        ]
    )
    answer = ask("upload a log", backend=backend, caller=caller, as_of=AS_OF)
    assert answer.calls == ()
    tool_message = backend.seen[-1][3]
    assert tool_message["role"] == "tool" and "unknown tool" in tool_message["content"]


def test_grounding_rejects_numbers_ids_and_dates_absent_from_the_records(
    caller: Caller,
) -> None:
    backend = ReplayBackend(
        [
            Turn("", (ToolCall("aircraft_due", {"key": "SYN-04"}),)),
            Turn(
                "SYN-04 has 2 packs. BAT-04A is 6 cycles past 300; BAT-04B passed its limit on "
                "2026-03-31. Also BAT-99Z is 42 cycles over and the annual is due 2027-02-28.",
                (),
            ),
        ]
    )
    answer = ask("SYN-04?", backend=backend, caller=caller, as_of=AS_OF)
    assert not answer.grounding.verified
    assert answer.grounding.unsupported == ("2027-02-28", "42", "BAT-99Z")


def test_grounding_rules_on_hand_built_records() -> None:
    from uas_workbench.assistant.agent import Call

    calls = (
        Call(
            "aircraft_due",
            {"key": "SYN-02"},
            "/aircraft/SYN-02/due",
            {},
            {"items": [{"remaining": 8.421, "limit": 300.0, "component_id": "PROP-02"}]},
        ),
    )
    ok = ground("PROP-02 has 8.4 h left of 300 h; 1 item, 3 aircraft checked.", calls)
    assert ok.verified, ok.unsupported  # rounding to one decimal and counts up to 10 are fine
    assert ground("PROP-02 has 8.5 h left.", calls).unsupported == ("8.5",)
    assert ground("Aircraft SYN-02 has 12 items.", calls).unsupported == ("12",)
    assert ground("Nothing to report.", ()).verified
