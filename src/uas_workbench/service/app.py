"""FastAPI application over a Store: aircraft, flights, reconciliation, ingest, metrics."""

from __future__ import annotations

import dataclasses
import gc
import logging
import os
import tempfile
import time
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel

from uas_workbench import __version__
from uas_workbench.fleet import FleetConfig, load_config
from uas_workbench.fleet.model import Aircraft
from uas_workbench.flight import Reconciliation, is_known, reconcile
from uas_workbench.flight.ardupilot import read_dataflash
from uas_workbench.flight.codec import record_to_json
from uas_workbench.flight.px4 import read_ulog
from uas_workbench.flight.record import FlightRecord

from .observability import FINDINGS, FLIGHTS_STORED, HTTP_LATENCY, HTTP_REQUESTS
from .store import DuplicateFlight, Store

logger = logging.getLogger("uasw.http")

DESCRIPTION = """Flight records per aircraft from PX4 and ArduPilot logs, and reconciliation of
logged flight time against the autopilot's own lifetime counter.

Every value a log cannot support is returned as `{"unknown": "<reason>"}`, never as zero.
Every record and aircraft carries `synthetic`: the demo fleet is generated from a seed; the
two real showcase aircraft are excerpts of public, licensed logs. Civil fleet sustainment
only; see the repository's scope and non-goals."""


# ---- response models -------------------------------------------------------------------


class UnknownOut(BaseModel):
    unknown: str


class FaultEventOut(BaseModel):
    t_s: float
    kind: str
    detail: str


class LifetimeOut(BaseModel):
    at_boot_s: float
    last_seen_s: float
    last_seen_at_s: float


class FlightOut(BaseModel):
    source: str
    log_ref: str
    licence: str
    attribution: str
    firmware: str
    log_span_s: float
    aircraft_key: str | UnknownOut
    utc_start: datetime | UnknownOut
    flight_time_s: float | UnknownOut
    arm_cycles: int | UnknownOut
    landings: int | UnknownOut
    battery_mah: float | UnknownOut
    battery_wh: float | UnknownOut
    fault_events: list[FaultEventOut] | UnknownOut
    boot_count: int | UnknownOut
    lifetime: LifetimeOut | UnknownOut
    synthetic: bool


class AircraftOut(BaseModel):
    key: str
    label: str
    source: str
    synthetic: bool
    licence: str
    attribution: str
    status: str | UnknownOut
    flights: int
    flight_time_known_s: float
    findings: int


class CoverageOut(BaseModel):
    log_ref: str
    next_log_ref: str
    unlogged_s: float
    boots_between: int | UnknownOut


class FindingOut(BaseModel):
    aircraft_key: str
    log_ref: str
    kind: str
    seconds: float
    message: str
    synthetic: bool


class ReconcileOut(BaseModel):
    aircraft_key: str
    synthetic: bool
    tolerance_s: float
    coverage: list[CoverageOut]
    findings: list[FindingOut]
    unchecked: dict[str, str]


# ---- views shared with the static export -----------------------------------------------


def _maybe(value: Any) -> Any:
    return value if is_known(value) else {"unknown": value.reason}


def flight_view(record: FlightRecord) -> FlightOut:
    return FlightOut.model_validate(record_to_json(record))


def reconcile_view(
    store: Store, aircraft: Aircraft, config: FleetConfig
) -> tuple[Reconciliation, ReconcileOut]:
    result = reconcile(
        store.flights(aircraft.key), aircraft_key=aircraft.key, tolerance_s=config.tolerance_s
    )
    out = ReconcileOut(
        aircraft_key=aircraft.key,
        synthetic=aircraft.synthetic,
        tolerance_s=config.tolerance_s,
        coverage=[
            CoverageOut(
                log_ref=c.log_ref,
                next_log_ref=c.next_log_ref,
                unlogged_s=c.unlogged_s,
                boots_between=_maybe(c.boots_between),
            )
            for c in result.coverage
        ],
        findings=[
            FindingOut(
                aircraft_key=f.aircraft_key,
                log_ref=f.log_ref,
                kind=f.kind,
                seconds=f.seconds,
                message=f.message,
                synthetic=aircraft.synthetic,
            )
            for f in result.findings
        ],
        unchecked=dict(result.unchecked),
    )
    return result, out


def aircraft_view(store: Store, aircraft: Aircraft, config: FleetConfig) -> AircraftOut:
    records = store.flights(aircraft.key)
    result, _ = reconcile_view(store, aircraft, config)
    return AircraftOut(
        key=aircraft.key,
        label=aircraft.label,
        source=aircraft.source,
        synthetic=aircraft.synthetic,
        licence=aircraft.licence,
        attribution=aircraft.attribution,
        status=_maybe(aircraft.status),
        flights=len(records),
        flight_time_known_s=round(
            sum(r.flight_time_s for r in records if is_known(r.flight_time_s)), 1
        ),
        findings=len(result.findings),
    )


