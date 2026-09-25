"""The assistant's tools map one-to-one onto the service's GET endpoints and nothing else."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.routing import APIRoute

from uas_workbench.assistant.tools import TOOLS, UnknownTool, as_ollama_tools, resolve
from uas_workbench.service.app import create_app
from uas_workbench.service.store import Store

AS_OF = datetime(2026, 10, 1, tzinfo=UTC)


def routes() -> dict[str, set[str]]:
    app = create_app(Store(":memory:"))
    out: dict[str, set[str]] = {}
    for r in app.routes:
        if isinstance(r, APIRoute):
            out.setdefault(r.path, set()).update(r.methods)
    return out


def test_every_tool_is_a_get_endpoint_of_the_service_and_no_endpoint_writes() -> None:
    by_path = routes()
    for tool in TOOLS:
        assert tool.path in by_path, tool.name
        assert by_path[tool.path] == {"GET"}, tool.name
    assert "/ingest" not in {t.path for t in TOOLS}
    assert {t.name for t in TOOLS} >= {
        "list_aircraft",
        "aircraft_due",
        "fleet_due",
        "reconcile_aircraft",
        "list_flights",
    }


def test_resolve_builds_the_path_and_pins_the_computation_date() -> None:
    path, query = resolve("aircraft_due", {"key": "SYN-04"}, AS_OF)
    assert path == "/aircraft/SYN-04/due"
    assert query == {"as_of": "2026-10-01T00:00:00Z"}
    path, query = resolve("fleet_due", {}, AS_OF)
    assert (path, query) == ("/fleet/due", {"as_of": "2026-10-01T00:00:00Z"})
    path, query = resolve("list_flights", {"key": "alfa-fixed-wing"}, AS_OF)
    assert (path, query) == ("/aircraft/alfa-fixed-wing/flights", {})
    # The model cannot move the date: a supplied as_of is ignored in favour of the pinned one.
    _, query = resolve("aircraft_due", {"key": "SYN-04", "as_of": "1999-01-01"}, AS_OF)
    assert query == {"as_of": "2026-10-01T00:00:00Z"}


def test_resolve_rejects_unknown_tools_and_bad_keys() -> None:
    with pytest.raises(UnknownTool):
        resolve("ingest", {}, AS_OF)
    with pytest.raises(ValueError):
        resolve("aircraft_due", {}, AS_OF)  # key is required
    with pytest.raises(ValueError):
        resolve("aircraft_due", {"key": "../metrics"}, AS_OF)  # no path tricks


def test_ollama_tool_schema_names_every_tool_with_its_parameters() -> None:
    schema = as_ollama_tools()
    assert [s["function"]["name"] for s in schema] == [t.name for t in TOOLS]
    for s in schema:
        assert s["type"] == "function"
        assert s["function"]["description"]
        assert s["function"]["parameters"]["type"] == "object"
    due = next(s for s in schema if s["function"]["name"] == "aircraft_due")
    assert due["function"]["parameters"]["required"] == ["key"]
    assert "as_of" not in due["function"]["parameters"]["properties"]
