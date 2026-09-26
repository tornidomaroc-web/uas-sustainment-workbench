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
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field

from uas_workbench import __version__
from uas_workbench.fleet import FleetConfig, load_config
from uas_workbench.fleet.model import Aircraft
from uas_workbench.flight import Reconciliation, is_known, reconcile
from uas_workbench.flight.ardupilot import read_dataflash
from uas_workbench.flight.codec import record_to_json
from uas_workbench.flight.px4 import read_ulog
from uas_workbench.flight.record import FlightRecord
from uas_workbench.ledger import NOTE, Entry, LedgerError, append, entry_to_json
from uas_workbench.life import Board, Component, DueList, board, due_list

from .observability import (
    AIRCRAFT_BY_STATUS,
    DUE_ITEMS,
    FINDINGS,
    FLIGHTS_STORED,
    HTTP_LATENCY,
    HTTP_REQUESTS,
)
from .store import DuplicateFlight, Store

logger = logging.getLogger("uasw.http")

DESCRIPTION = """Flight records per aircraft from PX4 and ArduPilot logs, reconciliation of
logged flight time against the autopilot's own lifetime counter, and the component life,
inspection due list and board state computed from them.

Maintenance is an append-only ledger (`/entries`): what a person states, when, and in what
words; a correction supersedes with a reason and the history stays. Every entry says that
the workbench does not certify airworthiness or return to service. Writes need the token in
`UASW_WRITE_TOKEN`, or come from this machine when none is set.

Every value a log cannot support is returned as `{"unknown": "<reason>"}`, never as zero.
Every record and aircraft carries `synthetic`: the demo fleet is generated from a seed; the
two real showcase aircraft are excerpts of public, licensed logs. Board states use civil
vocabulary only and are computed, never stored; every state but serviceable carries its
reasons as full sentences. Limits and intervals come from `fleet.toml`, each with its public
civil source. Civil fleet sustainment only; see the repository's scope and non-goals."""


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
    status: str | UnknownOut  # computed board state, civil vocabulary
    status_reasons: list[str]  # full sentences; empty only when serviceable or unknown
    overdue: int  # due items past their limit, tolerance included
    due_soon: int
    flights: int
    flight_time_known_s: float
    findings: int


class DueItemOut(BaseModel):
    aircraft_key: str
    subject: str
    component_id: str | None
    basis: str
    unit: str
    used: float
    limit: float
    remaining: float
    tolerance: float
    state: str
    source: str
    message: str
    synthetic: bool


class DueOut(BaseModel):
    aircraft_key: str
    synthetic: bool
    as_of: datetime
    status: str | UnknownOut
    status_reasons: list[str]
    time_in_service_s: float | UnknownOut
    items: list[DueItemOut]
    notes: list[str]


class EntryIn(BaseModel):
    """A maintenance ledger entry as a person states it. Never edited once appended."""

    subject: str = Field(min_length=1, description="an aircraft key or a component id")
    kind: str = Field(min_length=1)
    occurred_utc: datetime = Field(description="when the event happened")
    entered_by: str = Field(min_length=1, description="the person's name and role, as typed")
    statement: str = Field(default="", description="what was done, in the person's words")
    payload: dict[str, Any] = Field(default_factory=dict)
    supersedes: int | None = Field(default=None, description="the entry this one corrects")
    reason: str | None = Field(default=None, description="why; required with supersedes")


class EntryOut(BaseModel):
    id: int
    subject: str
    kind: str
    occurred_utc: datetime
    recorded_utc: datetime
    entered_by: str
    statement: str
    payload: dict[str, Any]
    supersedes: int | None
    reason: str | None
    synthetic: bool
    superseded_by: int | None
    note: str


class InstallationOut(BaseModel):
    aircraft_key: str
    from_utc: datetime
    to_utc: datetime | None


class ComponentOut(BaseModel):
    id: str
    kind: str
    in_service_since: str
    hours_s_before: float
    cycles_before: int
    installations: list[InstallationOut]
    installed_on: str | None
    synthetic: bool
    entries: list[EntryOut] = []


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


DUE_ORDER = {"overdue": 0, "overdue_within_tolerance": 1, "due_soon": 2, "ok": 3}


def due_view(
    store: Store, aircraft: Aircraft, config: FleetConfig, as_of: datetime | None = None
) -> tuple[DueList, Board, DueOut]:
    """The due list and board state of one aircraft at `as_of` (default: now)."""
    at = as_of or datetime.now(UTC)
    due = due_list(
        aircraft.key,
        maintenance=store.maintenance(aircraft.key),
        components=store.components(),
        flights_of=store.flights,
        policy=config.life,
        as_of=at,
        tolerance_s=config.tolerance_s,
    )
    state = board(due)
    items = sorted(due.items, key=lambda i: (DUE_ORDER[i.state], i.remaining / (i.limit or 1)))
    out = DueOut(
        aircraft_key=aircraft.key,
        synthetic=aircraft.synthetic,
        as_of=at,
        status=_maybe(state.status),
        status_reasons=list(state.reasons),
        time_in_service_s=_maybe(due.time_in_service_s),
        items=[
            DueItemOut(**{**dataclasses.asdict(i), "synthetic": aircraft.synthetic}) for i in items
        ],
        notes=list(due.notes),
    )
    return due, state, out


