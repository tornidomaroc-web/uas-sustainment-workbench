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
calendar limits have a date. Say this when asked what will be due by a future date.
3. The workbench does not certify airworthiness and neither do you. Report the board state \
and its reasons; never say an aircraft is safe or fit to fly.
4. Use the aircraft keys, component ids and wording of the tool results. Quote the reason \
sentences the service gives.
5. Answer in plain sentences, briefly, with no tables. Say when data is synthetic if the \
result says so."""

MAX_STEPS = 6
NUMBER = re.compile(r"(?<![\w.-])-?\d+(?:\.\d+)?(?![\w.])")
DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
IDENT = re.compile(r"\b[A-Z]{2,6}-\d{2,}[A-Z]?\b")
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


def ground(text: str, calls: tuple[Call, ...]) -> Grounding:
    numbers, dates, raw = _record_tokens(calls)
    unsupported: set[str] = set()
    for date in DATE.findall(text):
        if date not in dates:
            unsupported.add(date)
    without_dates = DATE.sub(" ", text)
    for m in NUMBER.finditer(without_dates):
        token = m.group(0).lstrip("-")
        if "." not in token and int(token) <= FREE_COUNT:
            continue
        if token not in numbers:
            unsupported.add(token)
    for ident in IDENT.findall(text):
        if ident not in raw:
            unsupported.add(ident)
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
