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
    assert len(RECORDINGS) >= 9
    questions = " ".join(r.question.lower() for r in RECORDINGS)
    assert "safe to fly" in questions  # the certification refusal
    assert "before" in questions  # the honest limit on projecting hours and cycles
    assert "who recorded" in questions  # the ledger: who, when
    assert "superseded" in questions  # the ledger: what superseded what, and why
    assert "certificate" in questions  # a question the ledger cannot answer
    first_five = [
        "what-is-due-on-syn-04-before",
        "which-aircraft-are-not-serviceable",
        "why-does-syn-03-carry-a-deferred-defect",
        "what-flight-do-the-logs-miss-on-the-real-alfa",
        "is-syn-07-safe-to-fly",
    ]  # the first five runs are kept in place, not substituted
    for r, prefix in zip(RECORDINGS[:5], first_five, strict=True):
        assert r.slug.startswith(prefix), r.slug


@pytest.mark.parametrize("recording", RECORDINGS, ids=[r.slug for r in RECORDINGS])
def test_answers_drawing_on_the_ledger_carry_the_note_and_name_no_real_person(
    recording: Recording,
) -> None:
    used_ledger = any(c.name == "ledger_entries" for c in recording.calls)
    if used_ledger:
        assert "does not certify airworthiness or return to service" in recording.answer
        people = {
            str(e["entered_by"])
            for c in recording.calls
            if c.name == "ledger_entries"
            for e in c.result
        }
        assert all("synthetic" in p for p in people), people
    if "certificate" in recording.question.lower():
        # The honest answer, judged without the closing note sentence.
        body = recording.answer.lower().split("entries record what")[0]
        assert "certificate" in body
        assert "no " in body or "not " in body or "did not" in body


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
