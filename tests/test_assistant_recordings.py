"""Every committed recorded run is re-executed against the current code, with no model.

The recorded model turns are replayed; the tool calls run again against the seeded fleet
at the recording's fixed computation date, and their results must equal what the model saw
when it answered. A change to the engine therefore fails here instead of leaving a stale
answer on the public page. The answer itself must pass the grounding check.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from uas_workbench.assistant.recording import Recording, load_recordings, replay
from uas_workbench.fleet import load_config
from uas_workbench.fleet.showcase import showcase
from uas_workbench.fleet.synthetic import generate
from uas_workbench.service.app import create_app
from uas_workbench.service.store import Store

FIXTURES = Path(__file__).parent / "fixtures"
RECORDINGS = load_recordings()
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


def test_there_are_recorded_runs_and_they_cover_the_required_questions() -> None:
    assert len(RECORDINGS) >= 4
    questions = " ".join(r.question.lower() for r in RECORDINGS)
    assert "safe to fly" in questions  # the certification refusal
    assert "before" in questions  # the honest limit on projecting hours and cycles


@pytest.mark.parametrize("recording", RECORDINGS, ids=[r.slug for r in RECORDINGS])
def test_recording_is_labelled(recording: Recording) -> None:
    assert recording.synthetic is True
    assert re.fullmatch(r"[a-z0-9.-]+:[a-z0-9.-]+", recording.model_tag), recording.model_tag
    assert re.fullmatch(r"[0-9a-f]{12,64}", recording.model_digest), recording.model_digest
    assert recording.recorded_utc.tzinfo is not None
    assert recording.as_of.tzinfo is not None
    assert recording.calls, "a run that used no tool is not evidence"


@pytest.mark.parametrize("recording", RECORDINGS, ids=[r.slug for r in RECORDINGS])
def test_recording_replays_identically_on_current_code(
    recording: Recording, caller: Caller
) -> None:
    answer = replay(recording, caller)
    assert [c.name for c in answer.calls] == [c.name for c in recording.calls]
    for now, then in zip(answer.calls, recording.calls, strict=True):
        assert (now.path, now.query) == (then.path, then.query)
        assert now.result == then.result, f"{then.name} returns something else today"
    assert answer.text == recording.answer
    assert answer.grounding.verified, answer.grounding.unsupported
    assert recording.grounding_verified is True


@pytest.mark.parametrize("recording", RECORDINGS, ids=[r.slug for r in RECORDINGS])
def test_recorded_answers_do_not_certify_and_do_not_project_usage(recording: Recording) -> None:
    text = recording.answer.lower()
    if "safe to fly" in recording.question.lower():
        assert "does not certify" in text or "cannot certify" in text
    if "before" in recording.question.lower():
        assert "cannot predict" in text or "cannot be predicted" in text or "depends on" in text
