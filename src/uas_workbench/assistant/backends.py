"""Model backends behind one interface: a local Ollama model, and a replay of recorded turns."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.request import Request, urlopen

LOCALHOST = "http://127.0.0.1:11434"


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class Turn:
    """One model turn: either text, or tool calls to make before the model speaks again."""

    content: str
    tool_calls: tuple[ToolCall, ...]


class Backend(Protocol):
    tag: str
    digest: str

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Turn: ...


Http = Callable[[str, dict[str, Any] | None], Any]


def json_http(url: str, body: dict[str, Any] | None, timeout: float = 600.0) -> Any:
    """POST `body` as JSON (GET when None) and parse the JSON reply. Standard library only."""
    data = json.dumps(body).encode() if body is not None else None
    request = Request(url, data=data, headers={"Content-Type": "application/json"})
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


class OllamaBackend:
    """A model served by Ollama on this machine, deterministic: temperature 0, seed 0, no
    thinking. The digest of the model weights is read once so a recording can name it."""

    def __init__(self, model: str, *, host: str = LOCALHOST, http: Http = json_http) -> None:
        self.tag = model
        self._host = host.rstrip("/")
        self._http = http
        tags = self._http(f"{self._host}/api/tags", None)
        digests = {m["name"]: str(m.get("digest", "")) for m in tags.get("models", [])}
        if model not in digests:
            raise LookupError(f"model {model!r} is not pulled; Ollama has {sorted(digests)}")
        self.digest = digests[model]

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Turn:
        reply = self._http(
            f"{self._host}/api/chat",
            {
                "model": self.tag,
                "messages": messages,
                "tools": tools,
                "stream": False,
                "think": False,
                "options": {"temperature": 0, "seed": 0},
            },
        )
        message = reply.get("message", {})
        calls = tuple(
            ToolCall(str(c["function"]["name"]), dict(c["function"].get("arguments") or {}))
            for c in message.get("tool_calls") or []
        )
        return Turn(str(message.get("content", "")), calls)


class ReplayBackend:
    """Returns recorded turns in order, and keeps every message list it was shown."""

    tag = "replay:recorded"
    digest = "0" * 12

    def __init__(self, turns: Sequence[Turn], tag: str | None = None, digest: str | None = None):
        self._turns = list(turns)
        self._next = 0
        self.seen: list[list[dict[str, Any]]] = []
        if tag:
            self.tag = tag
        if digest:
            self.digest = digest

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Turn:
        self.seen.append([dict(m) for m in messages])
        if self._next >= len(self._turns):
            raise RuntimeError("the recording has no more turns")
        turn = self._turns[self._next]
        self._next += 1
        return turn

    @property
    def exhausted(self) -> bool:
        return self._next == len(self._turns)
