"""The assistant's tools: one per read-only endpoint of the service, nothing else.

The model chooses which of these to call and phrases what comes back. It cannot upload,
change a record, or move the computation date: `as_of` is pinned by the caller and
injected here, whatever the model supplies.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


class UnknownTool(Exception):
    pass


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    path: str  # the service route, with {key} where an aircraft key goes
    key_required: bool
    dated: bool  # takes as_of, which the caller pins


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
    if tool.key_required:
        key = str(arguments.get("key", "")).strip()
        if not KEY.match(key):
            raise ValueError(f"{name} needs an aircraft key as list_aircraft returns it")
        path = path.replace("{key}", key)
    query = {"as_of": stamp(as_of)} if tool.dated else {}
    return path, query
