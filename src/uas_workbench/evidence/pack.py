"""Build the draft evidence pack for one aircraft as plain JSON-ready data.

Everything comes from the store: the flight records, the ledger, the projection and the
due list. Nothing is computed here that the engine does not already compute; the pack
gathers, labels and hashes.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from uas_workbench import __version__
from uas_workbench.fleet import FleetConfig
from uas_workbench.fleet.model import Aircraft
from uas_workbench.flight.record import is_known
from uas_workbench.ledger import NOTE, Entry
from uas_workbench.life import NO_RECORD
from uas_workbench.life.engine import time_in_service
from uas_workbench.service.app import (
    due_view,
    entries_about,
    entry_view,
    flight_view,
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


def _label(aircraft: Aircraft, entries: list[Entry]) -> str:
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


def _statuses(has_record: bool, has_components: bool) -> dict[str, tuple[str, list[int], str]]:
    """Item id -> (status, supporting sections, reason) for this aircraft's records."""
    log_yes = has_record
    none = "the workbench holds no maintenance instructions, staff list, authorisation, "
    none += "training record, release or procedure manual; only records of what was done"
    programme = (
        (
            "partly",
            [S_PROGRAMME, S_LOG],
            "section 6 shows the schedule status against the operator's intervals and section "
            "8 the maintenance log with why each thing was done; no release to service is "
            "recorded, and the workbench holds no staff authorisation",
        )
        if log_yes
        else ("not evidenced", [], "no maintenance record exists for this aircraft")
    )
    return {
        "integrity-low": ("not evidenced", [], none),
        "integrity-medium": programme,
        "integrity-high": ("not evidenced", [], none),
        "assurance-1-low": (
            (
                "partly",
                [S_LOG],
                "section 8 is a maintenance log with why each thing was done, "
                "which may be shown at audit; instructions and the staff list are not held",
            )
            if log_yes
            else ("not evidenced", [], "no maintenance record exists for this aircraft")
        ),
        "assurance-1-medium": (
            (
                "partly",
                [S_PROGRAMME],
                "section 6 shows the programme's layout with the public "
                "source each interval cites; whether the authority accepts it, and the list of "
                "release-authorised staff, are not evidenced",
            )
            if log_yes
            else ("not evidenced", [], "no maintenance record exists for this aircraft")
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
            if has_components
            else ("not evidenced", [], "no component is recorded on this aircraft")
        ),
    }


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
    entries = entries_about(store, aircraft_key)
    log = _log(store, entries)
    records = store.flights(aircraft_key)
    result, recon = reconcile_view(store, aircraft, config)
    due, _board, due_out = due_view(store, aircraft, config, as_of)
    maintenance = store.maintenance(aircraft_key)
    has_record = maintenance is not None or bool(due.usage)
    unlogged_s = round(sum(f.seconds for f in result.findings if f.kind == "unlogged_flight"), 1)
    logged_s = round(sum(r.flight_time_s for r in records if is_known(r.flight_time_s)), 1)
    tis: Any = {"unknown": NO_RECORD}
    before_s: Any = {"unknown": NO_RECORD}
    if maintenance is not None:
        before_s = maintenance.time_in_service_before_s
        tis = round(
            time_in_service(aircraft_key, before_s, records, config.tolerance_s, until=as_of),
            1,
        )
    components: list[dict[str, Any]] = []
    for c in store.components():
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
                "entries": _log(store, list(store.entries(c.id))),
            }
        )
    statuses = _statuses(has_record, bool(components))
    items = []
    for item in OSO_ITEMS:
        status, sections, reason = statuses[item["id"]]
        items.append({**item, "status": status, "sections": sections, "reason": reason})
    programme = due_out.model_dump(mode="json")
    return {
        "title": f"Draft evidence pack for OSO #03: {aircraft_key}",
        "statement": list(STATEMENT),
        "label": _label(aircraft, entries),
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
        "ledger_hash": ledger_hash(log),
        "counts": {
            "entries": len(log),
            "flights": len(records),
            "due_items": len(programme["items"]),
        },
        "sections": [{"number": n, "title": t} for n, t in enumerate(SECTIONS, start=1)],
        "sources": list(SOURCES),
        "version_note": VERSION_NOTE,
        "items": items,
        "usage": {
            "time_in_service_s": tis,
            "before_s": before_s,
            "logged_s": logged_s,
            "unlogged_s": unlogged_s,
            "flights": [flight_view(r).model_dump(mode="json") for r in records],
            "findings": [f.model_dump(mode="json") for f in recon.findings],
            "unchecked": dict(recon.unchecked),
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
        "ledger_note": (
            "every entry about this aircraft, in the order it occurred, superseded entries "
            "included as history with the entry that superseded them"
            if log
            else LEDGER_EMPTY
        ),
        "entry_note": NOTE,
        "not_evidenced": [i for i in items if i["status"] == "not evidenced"],
        "section_usage": S_USAGE,
    }
