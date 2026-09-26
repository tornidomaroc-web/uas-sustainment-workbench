"""The agent loop and the grounding check.

The model phrases; it never computes. Every limit, state and date comes from the service,
fetched through the read-only tools, at a computation date the caller pins. The answer
carries every call it made with the records returned, and the grounding check marks it
unverified if it states a number, an id or a date that none of those records contain.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .backends import Backend, ToolCall
from .tools import UnknownTool, as_ollama_tools, resolve

Caller = Callable[[str, dict[str, str]], Any]

SYSTEM_PROMPT = """You are the assistant of a civil unmanned-aircraft maintenance workbench. \
You answer questions about the fleet's readiness, component life, inspections and flight \
records using only the tools. Rules:
1. Never compute, estimate or extrapolate. Every number, date, limit and state you state must \
come from a tool result. If a tool result does not contain it, say so.
2. Hours and cycles advance only when the aircraft flies, which cannot be predicted; only \
calendar limits have a date. If the question asks about a date, report what the records show \
as of the computation date, then end with this exact sentence: "Hours and cycles items \
cannot be predicted because they advance only with flight; only calendar limits have a date."
3. The workbench does not certify airworthiness and neither do you. When asked whether an \
aircraft is safe or fit to fly, first fetch its board state with aircraft_due and report the \
state with its reasons, then say that the workbench does not certify airworthiness.
4. Use the aircraft keys, component ids and wording of the tool results. Quote the reason \
sentences the service gives.
5. Answer in plain sentences, briefly. No markdown: no asterisks, no numbered or bulleted \
lists, no headings, no tables. Say when data is synthetic if the result says so.
6. Questions about who recorded what, when, or what was corrected are answered from \
ledger_entries. Name entries as "entry #<id>", quote entered_by as recorded, and give the \
occurred and recorded dates. An entry whose superseded_by is set is not current: if you \
mention it, say it was superseded by entry #<superseded_by> and quote that entry's reason. \
The ledger holds no signature, certificate number or verification of the person: when asked \
for one, say the ledger does not record it. End every answer that used ledger_entries with \
this exact sentence: "Entries record what the entering person stated; the workbench does \
not certify airworthiness or return to service.\""""

MAX_STEPS = 6
NUMBER = re.compile(r"(?<![\w.-])-?\d+(?:\.\d+)?(?![\w.])")
DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}(?!\d)")  # also inside 2026-08-07T09:00:00Z
IDENT = re.compile(r"\b[A-Z]{2,6}(?:-[A-Z0-9]+)+\b")  # SYN-04, BAT-04A, WO-SYN-05-1, WO-59
ENTRY_REF = re.compile(r"(?:\bentry\s*#?\s*|#)(\d+)\b", re.I)
PERSON = re.compile(r"\b[A-Z]\. ?[A-Z][a-z]+\b")  # an initial and a surname, as people type
WORK_STATES = {
    "awaiting_parts": ("awaiting parts", "awaiting_parts"),
    "in_work": ("in work", "in_work"),
    "deferred": ("deferred",),
}
LEDGER_TOOL = "ledger_entries"
LEDGER_NOTE = "does not certify airworthiness or return to service"
FREE_COUNT = 10  # small integers may be counts the model made while phrasing


class StepLimit(Exception):
    pass


@dataclass(frozen=True)
class Call:
    name: str
    arguments: dict[str, Any]
    path: str
    query: dict[str, str]
    result: Any


@dataclass(frozen=True)
class Grounding:
    verified: bool
    unsupported: tuple[str, ...]


@dataclass(frozen=True)
class Answer:
    question: str
    text: str
    calls: tuple[Call, ...]
    grounding: Grounding
    steps: int
    as_of: datetime
    model_tag: str
    model_digest: str
    turns: tuple[Any, ...]  # the model's turns, so the run can be recorded and replayed


def _record_tokens(calls: tuple[Call, ...]) -> tuple[set[str], set[str], str]:
    """Numbers (with one-decimal and integer roundings), dates and the raw text of the records."""
    text = json.dumps(
        [[c.path, c.arguments, c.result] for c in calls], ensure_ascii=False
    )  # the path and arguments name the aircraft the model asked about
    numbers: set[str] = set()
    for m in NUMBER.finditer(text):
        raw = m.group(0).lstrip("-")
        numbers.add(raw)
        value = float(raw)
        numbers.add(f"{value:.1f}")
        numbers.add(f"{value:.0f}")
        numbers.add(f"{round(value, 1):g}")
    return numbers, set(DATE.findall(text)), text


