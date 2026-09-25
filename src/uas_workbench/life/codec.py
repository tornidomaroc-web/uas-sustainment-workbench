"""JSON shape of the maintenance records, shared by storage and the API."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, cast

from .model import Component, InspectionDone, Installation, MaintenanceRecord, WorkOrder, WorkState

JsonDict = dict[str, Any]


def component_to_json(c: Component) -> JsonDict:
    return {
        "id": c.id,
        "kind": c.kind,
        "in_service_since": c.in_service_since.isoformat(),
        "hours_s_before": c.hours_s_before,
        "cycles_before": c.cycles_before,
        "installations": [
            {
                "aircraft_key": i.aircraft_key,
                "from_utc": i.from_utc.isoformat(),
                "to_utc": i.to_utc.isoformat() if i.to_utc else None,
            }
            for i in c.installations
        ],
        "synthetic": c.synthetic,
    }


def component_from_json(data: JsonDict) -> Component:
    return Component(
        id=str(data["id"]),
        kind=str(data["kind"]),
        in_service_since=date.fromisoformat(data["in_service_since"]),
        hours_s_before=float(data["hours_s_before"]),
        cycles_before=int(data["cycles_before"]),
        installations=tuple(
            Installation(
                str(i["aircraft_key"]),
                datetime.fromisoformat(i["from_utc"]),
                datetime.fromisoformat(i["to_utc"]) if i.get("to_utc") else None,
            )
            for i in data["installations"]
        ),
        synthetic=bool(data["synthetic"]),
    )


def maintenance_to_json(m: MaintenanceRecord) -> JsonDict:
    return {
        "aircraft_key": m.aircraft_key,
        "time_in_service_before_s": m.time_in_service_before_s,
        "inspections": [
            {
                "name": i.name,
                "done_utc": i.done_utc.isoformat(),
                "at_hours_s": i.at_hours_s,
                "carried_over_s": i.carried_over_s,
            }
            for i in m.inspections
        ],
        "work_orders": [
            {
                "opened_utc": w.opened_utc.isoformat(),
                "description": w.description,
                "state": w.state,
                "synthetic": w.synthetic,
            }
            for w in m.work_orders
        ],
        "synthetic": m.synthetic,
    }


def maintenance_from_json(data: JsonDict) -> MaintenanceRecord:
    return MaintenanceRecord(
        aircraft_key=str(data["aircraft_key"]),
        time_in_service_before_s=float(data["time_in_service_before_s"]),
        inspections=tuple(
            InspectionDone(
                str(i["name"]),
                datetime.fromisoformat(i["done_utc"]),
                float(i["at_hours_s"]),
                float(i.get("carried_over_s", 0.0)),
            )
            for i in data["inspections"]
        ),
        work_orders=tuple(
            WorkOrder(
                datetime.fromisoformat(w["opened_utc"]),
                str(w["description"]),
                cast(WorkState, str(w["state"])),
                bool(w["synthetic"]),
            )
            for w in data["work_orders"]
        ),
        synthetic=bool(data["synthetic"]),
    )
