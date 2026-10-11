"""The draft records summary of the whole fleet: one row per aircraft from the per-aircraft
summary (`build_records`), one integrity line for the whole store, and fleet-level counts.

It is not a second implementation: each row is the per-aircraft summary of that aircraft,
reduced to its section statuses, its span word and a few counts; the title, the statuses,
the span words and the sources are those of `records.py`. The integrity line is the store's
kept journal result, read once per build, never walked per row or per call.

Every row and the integrity line come from one read of the store (`Store.snapshot`): a
write that lands while the summary is built, through the store or from another connection,
reaches no row and not the line; the next build has it.

What cannot be read, at fleet level: the per-aircraft summary leaves what depended on a
record the store cannot read not known, with the record named, and the fleet summary keeps
each row as it is. The board's liveness rule (one entry the store cannot read makes every
projected record unknown, because liveness is decided over the whole ledger) carries over
through the rows themselves: such an entry leaves the maintenance point and the span not
known on every row, while a flight record that cannot be read leaves only its aircraft's
operations and span not known. The counts count each not-known row as not known, never in
another bucket; the first record the store cannot read is named once at fleet level; and the
summary is written, as the per-aircraft one is, where the board answers 503: it is a report
of what the records show, and that a record cannot be read is part of the report.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from uas_workbench import __version__
from uas_workbench.fleet import FleetConfig
from uas_workbench.ledger import UnreadableRecord
from uas_workbench.service.store import Store

from .records import (
    INTEGRITY_NOTE,
    NOT_KNOWN,
    RECORDS_ITEMS,
    RECORDS_SOURCES,
    RECORDS_STATEMENT,
    RECORDS_STATUSES,
    RECORDS_TITLE,
    RETENTION_YEARS,
    SPAN_NOT_KNOWN,
    SPAN_STATUSES,
    _integrity,
    _stamp,
    build_records,
)

FLEET_RECORDS_SECTIONS: tuple[str, ...] = (
    RECORDS_TITLE,
    "Integrity of the records held",
    "Whether every stored record reads",
    "What this workbench holds, point by point, across the fleet",
    "One row per aircraft",
    "Span of the records held against the 3-year minimum",
    "Traceability",
)

FLEET_STATEMENT: tuple[str, ...] = (
    RECORDS_STATEMENT[0],
    "It lists, for every aircraft in this workbench, what records are held against the "
    "record-keeping points of UAS.SPEC.050(1)(g) of Regulation (EU) 2019/947, one row per "
    "aircraft with the same statuses its own summary gives; it does not say that any record "
    "is true, it does not decide whether an obligation is satisfied, and it claims nothing "
    "about any operation; the authority decides what the records show.",
    RECORDS_STATEMENT[2],
    "Every row is the draft records summary of that aircraft, reduced to its statuses and "
    "its span; what the records cannot support is shown as not known with its reason, and a "
    "count of such rows is a count, never a finding.",
)

NOT_KNOWN_RULE = (
    "A row is not known where its own summary is: a stored entry the store cannot read "
    "leaves maintenance activities and the span not known on every row, because liveness "
    "is decided over the whole ledger (the rule the board follows), and a stored flight "
    "record that cannot be read leaves only that aircraft's operations and span not known. "
    "The counts count each such row as not known and in no other bucket; the first record "
    "that cannot be read is named above; and this summary is still written, because that a "
    "record cannot be read is part of what the records show."
)
ROWS_NOTE = (
    "Each row is the draft records summary of that aircraft, reduced: the three statuses, "
    "the span word, the counts of entries and flight records held by the computation date "
    "and the earliest dated record. The reasons behind each status are in the data and in "
    "that aircraft's own summary."
)


def _readable(problem: UnreadableRecord | None) -> dict[str, Any]:
    if problem is None:
        return {
            "ok": True,
            "unreadable": None,
            "line": "every stored entry and flight record reads; nothing in this summary is "
            "not known for want of a record the store cannot read.",
        }
    return {
        "ok": False,
        "unreadable": problem.detail,
        "line": f"a stored record cannot be read: {problem.detail}. What depended on it is "
        "not known on the rows below, with the record named.",
    }


def _row(one: dict[str, Any]) -> dict[str, Any]:
    statuses = {item["id"]: item["status"] for item in one["items"]}
    reasons = {item["id"]: item["reason"] for item in one["items"]}
    retention = one["retention"]
    return {
        "key": one["aircraft"]["key"],
        "label": one["aircraft"]["label"],
        "synthetic": one["aircraft"]["synthetic"],
        "data": one["label"],
        "statuses": statuses,
        "reasons": reasons,
        "span": retention["status"],
        "span_reason": retention["reason"],
        "earliest_utc": retention["earliest_utc"],
        "entries": one["maintenance"]["entries"],
        "flights": one["operations"]["flights"],
        "not_known": NOT_KNOWN in statuses.values() or retention["status"] == SPAN_NOT_KNOWN,
    }


def build_fleet_records(
    store: Store,
    config: FleetConfig,
    *,
    as_of: datetime,
    commit: str | None,
    generated_utc: datetime | None = None,
) -> dict[str, Any]:
    now = generated_utc or datetime.now(UTC).replace(microsecond=0)
    with store.snapshot():
        integrity = _integrity(store.journal_state())
        readable = _readable(store.readable())
        rows = [
            _row(
                build_records(
                    store,
                    aircraft.key,
                    config,
                    as_of=as_of,
                    commit=commit,
                    generated_utc=now,
                )
            )
            for aircraft in store.aircraft()
        ]
    by_point = {
        item["id"]: {
            status: sum(row["statuses"][item["id"]] == status for row in rows)
            for status in RECORDS_STATUSES
        }
        for item in RECORDS_ITEMS
    }
    span = {status: sum(row["span"] == status for row in rows) for status in SPAN_STATUSES}
    return {
        "title": RECORDS_TITLE,
        "scope": "fleet",
        "statement": list(FLEET_STATEMENT),
        "generated_utc": _stamp(now),
        "as_of": _stamp(as_of),
        "commit": commit or "unknown",
        "version": __version__,
        "integrity": integrity,
        "readable": readable,
        "sections": [
            {"number": n, "title": t} for n, t in enumerate(FLEET_RECORDS_SECTIONS, start=1)
        ],
        "sources": list(RECORDS_SOURCES),
        "items": [dict(item) for item in RECORDS_ITEMS],
        "rows": rows,
        "counts": {
            "aircraft": len(rows),
            "synthetic": sum(row["synthetic"] for row in rows),
            "real": sum(not row["synthetic"] for row in rows),
            "by_point": by_point,
            "span": span,
            "rows_not_known": sum(row["not_known"] for row in rows),
        },
        "retention": {"minimum_years": RETENTION_YEARS, "as_of": _stamp(as_of)},
        "not_known_rule": NOT_KNOWN_RULE,
        "rows_note": ROWS_NOTE,
        "integrity_note": INTEGRITY_NOTE,
    }
