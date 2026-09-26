"""uasw: seed, serve, ingest and export the workbench.

    uasw seed            # synthetic fleet + real showcase excerpts into the SQLite store
    uasw serve           # HTTP API with /docs, /metrics; seeds first if the store is empty
    uasw ingest KEY LOG  # parse one or more logs into flight records for an aircraft
    uasw export-static   # write site/fleet.json, assistant.json and index.html from the store
    uasw ask "QUESTION"  # a local model answers through the service's read-only endpoints
    uasw record ...      # append a maintenance entry: work orders, components, inspections
    uasw evidence KEY --out pack.html   # the draft evidence pack for OSO #03 of one aircraft

Environment: UASW_DB (SQLite path, default data/local/fleet.sqlite), UASW_FIXTURES
(showcase excerpts, default tests/fixtures).
"""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

from uas_workbench.fleet import load_config
from uas_workbench.fleet.showcase import showcase
from uas_workbench.fleet.synthetic import generate
from uas_workbench.flight.ardupilot import read_dataflash
from uas_workbench.flight.px4 import read_ulog

from .observability import configure_logging
from .store import DuplicateFlight, Store

log = logging.getLogger("uasw.cli")

DEFAULT_DB = os.environ.get("UASW_DB", "data/local/fleet.sqlite")
DEFAULT_FIXTURES = Path(os.environ.get("UASW_FIXTURES", "tests/fixtures"))
READERS = {".ulg": read_ulog, ".bin": read_dataflash}


def open_store(path: str) -> Store:
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    return Store(path)


def seed(store: Store, fixtures: Path) -> None:
    config = load_config()
    store.add_fleet(generate(config))
    real = showcase(fixtures) if fixtures.exists() else None
    if real is not None:
        store.add_fleet(real)
    log.info(
        "seeded",
        extra={
            "seed": config.seed,
            "aircraft": len(store.aircraft()),
            "flights": store.flight_count(),
            "showcase": real is not None,
        },
    )


def cmd_seed(args: argparse.Namespace) -> None:
    seed(open_store(args.db), args.fixtures)


def cmd_serve(args: argparse.Namespace) -> None:
    import uvicorn

    from .app import create_app

    store = open_store(args.db)
    if store.flight_count() == 0:
        seed(store, args.fixtures)
    uvicorn.run(create_app(store), host=args.host, port=args.port, log_config=None)


def cmd_ingest(args: argparse.Namespace) -> None:
    store = open_store(args.db)
    for path in args.logs:
        reader = READERS.get(path.suffix.lower())
        if reader is None:
            log.error("unsupported file type", extra={"path": str(path)})
            continue
        record = reader(path, licence=args.licence, attribution=args.attribution)
        store.ensure_aircraft(args.aircraft, record)
        try:
            store.add_flight(args.aircraft, record)
        except DuplicateFlight as exc:
            log.warning(str(exc))
            continue
        log.info("ingested", extra={"aircraft_key": args.aircraft, "log_ref": record.log_ref})


def cmd_export_static(args: argparse.Namespace) -> None:
    from .static_export import export_static

    out = export_static(open_store(args.db), args.out)
    log.info("exported", extra={"out": str(out)})


def cmd_ask(args: argparse.Namespace) -> None:
    """Ask the local model one question through the service's read-only endpoints."""
    import json
    from datetime import UTC, datetime
    from urllib.parse import urlencode

    from uas_workbench.assistant import OllamaBackend, ask
    from uas_workbench.assistant.backends import json_http
    from uas_workbench.assistant.recording import from_answer, save

    base = args.url.rstrip("/")

    def caller(path: str, query: dict[str, str]) -> object:
        return json_http(f"{base}{path}?{urlencode(query)}" if query else f"{base}{path}", None)

    as_of = datetime.fromisoformat(args.as_of) if args.as_of else datetime.now(UTC)
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=UTC)
    backend = OllamaBackend(args.model, host=args.ollama)
    answer = ask(args.question, backend=backend, caller=caller, as_of=as_of)
    print(answer.text)
    print()
    print(f"Calls ({len(answer.calls)}): " + ", ".join(f"GET {c.path}" for c in answer.calls))
    print(
        "Grounded: yes"
        if answer.grounding.verified
        else "Grounded: NO, unsupported: " + ", ".join(answer.grounding.unsupported)
    )
    print(f"Model {answer.model_tag} ({answer.model_digest[:12]}), as of {as_of.isoformat()}")
    if args.record:
        recording = from_answer(answer, datetime.now(UTC).replace(microsecond=0))
        save(recording, args.record)
        log.info(
            "recorded", extra={"path": str(args.record), "grounded": answer.grounding.verified}
        )
    if args.json:
        print(json.dumps([c.result for c in answer.calls], indent=1))


def current_commit() -> str | None:
    """The commit this checkout is at, from git, else the UASW_COMMIT setting, else None."""
    import subprocess

    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"], capture_output=True, text=True, timeout=5
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return os.environ.get("UASW_COMMIT") or None


