"""Recorded runs: a question, the model's turns, the calls with their records, the answer.

A recording names the model tag and the digest of its weights, when it was recorded and
the fixed computation date, and states that it ran on the synthetic fleet. Replaying one
feeds the recorded model turns back through the agent, so every tool call runs again on
the current code; the test suite requires the records and the answer to come out the same.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from importlib import resources
from pathlib import Path
from typing import Any

from .agent import Answer, Call, Caller, ask
from .backends import ReplayBackend, ToolCall, Turn

NOTICE = (
    "Recorded runs of the assistant, not live: each was recorded once with a local open "
    "model against the seeded synthetic fleet at a fixed computation date, and is replayed "
    "here. The model phrases; every number, state and date comes from the workbench's own "
    "endpoints, listed under each answer with the records returned. A test re-runs every "
    "recording against the current code."
)


@dataclass(frozen=True)
class Recording:
    slug: str
    question: str
    answer: str
    as_of: datetime
    recorded_utc: datetime
    model_tag: str
    model_digest: str
    synthetic: bool
    turns: tuple[Turn, ...]
    calls: tuple[Call, ...]
    grounding_verified: bool
    grounding_unsupported: tuple[str, ...]


def slugify(question: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", question.lower()).strip("-")[:60]


def from_answer(answer: Answer, recorded_utc: datetime) -> Recording:
    return Recording(
        slug=slugify(answer.question),
        question=answer.question,
        answer=answer.text,
        as_of=answer.as_of,
        recorded_utc=recorded_utc,
        model_tag=answer.model_tag,
        model_digest=answer.model_digest,
        synthetic=True,
        turns=tuple(answer.turns),
        calls=answer.calls,
        grounding_verified=answer.grounding.verified,
        grounding_unsupported=answer.grounding.unsupported,
    )


def to_json(r: Recording) -> dict[str, Any]:
    return {
        "slug": r.slug,
        "question": r.question,
        "answer": r.answer,
        "as_of": r.as_of.isoformat(),
        "recorded_utc": r.recorded_utc.isoformat(),
        "model_tag": r.model_tag,
        "model_digest": r.model_digest,
        "synthetic": r.synthetic,
        "fleet": "seeded synthetic fleet plus the two real showcase excerpts",
        "turns": [
            {
                "content": t.content,
                "tool_calls": [{"name": c.name, "arguments": c.arguments} for c in t.tool_calls],
            }
            for t in r.turns
        ],
        "calls": [
            {
                "name": c.name,
                "arguments": c.arguments,
                "path": c.path,
                "query": c.query,
                "result": c.result,
            }
            for c in r.calls
        ],
        "grounding_verified": r.grounding_verified,
        "grounding_unsupported": list(r.grounding_unsupported),
    }


def from_json(data: dict[str, Any]) -> Recording:
    return Recording(
        slug=str(data["slug"]),
        question=str(data["question"]),
        answer=str(data["answer"]),
        as_of=datetime.fromisoformat(data["as_of"]),
        recorded_utc=datetime.fromisoformat(data["recorded_utc"]),
        model_tag=str(data["model_tag"]),
        model_digest=str(data["model_digest"]),
        synthetic=bool(data["synthetic"]),
        turns=tuple(
            Turn(
                str(t["content"]),
                tuple(ToolCall(str(c["name"]), dict(c["arguments"])) for c in t["tool_calls"]),
            )
            for t in data["turns"]
        ),
        calls=tuple(
            Call(
                str(c["name"]), dict(c["arguments"]), str(c["path"]), dict(c["query"]), c["result"]
            )
            for c in data["calls"]
        ),
        grounding_verified=bool(data["grounding_verified"]),
        grounding_unsupported=tuple(str(x) for x in data.get("grounding_unsupported", [])),
    )


def save(r: Recording, path: Path) -> None:
    path.write_text(json.dumps(to_json(r), indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def load_recordings() -> list[Recording]:
    """The recordings shipped in this package, in file order."""
    folder = resources.files("uas_workbench.assistant").joinpath("recordings")
    out: list[Recording] = []
    for entry in sorted(folder.iterdir(), key=lambda e: e.name):
        if entry.name.endswith(".json"):
            out.append(from_json(json.loads(entry.read_text("utf-8"))))
    return out


def replay(r: Recording, caller: Caller) -> Answer:
    backend = ReplayBackend(r.turns, tag=r.model_tag, digest=r.model_digest)
    return ask(r.question, backend=backend, caller=caller, as_of=r.as_of)