def _entries(calls: tuple[Call, ...]) -> list[dict[str, Any]]:
    return [
        e
        for c in calls
        if c.name == LEDGER_TOOL and isinstance(c.result, list)
        for e in c.result
        if isinstance(e, dict) and "id" in e
    ]


def _ledger_checks(text: str, calls: tuple[Call, ...], raw: str) -> set[str]:
    """Entry ids, people, work order states, and the two rules every ledger answer obeys."""
    unsupported: set[str] = set()
    entries = _entries(calls)
    if not entries:
        return unsupported  # every rule here is about ledger vocabulary
    by_id = {int(e["id"]): e for e in entries}
    mentioned = {int(m.group(1)) for m in ENTRY_REF.finditer(text)}
    for entry_id in mentioned:
        if entry_id not in by_id:
            unsupported.add(f"#{entry_id}")
            continue
        successor = by_id[entry_id].get("superseded_by")
        if successor is not None and int(successor) not in mentioned:
            unsupported.add(
                f"entry #{entry_id} is superseded by #{successor}, which the answer does not say"
            )
    for person in PERSON.findall(text):
        if person not in raw:
            unsupported.add(person)
    lower = text.lower()
    for state, phrases in WORK_STATES.items():
        if any(p in lower for p in phrases) and state not in raw:
            unsupported.add(phrases[0])
    if LEDGER_NOTE not in text:
        unsupported.add("the sentence that entries record what the person stated is missing")
    return unsupported


def ground(text: str, calls: tuple[Call, ...]) -> Grounding:
    numbers, dates, raw = _record_tokens(calls)
    unsupported: set[str] = set()
    for date in DATE.findall(text):
        if date not in dates:
            unsupported.add(date)
    without_dates = DATE.sub(" ", text)
    # With ledger entries in play, "entry #12" is checked as an id below, not as a number.
    without_refs = ENTRY_REF.sub(" ", without_dates) if _entries(calls) else without_dates
    for m in NUMBER.finditer(without_refs):
        token = m.group(0).lstrip("-")
        if "." not in token and int(token) <= FREE_COUNT:
            continue
        if token not in numbers:
            unsupported.add(token)
    for ident in IDENT.findall(text):
        if ident not in raw:
            unsupported.add(ident)
    unsupported |= _ledger_checks(text, calls, raw)
    return Grounding(not unsupported, tuple(sorted(unsupported)))


def _tool_message(name: str, content: str) -> dict[str, Any]:
    return {"role": "tool", "tool_name": name, "content": content}


def ask(
    question: str,
    *,
    backend: Backend,
    caller: Caller,
    as_of: datetime,
    max_steps: int = MAX_STEPS,
) -> Answer:
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]
    tools = as_ollama_tools()
    calls: list[Call] = []
    turns: list[Any] = []
    for step in range(1, max_steps + 1):
        turn = backend.chat(messages, tools)
        turns.append(turn)
        if not turn.tool_calls:
            return Answer(
                question,
                turn.content.strip(),
                tuple(calls),
                ground(turn.content, tuple(calls)),
                step,
                as_of,
                backend.tag,
                backend.digest,
                tuple(turns),
            )
        messages.append(
            {
                "role": "assistant",
                "content": turn.content,
                "tool_calls": [
                    {"function": {"name": c.name, "arguments": c.arguments}}
                    for c in turn.tool_calls
                ],
            }
        )
        for c in turn.tool_calls:
            messages.append(_tool_message(c.name, _execute(c, caller, as_of, calls)))
    raise StepLimit(f"no answer after {max_steps} steps")


def _execute(c: ToolCall, caller: Caller, as_of: datetime, calls: list[Call]) -> str:
    try:
        path, query = resolve(c.name, c.arguments, as_of)
    except UnknownTool as exc:
        return f"unknown tool: {exc}"
    except ValueError as exc:
        return f"invalid arguments: {exc}"
    result = caller(path, query)
    calls.append(Call(c.name, dict(c.arguments), path, query, result))
    return json.dumps(result, ensure_ascii=False)
