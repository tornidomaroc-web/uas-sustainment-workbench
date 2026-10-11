"""The draft records summary of one aircraft: what this workbench holds against the record
keeping points of UAS.SPEC.050(1)(g) of Regulation (EU) 2019/947, with the write journal's
head and verify result in it.

It gathers; it does not find. Each point is marked with one of four statuses, which say
whether the workbench holds records that bear on it, never whether an obligation is met or a
record is true. The span of the records held is set against the 3-year minimum the point
names, as covered, not yet 3 years or not known. A record the store cannot read leaves what
depended on it not known, with the record named; nothing is computed without it.

The verify result comes from `Store.journal_state()`, the last walk of the journal kept by
the store and moved on by its own writes, so a caller who needs no token to read this pack
cannot make the service walk the whole journal on every request; `GET /journal/verify` and
`uasw verify`, which walk it on demand, stay guarded as the writes are.

Every read the summary makes, the projection and the verify result included, is inside one
`Store.snapshot()`: one state of the store, which holds no write lock. A write landing while
the summary is built is in the next one, never in half of this one.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from uas_workbench import __version__
from uas_workbench.fleet import FleetConfig
from uas_workbench.fleet.model import Aircraft
from uas_workbench.flight.record import FlightRecord, is_known
from uas_workbench.ledger import NOTE, Entry, UnreadableRecord
from uas_workbench.service.app import (
    due_view,
    entries_about,
    flight_view,
    flights_until,
    grounded_view,
    reconcile_view,
)
from uas_workbench.service.journal import Verification
from uas_workbench.service.store import Store

RECORDS_TITLE = "Draft records summary for the operator's own review"
RECORDS_STATUSES: tuple[str, ...] = (
    "supported by these records",
    "partly",
    "not evidenced by this workbench",
    "not known",
)
SUPPORTED, PARTLY, NOT_EVIDENCED, NOT_KNOWN = RECORDS_STATUSES
SPAN_STATUSES: tuple[str, ...] = ("covered", "not yet 3 years", "not known")
COVERED, NOT_YET, SPAN_NOT_KNOWN = SPAN_STATUSES
RETENTION_YEARS = 3

RECORDS_STATEMENT: tuple[str, ...] = (
    "This is a draft records summary, gathered by a software tool for the operator's own "
    "review before anything is sent to an authority.",
    "It lists what records this workbench holds for one aircraft against the record-keeping "
    "points of UAS.SPEC.050(1)(g) of Regulation (EU) 2019/947; it does not say that any record "
    "is true, it does not decide whether an obligation is satisfied, and it claims nothing "
    "about the operation; the authority decides what the records show.",
    "It does not certify airworthiness or return to service, and no record in it does: each "
    "entry records what the entering person stated.",
    "Every value comes from the flight logs, the maintenance ledger and the write journal; "
    "what the records cannot support is shown as not known with its reason.",
)

# The one regulatory source, paraphrased. Point (1)(g) was read on EUR-Lex in the consolidated
# text of 1 May 2025, the latest listed when checked (11 October 2026); its wording there is the
# same as in the consolidation of 4 April 2022 that follows (EU) 2020/639. Nothing here is
# quoted.
RECORDS_SOURCES: tuple[dict[str, str], ...] = (
    {
        "id": "eu-2019-947-uas-spec-050",
        "title": "Commission Implementing Regulation (EU) 2019/947 on the rules and procedures "
        "for the operation of unmanned aircraft, Annex, Part B, UAS.SPEC.050 Responsibilities "
        "of the UAS operator, point (1)(g), as amended by Implementing Regulation (EU) 2020/639",
        "document": "Regulation (EU) 2019/947, consolidated",
        "url": "https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:02019R0947-20250501",
        "note": "Paraphrased, not quoted: point (1)(g) asks the UAS operator to keep and "
        "maintain up to date a record of (i) the qualifications and training of the remote "
        "pilots, the other personnel essential to the operation and the maintenance staff, (ii) "
        "the maintenance activities conducted on the UAS, and (iii) the information on UAS "
        "operations, including any unusual technical or operational occurrences and other data "
        "the declaration or the operational authorisation requires, each for a minimum of 3 "
        "years. A reader checks the wording against the consolidated text.",
    },
)

# id, point, paraphrase. The status of each for one aircraft is decided below from what that
# aircraft's records hold.
RECORDS_ITEMS: tuple[dict[str, str], ...] = (
    {
        "id": "training-records",
        "point": "UAS.SPEC.050(1)(g)(i)",
        "text": "A record of the qualifications and training of the remote pilots, of the other "
        "personnel essential to the operation and of the maintenance staff, kept for at least "
        "3 years after they leave or change position.",
    },
    {
        "id": "maintenance-activities",
        "point": "UAS.SPEC.050(1)(g)(ii)",
        "text": "A record of the maintenance activities conducted on the UAS, kept for a "
        "minimum of 3 years.",
    },
    {
        "id": "operations-occurrences",
        "point": "UAS.SPEC.050(1)(g)(iii)",
        "text": "A record of the information on UAS operations, including any unusual "
        "technical or operational occurrences and the other data the declaration or the "
        "operational authorisation requires, kept for a minimum of 3 years.",
    },
)

RECORDS_SECTIONS: tuple[str, ...] = (
    RECORDS_TITLE,
    "Integrity of the records held",
    "The aircraft",
    "What this workbench holds, point by point",
    "Maintenance activities",
    "Operations and unusual occurrences",
    "Span of the records held against the 3-year minimum",
    "Traceability",
)

INTEGRITY_NOTE = (
    "The write journal links every entry and flight record in the store, in the order written, "
    "into one hash chain. Verifying recomputes every hash from the rows as they are now. "
    "A chain that verifies says the records are as they were when linked; it does not say "
    "that what they state is true, and a chain cut short or recomputed with the file verifies "
    "alone and fails only against a head kept outside the file."
)
OCCURRENCE_NOTE = (
    "The workbench holds no occurrence report written by a person: what it shows as unusual "
    "is what the autopilot itself reported as a fault in a log, what the logs and the "
    "autopilot's own counter disagree on, and a log that started while the ledger shows the "
    "aircraft grounded. Each is what the records show, not what happened."
)
SPAN_NOTE = (
    "The span is from the earliest record held for this aircraft, an entry's date or a log's "
    "UTC start, to the computation date; covered means that record is dated 3 calendar years "
    "before the computation date or earlier (a computation date of 29 February counts back to "
    "28 February). It says how far back the records held reach; it "
    "cannot say whether a record was ever removed, and a record that cannot be placed in time "
    "(a log with no UTC start) is counted but gives no date."
)
NO_TRAINING = (
    "the workbench holds no qualification, training or staff record; only records of what "
    "was done to the aircraft and what its logs show"
)


def _stamp(t: datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _unknown(problem: UnreadableRecord) -> dict[str, str]:
    return {"unknown": problem.detail}


def _label(aircraft: Aircraft, entries: Sequence[Entry], problem: UnreadableRecord | None) -> str:
    data = (
        "Synthetic data: this aircraft and its flights are generated"
        if aircraft.synthetic
        else "Real data: public log excerpts"
    )
    if problem is not None:
        return f"{data}; a stored record cannot be read ({problem.name})"
    if aircraft.synthetic:
        if not entries or all(e.synthetic for e in entries):
            return "Synthetic data: this aircraft, its flights and its records are generated"
        return "Mixed data: a generated aircraft and flights, with entries a person recorded"
    if entries:
        return "Real data: public log excerpts, with entries a person recorded"
    return "Real data: public log excerpts; no maintenance record exists for this aircraft"


def years_before(at: datetime, years: int) -> datetime:
    """`at` less `years` calendar years; a 29 February with no counterpart is 28 February."""
    try:
        return at.replace(year=at.year - years)
    except ValueError:
        return at.replace(year=at.year - years, day=28)


def _integrity(state: Verification) -> dict[str, Any]:
    if state.ok:
        line = (
            f"unchanged since head {state.head} ({state.length} link"
            f"{'s' if state.length != 1 else ''}): every entry and flight record in the store "
            "is what its journal link recorded. Unchanged says the records are as they were "
            "when linked, not that they are true."
        )
    else:
        assert state.broken is not None
        line = (
            f"journal broken at link {state.broken.seq}: {state.broken.kind}: "
            f"{state.broken.detail}. From that link on, the records cannot be shown to be as they "
            f"were when linked; the chain is intact up to that link, head there {state.head}."
        )
    return {
        "ok": state.ok,
        "head": state.head,
        "length": state.length,
        "broken": dataclasses.asdict(state.broken) if state.broken else None,
        "migration": dataclasses.asdict(state.migration) if state.migration else None,
        "line": line,
        "note": INTEGRITY_NOTE,
    }


def _retention(
    as_of: datetime,
    earliest: tuple[datetime, str] | None,
    undated: int,
    problem: UnreadableRecord | None,
) -> dict[str, Any]:
    threshold = years_before(as_of, RETENTION_YEARS)
    out: dict[str, Any] = {
        "minimum_years": RETENTION_YEARS,
        "as_of": _stamp(as_of),
        "threshold_utc": _stamp(threshold),
        "earliest_utc": None,
        "earliest_kind": None,
        "days": None,
        "undated_flights": undated,
        "note": SPAN_NOTE,
    }
    if problem is not None:
        out["status"] = SPAN_NOT_KNOWN
        out["reason"] = (
            f"the earliest record cannot be placed: {problem.detail}; so whether the records "
            f"held reach back {RETENTION_YEARS} years is not known"
        )
        return out
    if earliest is None:
        out["status"] = SPAN_NOT_KNOWN
        why = (
            "no record of this aircraft is held at the computation date"
            if undated == 0
            else f"the only records held are {undated} log(s) with no UTC start, which cannot "
            "be placed in time"
        )
        out["reason"] = (
            f"{why}; so whether the records held reach back {RETENTION_YEARS} years is not known"
        )
        return out
    first, kind = earliest
    days = (as_of - first).days
    out.update(earliest_utc=_stamp(first), earliest_kind=kind, days=days)
    reach = (
        f"the earliest record held is {'an entry' if kind == 'entry' else 'a log'} dated "
        f"{_stamp(first)}, {days} day{'s' if days != 1 else ''} before the computation date"
    )
    if first <= threshold:
        out["status"] = COVERED
        out["reason"] = (
            f"{reach}; the records held reach back {RETENTION_YEARS} years or more, to "
            f"{_stamp(threshold)} and beyond"
        )
    else:
        out["status"] = NOT_YET
        out["reason"] = (
            f"{reach}; the records held do not yet reach back {RETENTION_YEARS} years "
            f"(that would be {_stamp(threshold)} or earlier)"
        )
    return out


def build_records(
    store: Store,
    aircraft_key: str,
    config: FleetConfig,
    *,
    as_of: datetime,
    commit: str | None,
    generated_utc: datetime | None = None,
) -> dict[str, Any]:
    with store.snapshot():
        return _build_records(
            store, aircraft_key, config, as_of=as_of, commit=commit, generated_utc=generated_utc
        )


def _build_records(
    store: Store,
    aircraft_key: str,
    config: FleetConfig,
    *,
    as_of: datetime,
    commit: str | None,
    generated_utc: datetime | None,
) -> dict[str, Any]:
    aircraft = store.get_aircraft(aircraft_key)
    if aircraft is None:
        raise KeyError(aircraft_key)
    now = generated_utc or datetime.now(UTC).replace(microsecond=0)
    # Each read that can meet a record the store cannot read is tried on its own, so what
    # that record leaves unknown is said where it would have been shown, and nothing else.
    ledger: UnreadableRecord | None = None
    flights: UnreadableRecord | None = None
    entries: list[Entry] = []
    try:
        entries = [e for e in entries_about(store, aircraft_key) if e.occurred_utc <= as_of]
        dead = store.projection().superseded_by
    except UnreadableRecord as exc:
        ledger = exc
        dead = {}
    records: Sequence[FlightRecord] = []
    reconcile_findings: list[str] = []
    try:
        records = flights_until(store.flights(aircraft_key), as_of)
        result, _ = reconcile_view(store, aircraft, config, as_of)
        reconcile_findings = [f.message for f in result.findings]
    except UnreadableRecord as exc:
        flights = exc
    grounded: dict[str, Any]
    if flights is not None:
        grounded = _unknown(flights)
    else:
        try:
            due_view(store, aircraft, config, as_of)  # the board the logs are judged on
            view = grounded_view(store, aircraft, config, as_of)[1]
            grounded = {
                "judged": view.judged,
                "findings": [f.message for f in view.findings],
                "not_judged": [n.why for n in view.not_judged],
            }
        except UnreadableRecord as exc:
            # The logs read and the board does not: no log is judged, each with that reason.
            grounded = {
                "judged": 0,
                "findings": [],
                "not_judged": [exc.detail for _ in records],
            }

    # Maintenance activities: the ledger about this aircraft, superseded entries kept.
    maintenance: dict[str, Any]
    if ledger is not None:
        maintenance = {
            "entries": _unknown(ledger),
            "superseded": _unknown(ledger),
            "kinds": _unknown(ledger),
            "first_utc": _unknown(ledger),
            "last_utc": _unknown(ledger),
            "entered_by": _unknown(ledger),
        }
        maintenance_status = (NOT_KNOWN, ledger.detail)
    else:
        kinds: dict[str, int] = {}
        for e in entries:
            kinds[e.kind] = kinds.get(e.kind, 0) + 1
        maintenance = {
            "entries": len(entries),
            "superseded": sum(e.id in dead for e in entries),
            "kinds": dict(sorted(kinds.items())),
            "first_utc": _stamp(entries[0].occurred_utc) if entries else None,
            "last_utc": _stamp(entries[-1].occurred_utc) if entries else None,
            "entered_by": sorted({e.entered_by for e in entries}),
        }
        if entries:
            maintenance_status = (
                SUPPORTED,
                f"the ledger holds {len(entries)} entr{'y' if len(entries) == 1 else 'ies'} "
                "about this aircraft by the computation date, each with when it happened, when "
                "it was entered, by whom and in what words, superseded entries kept as history; "
                "the draft evidence pack for OSO #03 shows them in full",
            )
        else:
            maintenance_status = (
                NOT_EVIDENCED,
                "the ledger holds no entry about this aircraft by the computation date",
            )

    # Operations and unusual occurrences: the logs, and what they and the ledger show.
    operations: dict[str, Any]
    if flights is not None:
        operations = {
            key: _unknown(flights)
            for key in (
                "flights",
                "undated_flights",
                "first_utc",
                "last_utc",
                "logged_s",
                "fault_events",
                "reconcile_findings",
                "grounded_findings",
                "not_judged",
            )
        }
        operations["grounded"] = grounded
        operations_status = (NOT_KNOWN, flights.detail)
        undated = 0
        first_flight: datetime | None = None
    else:
        starts = sorted(r.utc_start for r in records if is_known(r.utc_start))
        undated = len(records) - len(starts)
        first_flight = starts[0] if starts else None
        faults: list[dict[str, Any]] = []
        for r in records:
            if is_known(r.fault_events):
                faults.extend(
                    {"log_ref": r.log_ref, "t_s": f.t_s, "kind": f.kind, "detail": f.detail}
                    for f in r.fault_events
                )
        operations = {
            "flights": len(records),
            "undated_flights": undated,
            "first_utc": _stamp(starts[0]) if starts else None,
            "last_utc": _stamp(starts[-1]) if starts else None,
            "logged_s": round(
                sum(r.flight_time_s for r in records if is_known(r.flight_time_s)), 1
            ),
            "fault_events": faults,
            "reconcile_findings": reconcile_findings,
            "grounded_findings": grounded["findings"] if "unknown" not in grounded else [],
            "not_judged": len(grounded["not_judged"]) if "unknown" not in grounded else 0,
            "grounded": grounded,
            "logs": [flight_view(r).model_dump(mode="json") for r in records],
        }
        if records:
            operations_status = (
                PARTLY,
                f"{len(records)} flight record{'s' if len(records) != 1 else ''} from the "
                "aircraft's logs hold the date, duration and what the autopilot reported; "
                f"{len(faults)} fault event{'s' if len(faults) != 1 else ''} reported by the "
                f"autopilot, {len(reconcile_findings)} finding"
                f"{'s' if len(reconcile_findings) != 1 else ''} from reconciling the logs and "
                f"{len(operations['grounded_findings'])} log"
                f"{'s' if len(operations['grounded_findings']) != 1 else ''} started while the "
                "ledger shows the aircraft grounded are shown; no occurrence report written by a "
                "person is held, and the other data a declaration or authorisation requires is "
                "not held",
            )
        else:
            operations_status = (
                NOT_EVIDENCED,
                "no flight record of this aircraft is held by the computation date",
            )

    # The span: the earliest dated record of either kind.
    candidates: list[tuple[datetime, str]] = []
    if entries:
        candidates.append((min(e.occurred_utc for e in entries), "entry"))
    if first_flight is not None:
        candidates.append((first_flight, "flight"))
    earliest = min(candidates) if candidates else None
    retention = _retention(as_of, earliest, undated, ledger or flights)

    statuses = {
        "training-records": (NOT_EVIDENCED, NO_TRAINING),
        "maintenance-activities": maintenance_status,
        "operations-occurrences": operations_status,
    }
    items = []
    for item in RECORDS_ITEMS:
        status, reason = statuses[item["id"]]
        items.append({**item, "status": status, "reason": reason})
    return {
        "title": RECORDS_TITLE,
        "statement": list(RECORDS_STATEMENT),
        "label": _label(aircraft, entries, ledger or flights),
        "aircraft": {
            "key": aircraft.key,
            "label": aircraft.label,
            "source": aircraft.source,
            "synthetic": aircraft.synthetic,
            "licence": aircraft.licence,
            "attribution": aircraft.attribution,
            "identified_by": "the flight controller's id in its logs; nothing in a log "
            "identifies the airframe, motors, propellers or battery packs",
        },
        "generated_utc": _stamp(now),
        "as_of": _stamp(as_of),
        "commit": commit or "unknown",
        "version": __version__,
        "integrity": _integrity(store.journal_state()),
        "sections": [{"number": n, "title": t} for n, t in enumerate(RECORDS_SECTIONS, start=1)],
        "sources": list(RECORDS_SOURCES),
        "items": items,
        "maintenance": maintenance,
        "operations": operations,
        "occurrence_note": OCCURRENCE_NOTE,
        "retention": retention,
        "entry_note": NOTE,
    }