def aircraft_view(store: Store, aircraft: Aircraft, config: FleetConfig) -> AircraftOut:
    records = store.flights(aircraft.key)
    result, _ = reconcile_view(store, aircraft, config)
    _, state, due = due_view(store, aircraft, config)
    return AircraftOut(
        key=aircraft.key,
        label=aircraft.label,
        source=aircraft.source,
        synthetic=aircraft.synthetic,
        licence=aircraft.licence,
        attribution=aircraft.attribution,
        status=_maybe(state.status),
        status_reasons=list(state.reasons),
        overdue=sum(i.state in ("overdue", "overdue_within_tolerance") for i in due.items),
        due_soon=sum(i.state == "due_soon" for i in due.items),
        flights=len(records),
        flight_time_known_s=round(
            sum(r.flight_time_s for r in records if is_known(r.flight_time_s)), 1
        ),
        findings=len(result.findings),
    )


def entry_view(store: Store, entry: Entry) -> EntryOut:
    superseded_by = store.projection().superseded_by
    data = {k: v for k, v in entry_to_json(entry).items() if k != "id"}
    return EntryOut(
        id=entry.id or 0,
        superseded_by=superseded_by.get(entry.id or -1),
        note=NOTE,
        **data,
    )


def entries_about(store: Store, aircraft_key: str) -> list[Entry]:
    """The aircraft's own entries plus the component installs and removals that name it,
    in the order they occurred: what a maintenance lead reads as the aircraft's history."""
    own = list(store.entries(aircraft_key))
    moves = [
        e
        for e in store.entries()
        if e.kind in ("component.install", "component.remove")
        and e.payload.get("aircraft_key") == aircraft_key
    ]
    return sorted(own + moves, key=lambda e: (e.occurred_utc, e.id or 0))


def component_view(store: Store, c: Component, with_entries: bool = False) -> ComponentOut:
    on = [i for i in c.installations if i.to_utc is None]
    return ComponentOut(
        id=c.id,
        kind=c.kind,
        in_service_since=c.in_service_since.isoformat(),
        hours_s_before=c.hours_s_before,
        cycles_before=c.cycles_before,
        installations=[
            InstallationOut(aircraft_key=i.aircraft_key, from_utc=i.from_utc, to_utc=i.to_utc)
            for i in c.installations
        ],
        installed_on=on[-1].aircraft_key if on else None,
        synthetic=c.synthetic,
        entries=[entry_view(store, e) for e in store.entries(c.id)] if with_entries else [],
    )


def fleet_due(store: Store, config: FleetConfig, as_of: datetime | None = None) -> list[DueItemOut]:
    """Every due-soon and overdue item across the fleet, worst first."""
    items = [
        i
        for a in store.aircraft()
        for i in due_view(store, a, config, as_of)[2].items
        if i.state != "ok"
    ]
    return sorted(items, key=lambda i: (DUE_ORDER[i.state], i.remaining / (i.limit or 1)))


def refresh_gauges(store: Store, config: FleetConfig) -> None:
    FLIGHTS_STORED.set(store.flight_count())
    kinds: Counter[str] = Counter()
    statuses: Counter[str] = Counter()
    states: Counter[str] = Counter()
    for aircraft in store.aircraft():
        result, _ = reconcile_view(store, aircraft, config)
        kinds.update(f.kind for f in result.findings)
        due, state, _ = due_view(store, aircraft, config)
        statuses[state.status if is_known(state.status) else "unknown"] += 1
        states.update(i.state for i in due.items)
    for kind in ("unlogged_flight", "counter_mismatch"):
        FINDINGS.labels(kind=kind).set(kinds.get(kind, 0))
    for status in (*config.states, "unknown"):
        AIRCRAFT_BY_STATUS.labels(status=status).set(statuses.get(status, 0))
    for state_name in DUE_ORDER:
        DUE_ITEMS.labels(state=state_name).set(states.get(state_name, 0))


# ---- the application -------------------------------------------------------------------

READERS = {".ulg": read_ulog, ".bin": read_dataflash}


AsOf = Annotated[
    datetime | None, Query(description="Compute the due list at this UTC time instead of now.")
]


def _utc(value: datetime | None) -> datetime | None:
    """A naive query time is taken as UTC, so the engine always compares aware datetimes."""
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


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


LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def create_app(
    store: Store, config: FleetConfig | None = None, write_token: str | None = None
) -> FastAPI:
    """`write_token`: the bearer token writes must carry; None reads UASW_WRITE_TOKEN from the
    environment; with neither set, writes are accepted from loopback addresses only."""
    cfg = config or load_config()
    token = write_token if write_token is not None else os.environ.get("UASW_WRITE_TOKEN") or None
    app = FastAPI(
        title="UAS Sustainment Workbench",
        version=__version__,
        description=DESCRIPTION,
        license_info={"name": "Apache-2.0"},
    )
    refresh_gauges(store, cfg)

    def writer(request: Request) -> None:
        """Every write goes through here: POST /entries and POST /ingest."""
        if token:
            if request.headers.get("authorization") != f"Bearer {token}":
                raise HTTPException(
                    401, "writes need the token set in UASW_WRITE_TOKEN as a bearer token"
                )
            return
        host = request.client.host if request.client else ""
        if host not in LOOPBACK:
            raise HTTPException(
                403,
                f"writes are accepted from this machine only and this request came from {host}; "
                "set UASW_WRITE_TOKEN on the service to allow writes with a token",
            )

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

    @app.get("/aircraft/{key}/due", tags=["life"], response_model=DueOut)
    def aircraft_due(key: str, as_of: AsOf = None) -> DueOut:
        """Component life and inspection due list, and the board state with its reasons."""
        return due_view(store, _aircraft_or_404(key), cfg, _utc(as_of))[2]

    @app.get("/fleet/due", tags=["life"], response_model=list[DueItemOut])
    def fleet_due_items(as_of: AsOf = None) -> list[DueItemOut]:
        """What a maintenance lead asks first: everything due soon or overdue, worst first."""
        return fleet_due(store, cfg, _utc(as_of))

    @app.post(
        "/entries",
        tags=["ledger"],
        status_code=201,
        response_model=EntryOut,
        dependencies=[Depends(writer)],
    )
    def append_entry(body: EntryIn) -> EntryOut:
        """Append one maintenance entry. Entries are never edited; a correction supersedes.

        Refusals: 404 unknown aircraft or component, 403 a public showcase aircraft, 409 a
        transition the records cannot accept, 422 a shape error. Nothing is written then.
        """
        entry = Entry(
            id=None,
            subject=body.subject,
            kind=body.kind,
            occurred_utc=_utc(body.occurred_utc) or body.occurred_utc,
            recorded_utc=datetime.now(UTC).replace(microsecond=0),
            entered_by=body.entered_by,
            statement=body.statement,
            payload=body.payload,
            supersedes=body.supersedes,
            reason=body.reason,
            synthetic=False,
        )
        try:
            stored = append(
                store, entry, policy=cfg.life, now=datetime.now(UTC), tolerance_s=cfg.tolerance_s
            )
        except LedgerError as exc:
            raise HTTPException(exc.status, exc.detail) from exc
        refresh_gauges(store, cfg)
        logger.info(
            "entry",
            extra={"entry_id": stored.id, "kind": stored.kind, "subject": stored.subject},
        )
        return entry_view(store, stored)

    @app.get("/entries", tags=["ledger"], response_model=list[EntryOut])
    def list_entries(
        subject: str | None = None,
        aircraft: str | None = None,
        include_superseded: bool = False,
    ) -> list[EntryOut]:
        """The ledger, in the order entered. `subject` filters by exact subject; `aircraft`
        gives an aircraft's history, component moves included. Superseded entries are left
        out unless asked."""
        dead = store.projection().superseded_by
        found = entries_about(store, aircraft) if aircraft else store.entries(subject)
        return [entry_view(store, e) for e in found if include_superseded or e.id not in dead]

    @app.get("/entries/{entry_id}", tags=["ledger"], response_model=EntryOut)
    def get_entry(entry_id: int) -> EntryOut:
        entry = store.entry(entry_id)
        if entry is None:
            raise HTTPException(404, f"entry {entry_id} does not exist")
        return entry_view(store, entry)

    @app.get("/components", tags=["ledger"], response_model=list[ComponentOut])
    def list_components() -> list[ComponentOut]:
        return [component_view(store, c) for c in store.components()]

    @app.get("/components/{component_id}", tags=["ledger"], response_model=ComponentOut)
    def get_component(component_id: str) -> ComponentOut:
        found = [c for c in store.components() if c.id == component_id]
        if not found:
            raise HTTPException(404, f"component {component_id!r} is not registered")
        return component_view(store, found[0], with_entries=True)

    @app.post(
        "/ingest",
        tags=["flights"],
        status_code=201,
        response_model=FlightOut,
        dependencies=[Depends(writer)],
    )
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
