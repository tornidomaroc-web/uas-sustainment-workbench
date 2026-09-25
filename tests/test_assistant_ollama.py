"""The Ollama backend's request and response shape, through a fake HTTP transport."""

from __future__ import annotations

import json
from typing import Any

from uas_workbench.assistant import backends
from uas_workbench.assistant.backends import OllamaBackend, ToolCall


class FakeHttp:
    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses
        self.requests: list[tuple[str, Any]] = []

    def __call__(self, url: str, body: dict[str, Any] | None) -> Any:
        self.requests.append((url, body))
        return self.responses[url.rsplit("/", 1)[-1]]


def test_backend_sends_temperature_zero_no_thinking_and_the_tools() -> None:
    http = FakeHttp(
        {
            "tags": {"models": [{"name": "qwen3:8b", "digest": "abc123", "size": 1}]},
            "chat": {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{"function": {"name": "fleet_due", "arguments": {}}}],
                },
                "done": True,
            },
        }
    )
    backend = OllamaBackend("qwen3:8b", host="http://127.0.0.1:11434", http=http)
    assert (backend.tag, backend.digest) == ("qwen3:8b", "abc123")
    turn = backend.chat([{"role": "user", "content": "hi"}], [{"type": "function"}])
    url, body = http.requests[-1]
    assert url == "http://127.0.0.1:11434/api/chat" and body is not None
    assert body["model"] == "qwen3:8b" and body["stream"] is False
    assert body["options"]["temperature"] == 0 and body["options"]["seed"] == 0
    assert body["think"] is False
    assert body["tools"] == [{"type": "function"}]
    assert body["messages"] == [{"role": "user", "content": "hi"}]
    assert turn.tool_calls == (ToolCall("fleet_due", {}),) and turn.content == ""


def test_backend_returns_plain_text_when_the_model_answers() -> None:
    http = FakeHttp(
        {
            "tags": {"models": [{"name": "qwen3:8b", "digest": "abc123"}]},
            "chat": {"message": {"role": "assistant", "content": "Done."}, "done": True},
        }
    )
    backend = OllamaBackend("qwen3:8b", http=http)
    turn = backend.chat([], [])
    assert turn.content == "Done." and turn.tool_calls == ()


def test_backend_only_talks_to_localhost_by_default() -> None:
    http = FakeHttp({"tags": {"models": [{"name": "m", "digest": "d"}]}})
    OllamaBackend("m", http=http)
    assert http.requests[0][0].startswith("http://127.0.0.1:11434/")


def test_json_transport_posts_json(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}

    class Response:
        def __enter__(self) -> Response:
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def read(self) -> bytes:
            return b'{"ok": true}'

    def fake_urlopen(request: Any, timeout: float) -> Response:
        captured["url"] = request.full_url
        captured["data"] = request.data
        captured["content_type"] = request.get_header("Content-type")
        return Response()

    monkeypatch.setattr(backends, "urlopen", fake_urlopen)
    assert backends.json_http("http://127.0.0.1:11434/api/chat", {"a": 1}) == {"ok": True}
    assert json.loads(captured["data"]) == {"a": 1}
    assert captured["content_type"] == "application/json"
