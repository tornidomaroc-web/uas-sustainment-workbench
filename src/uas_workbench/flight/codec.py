"""JSON shape of a FlightRecord, shared by storage, the API and the static export.

Every Maybe field is either its value or {"unknown": "<reason>"}, so a reader of the JSON
can never mistake a missing value for a zero.
"""

from __future__ import annotations

from dataclasses import asdict, fields
from datetime import datetime
from typing import Any, cast

from .record import FaultEvent, FlightRecord, LifetimeCounter, Source, Unknown

JsonDict = dict[str, Any]


def _out(value: Any) -> Any:
    if isinstance(value, Unknown):
        return {"unknown": value.reason}
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, LifetimeCounter):
        return asdict(value)
    if isinstance(value, tuple):
        return [asdict(v) if isinstance(v, FaultEvent) else _out(v) for v in value]
    return value


def record_to_json(record: FlightRecord) -> JsonDict:
    return {f.name: _out(getattr(record, f.name)) for f in fields(record)}


def _maybe(value: Any) -> Any:
    if isinstance(value, dict) and set(value) == {"unknown"}:
        return Unknown(str(value["unknown"]))
    return value


def record_from_json(data: JsonDict) -> FlightRecord:
    utc = _maybe(data["utc_start"])
    lifetime = _maybe(data["lifetime"])
    faults = _maybe(data["fault_events"])
    return FlightRecord(
        source=cast(Source, data["source"]),
        log_ref=str(data["log_ref"]),
        licence=str(data["licence"]),
        attribution=str(data["attribution"]),
        firmware=str(data["firmware"]),
        log_span_s=float(data["log_span_s"]),
        aircraft_key=_maybe(data["aircraft_key"]),
        utc_start=datetime.fromisoformat(utc) if isinstance(utc, str) else utc,
        flight_time_s=_maybe(data["flight_time_s"]),
        arm_cycles=_maybe(data["arm_cycles"]),
        landings=_maybe(data["landings"]),
        battery_mah=_maybe(data["battery_mah"]),
        battery_wh=_maybe(data["battery_wh"]),
        fault_events=(
            tuple(FaultEvent(**e) for e in faults) if isinstance(faults, list) else faults
        ),
        boot_count=_maybe(data["boot_count"]),
        lifetime=LifetimeCounter(**lifetime) if isinstance(lifetime, dict) else lifetime,
        synthetic=bool(data.get("synthetic", False)),
    )
