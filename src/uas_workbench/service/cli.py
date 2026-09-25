"""uasw: seed, serve, ingest and export the workbench.

    uasw seed            # synthetic fleet + real showcase excerpts into the SQLite store
    uasw serve           # HTTP API with /docs, /metrics; seeds first if the store is empty
    uasw ingest KEY LOG  # parse one or more logs into flight records for an aircraft
    uasw export-static   # write site/fleet.json, assistant.json and index.html from the store
    uasw ask "QUESTION"  # a local model answers through the service's read-only endpoints

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