def cmd_evidence(args: argparse.Namespace) -> None:
    """Write the draft evidence pack for one aircraft as HTML, and as JSON with --json."""
    import json
    import sys
    from datetime import UTC, datetime

    from uas_workbench.evidence import build_pack, render_html

    store = open_store(args.db)
    if store.get_aircraft(args.aircraft) is None:
        print(f"refused (404): aircraft {args.aircraft} is not in the store", file=sys.stderr)
        sys.exit(1)
    as_of = datetime.fromisoformat(args.as_of) if args.as_of else datetime.now(UTC)
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=UTC)
    pack = build_pack(store, args.aircraft, load_config(), as_of=as_of, commit=current_commit())
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render_html(pack), encoding="utf-8")
    written = [str(args.out)]
    if args.json:
        path = args.out.with_suffix(".json")
        path.write_text(json.dumps(pack, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        written.append(str(path))
    log.info("evidence", extra={"aircraft_key": args.aircraft, "out": written})
    print(f"{pack['title']}: written to {', '.join(written)}. {pack['statement'][1]}")


def _record_entry(args: argparse.Namespace, subject: str, kind_: str, **details: object) -> None:
    """Build one entry from the parsed arguments, validate and append it, and print it."""
    import sys
    from datetime import UTC, datetime

    from uas_workbench.ledger import NOTE, Entry, LedgerError, append

    now = datetime.now(UTC).replace(microsecond=0)
    at = datetime.fromisoformat(args.at) if args.at else now
    if at.tzinfo is None:
        at = at.replace(tzinfo=UTC)
    entry = Entry(
        id=None,
        subject=subject,
        kind=kind_,
        occurred_utc=at,
        recorded_utc=now,
        entered_by=args.by,
        statement=getattr(args, "statement", "") or "",
        details={k: v for k, v in details.items() if v is not None},
        supersedes=getattr(args, "supersedes", None),
        reason=getattr(args, "reason", None),
        synthetic=False,
    )
    config = load_config()
    store = open_store(args.db)
    try:
        stored = append(store, entry, policy=config.life, now=now, tolerance_s=config.tolerance_s)
    except LedgerError as exc:
        print(f"refused ({exc.status}): {exc.detail}", file=sys.stderr)
        sys.exit(1)
    work = f" {stored.details['work_id']}" if "work_id" in stored.details else ""
    print(f"entry {stored.id} recorded: {stored.kind} on {stored.subject}{work}. {NOTE}")


def cmd_record(args: argparse.Namespace) -> None:
    what = args.what
    if what == "work-order":
        wid = getattr(args, "work_id", None)
        if args.action == "open":
            _record_entry(args, args.aircraft, "work_order.open", state=args.state, work_id=wid)
        elif args.action == "state":
            _record_entry(args, args.aircraft, "work_order.state", work_id=wid, state=args.state)
        else:
            _record_entry(args, args.aircraft, "work_order.close", work_id=wid)
    elif what == "component":
        if args.action == "register":
            _record_entry(
                args,
                args.component,
                "component.register",
                kind=args.kind,
                in_service_since=args.since,
                hours_s_before=args.hours_before * 3600.0,
                cycles_before=args.cycles_before,
            )
        else:
            _record_entry(
                args, args.component, f"component.{args.action}", aircraft_key=args.aircraft
            )
    elif what == "inspection":
        _record_entry(
            args,
            args.aircraft,
            "inspection.done",
            name=args.name,
            at_hours_s=args.at_hours * 3600.0 if args.at_hours is not None else None,
            carried_over_s=args.carried_over_hours * 3600.0,
        )
    elif what == "time-in-service":
        _record_entry(args, args.aircraft, "time_in_service.set", before_s=args.hours * 3600.0)
    elif what == "retract":
        target = open_store(args.db).entry(args.entry_id)
        if target is None:
            import sys

            print(f"refused (404): entry {args.entry_id} does not exist", file=sys.stderr)
            sys.exit(1)
        args.supersedes = args.entry_id
        args.statement = ""
        _record_entry(args, target.subject, "retraction")
    elif what == "history":
        store = open_store(args.db)
        dead = store.projection().superseded_by
        for e in store.entries(args.subject):
            if e.id in dead and not args.all:
                continue
            line = (
                f"#{e.id} {e.occurred_utc:%Y-%m-%d %H:%M} {e.kind} by {e.entered_by}: {e.statement}"
            )
            if e.details.get("work_id"):
                line += f" [{e.details['work_id']}]"
            if e.supersedes is not None:
                line += f" (supersedes #{e.supersedes}: {e.reason})"
            if e.id in dead:
                by = store.entry(dead[e.id])
                line += f" (superseded by #{dead[e.id]}: {by.reason if by else ''})"
            print(line)


def _who(p: argparse.ArgumentParser, statement: bool = True) -> None:
    p.add_argument("--by", required=True, help="your name and role, recorded as typed")
    if statement:
        p.add_argument("--statement", required=True, help="what was done, in your words")
    p.add_argument("--at", help="when it happened (ISO 8601, UTC); default now")
    p.add_argument("--supersedes", type=int, help="the entry id this one corrects")
    p.add_argument("--reason", help="why that entry was wrong; required with --supersedes")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="uasw", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--db", default=DEFAULT_DB, help="SQLite file, or :memory:")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("seed", help="seed the store with the synthetic fleet and showcase")
    p.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURES)
    p.set_defaults(func=cmd_seed)

    p = sub.add_parser("serve", help="run the HTTP API")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURES)
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("ingest", help="parse logs into flight records")
    p.add_argument("aircraft", help="aircraft key the logs belong to")
    p.add_argument("logs", type=Path, nargs="+")
    p.add_argument("--licence", default="unknown")
    p.add_argument("--attribution", default="unknown")
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("export-static", help="write the static site from the store")
    p.add_argument("--out", type=Path, default=Path("site"))
    p.set_defaults(func=cmd_export_static)

    r = sub.add_parser(
        "record",
        help="append a maintenance entry to the ledger (never edited; corrections supersede)",
    )
    what = r.add_subparsers(dest="what", required=True)

    wo = what.add_parser("work-order", help="open, change the state of, or close a work order")
    wo_action = wo.add_subparsers(dest="action", required=True)
    p = wo_action.add_parser("open")
    p.add_argument("aircraft")
    p.add_argument("--state", required=True, choices=["in_work", "awaiting_parts", "deferred"])
    p.add_argument("--work-id", dest="work_id", help="default WO-<entry id>")
    _who(p)
    p = wo_action.add_parser("state")
    p.add_argument("aircraft")
    p.add_argument("work_id")
    p.add_argument("--state", required=True, choices=["in_work", "awaiting_parts", "deferred"])
    _who(p)
    p = wo_action.add_parser("close")
    p.add_argument("aircraft")
    p.add_argument("work_id")
    _who(p)

    comp = what.add_parser("component", help="register, install or remove a component")
    comp_action = comp.add_subparsers(dest="action", required=True)
    p = comp_action.add_parser("register")
    p.add_argument("component")
    p.add_argument("--kind", required=True, help="a kind named in fleet.toml")
    p.add_argument("--since", required=True, help="in service since (YYYY-MM-DD)")
    p.add_argument("--hours-before", dest="hours_before", type=float, default=0.0)
    p.add_argument("--cycles-before", dest="cycles_before", type=int, default=0)
    _who(p)
    for action in ("install", "remove"):
        p = comp_action.add_parser(action)
        p.add_argument("component")
        p.add_argument("aircraft")
        _who(p)

    p = what.add_parser("inspection", help="record a completed inspection")
    p.add_argument("aircraft")
    p.add_argument("name", help="an inspection named in fleet.toml")
    p.add_argument("--at-hours", dest="at_hours", type=float, help="time in service; derived")
    p.add_argument("--carried-over-hours", dest="carried_over_hours", type=float, default=0.0)
    _who(p)

    p = what.add_parser("time-in-service", help="hours before the first log this tool holds")
    p.add_argument("aircraft")
    p.add_argument("hours", type=float)
    _who(p)

    p = what.add_parser("retract", help="supersede an entry with nothing, giving the reason")
    p.add_argument("entry_id", type=int)
    p.add_argument("--by", required=True)
    p.add_argument("--reason", required=True)
    p.add_argument("--at", help=argparse.SUPPRESS)

    p = what.add_parser("history", help="the entries of one aircraft or component")
    p.add_argument("subject")
    p.add_argument("--all", action="store_true", help="include superseded entries")
    r.set_defaults(func=cmd_record)

    p = sub.add_parser(
        "evidence", help="write the draft evidence pack for OSO #03 of one aircraft (HTML)"
    )
    p.add_argument("aircraft")
    p.add_argument("--out", type=Path, required=True, help="the HTML file to write")
    p.add_argument("--json", action="store_true", help="also write the pack as JSON next to it")
    p.add_argument("--as-of", help="fixed computation date (ISO 8601, UTC); default now")
    p.set_defaults(func=cmd_evidence)

    p = sub.add_parser("ask", help="ask the local model a question through the running service")
    p.add_argument("question")
    p.add_argument("--url", default="http://127.0.0.1:8000", help="the running uasw service")
    p.add_argument("--ollama", default="http://127.0.0.1:11434", help="the local Ollama server")
    p.add_argument("--model", default="qwen3:8b")
    p.add_argument("--as-of", help="fixed computation date (ISO 8601, UTC); default now")
    p.add_argument("--record", type=Path, help="save the run as a replayable recording")
    p.add_argument("--json", action="store_true", help="also print the records fetched")
    p.set_defaults(func=cmd_ask)
    return parser


def main(argv: list[str] | None = None) -> None:
    configure_logging()
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
