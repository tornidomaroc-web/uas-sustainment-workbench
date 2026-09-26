"""The assistant's tools: one per read-only endpoint of the service, nothing else.

The model chooses which of these to call and phrases what comes back. It cannot upload,
change a record, or move the computation date: every tool is a GET, resolve() returns a
path and a query and nothing else, and `as_of` is pinned by the caller and injected here,
whatever the model supplies.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
METHOD = "GET"


class UnknownTool(Exception):
    pass


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    path: str  # the service route, with {key} where an aircraft key goes
    key_required: bool
    dated: bool  # takes as_of, which the caller pins
    filters: tuple[str, ...] = ()  # optional query filters the model may set
    method: str = METHOD


TOOLS: tuple[Tool, ...] = (
    Tool(
        "list_aircraft",
        "Every aircraft in the fleet with its computed board state, the reasons for that "
        "state, and its counts of overdue and due-soon items, flights and findings.",
        "/aircraft",
        key_required=False,
        dated=False,
    ),
    Tool(
        "aircraft_due",
        "The maintenance due list of one aircraft: every life limit and inspection with what "
        "is used, the limit, what remains, its state (ok, due_soon, overdue_within_tolerance, "
        "overdue), the public source of the limit, and the board state with its reasons.",
        "/aircraft/{key}/due",
        key_required=True,
        dated=True,
    ),
    Tool(
        "fleet_due",
        "Everything due soon or overdue across the whole fleet, worst first.",
        "/fleet/due",
        key_required=False,
        dated=True,
    ),
    Tool(
        "reconcile_aircraft",
        "For one aircraft, the flight time the logs do not cover, found by comparing "
        "consecutive logs with the autopilot's own lifetime counter: findings, coverage "
        "between logs, and logs that could not be checked with the reason.",
        "/aircraft/{key}/reconcile",
        key_required=True,
        dated=False,
    ),
    Tool(
        "list_flights",
        "The flight records of one aircraft: log reference, UTC start, flight time, arm "
        "cycles, landings, battery use, fault events; values a log cannot give are unknown "
        "with the reason.",
        "/aircraft/{key}/flights",
        key_required=True,
        dated=False,
    ),
    Tool(
        "ledger_entries",
        "The maintenance ledger: what people recorded, never edited. Each entry has an id, "
        "what happened (kind and details), when it occurred, when it was recorded, who entered "
        "it (entered_by, as typed), their statement, and whether a later entry superseded it "
        "(superseded_by) and why (reason on the superseding entry). Filter by aircraft (its own "
        "entries plus component installs and removals on it) or by component id; with neither, "
        "the whole ledger. Superseded entries are included and flagged: never present one as "
        "current.",
        "/entries",
        key_required=False,
        dated=False,
        filters=("aircraft", "component"),
    ),
)
BY_NAME = {t.name: t for t in TOOLS}


def as_ollama_tools() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for t in TOOLS:
        properties: dict[str, Any] = {}
        if t.key_required:
            properties["key"] = {
                "type": "string",
                "description": "The aircraft key exactly as list_aircraft returns it.",
            }
        for f in t.filters:
            properties[f] = {
                "type": "string",
                "description": f"Only entries about this {f}, by its key or id.",
            }
        out.append(
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": {
                        "type": "object",
                        "properties": properties,
                        "required": ["key"] if t.key_required else [],
                    },
                },
            }
        )
    return out


def stamp(as_of: datetime) -> str:
    return as_of.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def resolve(name: str, arguments: dict[str, Any], as_of: datetime) -> tuple[str, dict[str, str]]:
    """The service path and query for a tool call, or UnknownTool / ValueError."""
    tool = BY_NAME.get(name)
    if tool is None:
        raise UnknownTool(f"unknown tool {name!r}; the tools are {', '.join(BY_NAME)}")
    path = tool.path
    query: dict[str, str] = {}
    if tool.key_required:
        key = str(arguments.get("key", "")).strip()
        if not KEY.match(key):
            raise ValueError(f"{name} needs an aircraft key as list_aircraft returns it")
        path = path.replace("{key}", key)
    if tool.name == "ledger_entries":
        aircraft = str(arguments.get("aircraft") or "").strip()
        component = str(arguments.get("component") or "").strip()
        if aircraft:
            if not KEY.match(aircraft):
                raise ValueError("ledger_entries needs an aircraft key as list_aircraft returns it")
            query["aircraft"] = aircraft
        elif component:
            if not KEY.match(component):
                raise ValueError("ledger_entries needs a component id as the ledger returns it")
            query["subject"] = component
        query["include_superseded"] = "true"  # always, so superseded entries arrive flagged
    if tool.dated:
        query["as_of"] = stamp(as_of)
    return path, query
