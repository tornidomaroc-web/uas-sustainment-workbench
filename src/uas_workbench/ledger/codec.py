"""JSON shape of an entry, shared by storage, the API, the command line and the export."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .model import Entry

JsonDict = dict[str, Any]


def entry_to_json(e: Entry) -> JsonDict:
    return {
        "id": e.id,
        "subject": e.subject,
        "kind": e.kind,
        "occurred_utc": e.occurred_utc.isoformat(),
        "recorded_utc": e.recorded_utc.isoformat(),
        "entered_by": e.entered_by,
        "statement": e.statement,
        "payload": dict(e.payload),
        "supersedes": e.supersedes,
        "reason": e.reason,
        "synthetic": e.synthetic,
    }


def entry_from_json(data: JsonDict) -> Entry:
    return Entry(
        id=int(data["id"]) if data.get("id") is not None else None,
        subject=str(data["subject"]),
        kind=str(data["kind"]),
        occurred_utc=datetime.fromisoformat(data["occurred_utc"]),
        recorded_utc=datetime.fromisoformat(data["recorded_utc"]),
        entered_by=str(data["entered_by"]),
        statement=str(data["statement"]),
        payload=dict(data.get("payload") or {}),
        supersedes=int(data["supersedes"]) if data.get("supersedes") is not None else None,
        reason=str(data["reason"]) if data.get("reason") is not None else None,
        synthetic=bool(data["synthetic"]),
    )
