"""JSON shape of an entry, shared by storage, the API, the command line and the export."""

from __future__ import annotations

from collections.abc import Callable
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
        "details": dict(e.details),
        "supersedes": e.supersedes,
        "reason": e.reason,
        "synthetic": e.synthetic,
    }


def entry_from_json(data: JsonDict) -> Entry:
    """The entry a stored record holds. A field missing raises KeyError naming it; one of
    the wrong type raises ValueError naming it, so the store can say which."""
    if not isinstance(data, dict):
        raise TypeError(f"the record is a JSON {type(data).__name__}, not an object")
    return Entry(
        id=_field(data, "id", lambda v: int(v) if v is not None else None, optional=True),
        subject=_field(data, "subject", str),
        kind=_field(data, "kind", str),
        occurred_utc=_field(data, "occurred_utc", datetime.fromisoformat),
        recorded_utc=_field(data, "recorded_utc", datetime.fromisoformat),
        entered_by=_field(data, "entered_by", str),
        statement=_field(data, "statement", str),
        details=_field(data, "details", lambda v: dict(v or {}), optional=True),
        supersedes=_field(
            data, "supersedes", lambda v: int(v) if v is not None else None, optional=True
        ),
        reason=_field(data, "reason", lambda v: str(v) if v is not None else None, optional=True),
        synthetic=_field(data, "synthetic", bool),
    )


def _field[T](data: JsonDict, name: str, convert: Callable[[Any], T], optional: bool = False) -> T:
    value = data.get(name) if optional else data[name]
    try:
        return convert(value)
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError(f"field {name!r}: {exc}") from exc
