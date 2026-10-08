"""Build the draft evidence pack for one aircraft as plain JSON-ready data.

Everything comes from the store: the flight records, the ledger, the projection and the
due list. Nothing is computed here that the engine does not already compute; the pack
gathers, labels and hashes.

A stored record the store cannot read (issue #43) does not stop the pack: a damaged store is
where a pack that names the damage matters. Nothing is computed without the record either:
each value that depended on it is `{"unknown": <the record named>}`, every item it would
have supported is "not evidenced" for that reason, and the label says so.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from uas_workbench import __version__
from uas_workbench.fleet import FleetConfig
from uas_workbench.fleet.model import Aircraft
from uas_workbench.flight import Reconciliation
from uas_workbench.flight.record import FlightRecord, Unknown, is_known
from uas_workbench.ledger import NOTE, Entry, UnreadableRecord
from uas_workbench.life import NO_RECORD, DueList, MaintenanceRecord
from uas_workbench.life.engine import time_in_service
from uas_workbench.service.app import (
    DueOut,
    ReconcileOut,
    due_view,
    entries_about,
    entry_view,
    flight_view,
    flights_until,
    grounded_view,
    reconcile_view,
)
from uas_workbench.service.store import Store

from .sources import (
    OSO_ITEMS,
    S_COMPONENTS,
    S_LOG,
    S_PROGRAMME,
    S_USAGE,
    SOURCES,
    STATEMENT,
    VERSION_NOTE,
)

SECTIONS: tuple[str, ...] = (
    "Draft evidence pack for OSO #03",
    "Sources relied on",
    "The aircraft",
    "What this pack holds, item by item",
    "Time in service and usage",
    "Maintenance programme status",
    "Life-limited and tracked components",
    "Maintenance log",
    "Not evidenced by this workbench",
    "Traceability",
)
LEDGER_EMPTY = (
    "the ledger holds nothing for this aircraft: no maintenance record has been entered, so "
    "there is no programme status, no component history and no maintenance log to show"
)
PROGRAMME_SOURCE_NOTE = (
    "The intervals and limits are the operator's editable defaults from fleet.toml, each "
    "citing the public civil source its form comes from; they are not the designer's "
    "instructions for continuing airworthiness, which an authority would ask for."
)
DERIVED = {"current", "superseded_reason"}


def _stamp(t: datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def ledger_hash(entries: list[dict[str, Any]]) -> str:
    """SHA-256 over the canonical JSON of every entry read, superseded ones included."""
    canonical = json.dumps(
        [{k: v for k, v in e.items() if k not in DERIVED} for e in entries],
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _label(aircraft: Aircraft, entries: list[Entry], problem: UnreadableRecord | None) -> str:
    if problem is not None:
        data = (
            "Synthetic data: this aircraft and its flights are generated"
            if aircraft.synthetic
            else "Real data: public log excerpts"
        )
        return f"{data}; a maintenance record cannot be read ({problem.name})"
    if aircraft.synthetic:
        if all(e.synthetic for e in entries):
            return "Synthetic data: this aircraft, its flights and its records are generated"
        return "Mixed data: a generated aircraft and flights, with entries a person recorded"
    if entries:
        return "Real data: public log excerpts, with entries a person recorded"
    return "Real data: public log excerpts; no maintenance record exists for this aircraft"


def _log(store: Store, entries: list[Entry]) -> list[dict[str, Any]]:
    dead = store.projection().superseded_by
    out: list[dict[str, Any]] = []
    for e in entries:
        view = entry_view(store, e).model_dump(mode="json")
        successor = store.entry(dead[e.id]) if e.id in dead else None
        view["current"] = e.id not in dead
        view["superseded_reason"] = successor.reason if successor else None
        out.append(view)
    return out


def _statuses(
    has_record: bool | str, programme: bool | str, has_components: bool | str
) -> dict[str, tuple[str, list[int], str]]:
    """Item id -> (status, supporting sections, reason) for this aircraft's records: whether
    a maintenance record exists (section 8), whether the programme status could be computed
    from it (section 6), and whether a component is recorded on the aircraft (section 7). A
    str in place of a bool is why that is not known: the item is then not evidenced for it."""
    log_yes = has_record is True
    programme_yes = programme is True
    none = "the workbench holds no maintenance instructions, staff list, authorisation, "
    none += "training record, release or procedure manual; only records of what was done"
    no_record = (
        has_record
        if isinstance(has_record, str)
        else "no maintenance record exists for this aircraft"
    )
    no_programme = programme if isinstance(programme, str) else no_record
    return {
        "integrity-low": ("not evidenced", [], none),
        "integrity-medium": (
            (
                "partly",
                [S_PROGRAMME, S_LOG],
                "section 6 shows the schedule status against the operator's intervals and "
                "section 8 the maintenance log with why each thing was done; no release to "
                "service is recorded, and the workbench holds no staff authorisation",
            )
            if log_yes and programme_yes
            else ("not evidenced", [], no_programme)
        ),
        "integrity-high": ("not evidenced", [], none),
        "assurance-1-low": (
            (
                "partly",
                [S_LOG],
                "section 8 is a maintenance log with why each thing was done, "
                "which may be shown at audit; instructions and the staff list are not held",
            )
            if log_yes
            else ("not evidenced", [], no_record)
        ),
        "assurance-1-medium": (
            (
                "partly",
                [S_PROGRAMME],
                "section 6 shows the programme's layout with the public "
                "source each interval cites; whether the authority accepts it, and the list of "
                "release-authorised staff, are not evidenced",
            )
            if programme_yes
            else ("not evidenced", [], no_programme)
        ),
        "assurance-1-high": ("not evidenced", [], "no third-party validation is recorded"),
        "assurance-2-low": ("not evidenced", [], "no training or qualification record is held"),
        "assurance-2-medium": ("not evidenced", [], "no training syllabus or record is held"),
        "assurance-2-high": ("not evidenced", [], "no recurrent training programme is held"),
        "ica-airworthiness-limitations": (
            (
                "supported",
                [S_PROGRAMME, S_COMPONENTS, S_LOG],
                "sections 6 to 8 hold each "
                "life-limited part's limits, usage, installations, removals and the entries "
                "behind them; the limits are the operator's defaults, not the designer's",
            )
            if has_components is True
            else (
                "not evidenced",
                [],
                has_components
                if isinstance(has_components, str)
                else "no component is recorded on this aircraft",
            )
        ),
    }


def _unknown(problem: UnreadableRecord) -> dict[str, str]:
    return {"unknown": problem.detail}


def build_pack(
    store: Store,
    aircraft_key: str,
    config: FleetConfig,
    *,
    as_of: datetime,
    commit: str | None,
    generated_utc: datetime | None = None,
) -> dict[str, Any]:
    aircraft = store.get_aircraft(aircraft_key)
    if aircraft is None:
        raise KeyError(aircraft_key)
    now = generated_utc or datetime.now(UTC).replace(microsecond=0)
    # Everything the pack reads is fixed at as_of: the entries that had occurred by then
    # (liveness decided over the whole ledger), and the flights whose logs had ended by then.
    # Each read that can meet a record the store cannot read is tried on its own, so what
    # that record leaves unknown is said where it would have been shown, and nothing else.
    ledger: UnreadableRecord | None = None  # the ledger, or the projection of it
    flights: UnreadableRecord | None = None  # this aircraft's flight records
    due_problem: UnreadableRecord | None = None  # the due list: both, and other aircraft's
    entries: list[Entry] = []
    log: list[dict[str, Any]] = []
    try:
        entries = [e for e in entries_about(store, aircraft_key) if e.occurred_utc <= as_of]
        log = _log(store, entries)
    except UnreadableRecord as exc:
        ledger = exc
    records: Sequence[FlightRecord] = []
    result: Reconciliation | None = None
    recon: ReconcileOut | None = None
    try:
        records = flights_until(store.flights(aircraft_key), as_of)
        result, recon = reconcile_view(store, aircraft, config, as_of)
    except UnreadableRecord as exc:
        flights = exc
    maintenance: MaintenanceRecord | None = None
    if ledger is None:
        maintenance = store.maintenance(aircraft_key, as_of)  # the projection: read once above
    due: DueList | None = None
    due_out: DueOut | None = None
    try:
        due, _board, due_out = due_view(store, aircraft, config, as_of)
    except UnreadableRecord as exc:
        due_problem = exc
    grounded: dict[str, Any]
    if flights is not None:
        grounded = _unknown(flights)
    elif due_problem is not None:
        # The logs are readable and the board at each one's start is not: no log is judged,
        # each with why, as the view says of a log it cannot judge.
        grounded = {
            "logs": len(records),
            "judged": 0,
            "findings": [],
            "not_judged": [
                {
                    "log_ref": r.log_ref,
                    "utc_start": _stamp_maybe(r.utc_start),
                    "why": due_problem.detail,
                }
                for r in records
            ],
        }
    else:
        view = grounded_view(store, aircraft, config, as_of)[1].model_dump(mode="json")
        grounded = {k: view[k] for k in ("logs", "judged", "findings", "not_judged")}
    # A ledger that cannot be projected fails the due list too, so `first` is the record to
    # name for everything that needs the due list, and `due` is set exactly when it is None.
    first = ledger or due_problem
    has_record: bool | str
    if ledger is not None:
        has_record = ledger.detail
    elif maintenance is not None:
        has_record = True
    elif due is not None:
        has_record = bool(due.usage)  # a component installed is a record of this aircraft
    else:
        assert due_problem is not None
        has_record = due_problem.detail
    usage_out: dict[str, Any]
    if flights is None:
        assert result is not None and recon is not None
        usage_out = {
            "logged_s": round(
                sum(r.flight_time_s for r in records if is_known(r.flight_time_s)), 1
            ),
            "unlogged_s": round(
                sum(f.seconds for f in result.findings if f.kind == "unlogged_flight"), 1
            ),
            "flights": [flight_view(r).model_dump(mode="json") for r in records],
            "findings": [f.model_dump(mode="json") for f in recon.findings],
            "unchecked": dict(recon.unchecked),
        }
    else:
        usage_out = dict.fromkeys(
            ("logged_s", "unlogged_s", "flights", "findings", "unchecked"), _unknown(flights)
        )
    tis: Any = {"unknown": NO_RECORD}
    before_s: Any = {"unknown": NO_RECORD}
    if ledger is not None:
        tis = before_s = _unknown(ledger)
    elif maintenance is not None:
        before_s = maintenance.time_in_service_before_s
        if flights is not None:
            tis = _unknown(flights)
        else:
            # Every flight is passed and `until` selects: the first log, which decides whether
            # the time before it is known, may lie after as_of.
            total = time_in_service(
                aircraft_key, before_s, store.flights(aircraft_key), config.tolerance_s, until=as_of
            )
            tis = {"unknown": total.reason} if isinstance(total, Unknown) else round(total, 1)
    components: list[dict[str, Any]] = []
    if first is None and due is not None:
        for c in store.components(as_of):
            usage = due.usage.get(c.id)
            if usage is None:
                continue
            components.append(
                {
                    "id": c.id,
                    "kind": c.kind,
                    "in_service_since": c.in_service_since.isoformat(),
                    "hours_s_before": c.hours_s_before,
                    "cycles_before": c.cycles_before,
                    "installations": [
                        {
                            "aircraft_key": i.aircraft_key,
                            "from_utc": _stamp(i.from_utc),
                            "to_utc": _stamp(i.to_utc) if i.to_utc else None,
                        }
                        for i in c.installations
                    ],
                    "usage": {"hours_s": round(usage.hours_s, 1), "cycles": usage.cycles},
                    "synthetic": c.synthetic,
                    "entries": _log(
                        store, [e for e in store.entries(c.id) if e.occurred_utc <= as_of]
                    ),
                }
            )
    programme_known: bool | str = first.detail if first else has_record
    has_components: bool | str = first.detail if first else bool(components)
    statuses = _statuses(has_record, programme_known, has_components)
    items = []
    for item in OSO_ITEMS:
        status, sections, reason = statuses[item["id"]]
        items.append({**item, "status": status, "sections": sections, "reason": reason})
    programme: dict[str, Any]
    if first is None:
        assert due_out is not None
        programme = due_out.model_dump(mode="json")
    else:
        programme = {"status": _unknown(first), "status_reasons": [], "items": [], "notes": []}
    if ledger is not None:
        ledger_note = ledger.detail
    elif log:
        ledger_note = (
            "every entry about this aircraft that had occurred by the pack's as_of, in the "
            "order it occurred, superseded entries included as history with the entry that "
            "superseded them"
        )
    else:
        ledger_note = LEDGER_EMPTY
    return {
        "title": f"Draft evidence pack for OSO #03: {aircraft_key}",
        "statement": list(STATEMENT),
        "label": _label(aircraft, entries, ledger),
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
        "ledger_hash": _unknown(ledger) if ledger else ledger_hash(log),
        "counts": {
            "entries": _unknown(ledger) if ledger else len(log),
            "flights": _unknown(flights) if flights else len(records),
            "due_items": _unknown(first) if first else len(programme["items"]),
        },
        "sections": [{"number": n, "title": t} for n, t in enumerate(SECTIONS, start=1)],
        "sources": list(SOURCES),
        "version_note": VERSION_NOTE,
        "items": items,
        "usage": {
            "time_in_service_s": tis,
            "before_s": before_s,
            "logged_s": usage_out["logged_s"],
            "unlogged_s": usage_out["unlogged_s"],
            "flights": usage_out["flights"],
            "findings": usage_out["findings"],
            # Flights whose log started while the records show the aircraft grounded, and the
            # logs that could not be judged with why: sentences, never cues.
            "grounded_flights": grounded,
            "unchecked": usage_out["unchecked"],
            "tolerance_s": config.tolerance_s,
        },
        "programme": {
            "status": programme["status"],
            "status_reasons": programme["status_reasons"],
            "items": programme["items"],
            "notes": programme["notes"],
            "source_note": PROGRAMME_SOURCE_NOTE,
        },
        "components": components,
        "log": log,
        "ledger_note": ledger_note,
        "entry_note": NOTE,
        "not_evidenced": [i for i in items if i["status"] == "not evidenced"],
        "section_usage": S_USAGE,
    }


def _stamp_maybe(t: Any) -> Any:
    return _stamp(t) if isinstance(t, datetime) else {"unknown": t.reason}
