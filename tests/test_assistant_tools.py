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
            out.setdefault(r.path, set()).update(r.methods or ())
    return out


def test_every_tool_is_a_get_of_an_existing_route_and_no_tool_can_write() -> None:
    by_path = routes()
    for tool in TOOLS:
        assert tool.path in by_path, tool.name
        assert tool.method == "GET" and "GET" in by_path[tool.path], tool.name
    # Routes that only write are never tool paths; /entries is shared with a POST, and the
    # tool layer can only ever issue GET: resolve() returns a path and a query, nothing else.
    write_only = {p for p, methods in by_path.items() if "GET" not in methods}
    assert "/ingest" in write_only
    assert {t.path for t in TOOLS}.isdisjoint(write_only)
    assert {t.name for t in TOOLS} >= {
        "list_aircraft",
        "aircraft_due",
        "fleet_due",
        "reconcile_aircraft",
        "list_flights",
        "ledger_entries",
    }


def test_ledger_tool_reads_history_with_superseded_entries_flagged() -> None:
    path, query = resolve("ledger_entries", {"aircraft": "SYN-04"}, AS_OF)
    assert (path, query) == ("/entries", {"aircraft": "SYN-04", "include_superseded": "true"})
    path, query = resolve("ledger_entries", {"component": "BAT-04A"}, AS_OF)
    assert (path, query) == ("/entries", {"subject": "BAT-04A", "include_superseded": "true"})
    assert resolve("ledger_entries", {}, AS_OF) == ("/entries", {"include_superseded": "true"})
    # Whatever the model supplies, superseded entries are always fetched and flagged, so the
    # grounding check can see what superseded what; and no other argument gets through.
    _, query = resolve(
        "ledger_entries", {"aircraft": "SYN-04", "include_superseded": "false"}, AS_OF
    )
    assert query["include_superseded"] == "true"
    _, query = resolve("ledger_entries", {"aircraft": "SYN-04", "method": "POST"}, AS_OF)
    assert set(query) == {"aircraft", "include_superseded"}
    with pytest.raises(ValueError):
        resolve("ledger_entries", {"aircraft": "../metrics"}, AS_OF)


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
    ledger = next(s for s in schema if s["function"]["name"] == "ledger_entries")
    assert set(ledger["function"]["parameters"]["properties"]) == {"aircraft", "component"}
    assert ledger["function"]["parameters"]["required"] == []
    assert "superseded" in ledger["function"]["description"]