def refresh_gauges(store: Store, config: FleetConfig) -> None:
    FLIGHTS_STORED.set(store.flight_count())
    kinds: Counter[str] = Counter()
    for aircraft in store.aircraft():
        result, _ = reconcile_view(store, aircraft, config)
        kinds.update(f.kind for f in result.findings)
    for kind in ("unlogged_flight", "counter_mismatch"):
        FINDINGS.labels(kind=kind).set(kinds.get(kind, 0))


# ---- the application -------------------------------------------------------------------

READERS = {".ulg": read_ulog, ".bin": read_dataflash}


def _remove(path: Path) -> None:
    """Delete the temporary upload; on Windows a just-released handle can need a retry."""
    for attempt in range(5):
        try:
            os.unlink(path)
            return
        except PermissionError:
            gc.collect()
            time.sleep(0.05 * (attempt + 1))
    logger.warning("temporary upload not removed", extra={"path": str(path)})


def create_app(store: Store, config: FleetConfig | None = None) -> FastAPI:
    cfg = config or load_config()
    app = FastAPI(
        title="UAS Sustainment Workbench",
        version=__version__,
        description=DESCRIPTION,
        license_info={"name": "Apache-2.0"},
    )
    refresh_gauges(store, cfg)

    @app.middleware("http")
    async def observe(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = uuid.uuid4().hex[:12]
        started = time.perf_counter()
        response = await call_next(request)
        duration = time.perf_counter() - started
        route = request.scope.get("route")
        path = getattr(route, "path", request.url.path)
        HTTP_REQUESTS.labels(request.method, path, str(response.status_code)).inc()
        HTTP_LATENCY.labels(request.method, path).observe(duration)
        response.headers["X-Request-ID"] = request_id
        logger.info(
            "request",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": path,
                "status": response.status_code,
                "duration_ms": round(duration * 1000, 2),
            },
        )
        return response

    def _aircraft_or_404(key: str) -> Aircraft:
        aircraft = store.get_aircraft(key)
        if aircraft is None:
            raise HTTPException(status_code=404, detail=f"aircraft {key!r} is not in the store")
        return aircraft

    @app.get("/health", tags=["service"])
    def health() -> dict[str, object]:
        return {
            "status": "ok",
            "version": __version__,
            "aircraft": len(store.aircraft()),
            "flights": store.flight_count(),
        }

    @app.get("/aircraft", tags=["fleet"], response_model=list[AircraftOut])
    def list_aircraft() -> list[AircraftOut]:
        return [aircraft_view(store, a, cfg) for a in store.aircraft()]

    @app.get("/aircraft/{key}", tags=["fleet"], response_model=AircraftOut)
    def get_aircraft(key: str) -> AircraftOut:
        return aircraft_view(store, _aircraft_or_404(key), cfg)

    @app.get("/aircraft/{key}/flights", tags=["flights"], response_model=list[FlightOut])
    def list_flights(key: str) -> list[FlightOut]:
        _aircraft_or_404(key)
        return [flight_view(r) for r in store.flights(key)]

    @app.get("/aircraft/{key}/reconcile", tags=["reconcile"], response_model=ReconcileOut)
    def reconcile_aircraft(key: str) -> ReconcileOut:
        return reconcile_view(store, _aircraft_or_404(key), cfg)[1]

    @app.get("/fleet/findings", tags=["reconcile"], response_model=list[FindingOut])
    def fleet_findings() -> list[FindingOut]:
        return [f for a in store.aircraft() for f in reconcile_view(store, a, cfg)[1].findings]

    @app.post("/ingest", tags=["flights"], status_code=201, response_model=FlightOut)
    def ingest(
        aircraft_key: Annotated[str, Form()],
        log: Annotated[UploadFile, File()],
        licence: Annotated[str, Form()] = "unknown",
        attribution: Annotated[str, Form()] = "unknown",
    ) -> FlightOut:
        """Parse one log into a flight record. The raw log is read once and not kept."""
        suffix = Path(log.filename or "").suffix.lower()
        reader = READERS.get(suffix)
        if reader is None:
            raise HTTPException(422, f"unsupported file type {suffix!r}; expected .ulg or .bin")
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(log.file.read())
            path = Path(tmp.name)
        record: FlightRecord | None = None
        error = ""
        try:
            record = reader(path, licence=licence, attribution=attribution)
        except Exception as exc:
            error = str(exc)
        # The failed parser's file handle lives in the exception's traceback; it is released
        # once the except block ends, so the temp file is removed only now.
        _remove(path)
        if record is None:
            raise HTTPException(422, f"not a readable PX4 ULog or ArduPilot DataFlash log: {error}")
        record = dataclasses.replace(record, log_ref=Path(log.filename or "upload").stem)
        store.ensure_aircraft(aircraft_key, record)
        try:
            store.add_flight(aircraft_key, record)
        except DuplicateFlight as exc:
            raise HTTPException(409, str(exc)) from exc
        refresh_gauges(store, cfg)
        logger.info("ingested", extra={"aircraft_key": aircraft_key, "log_ref": record.log_ref})
        return flight_view(record)

    @app.get("/metrics", tags=["service"], response_class=PlainTextResponse)
    def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app
