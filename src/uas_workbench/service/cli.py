"""uasw: seed, serve, ingest and export the workbench.

    uasw seed            # synthetic fleet + real showcase excerpts into the SQLite store
    uasw serve           # HTTP API with /docs, /metrics; seeds first if the store is empty
    uasw ingest KEY LOG  # parse one or more logs into flight records for an aircraft
    uasw export-static   # write site/fleet.json, assistant.json and index.html from the store
    uasw ask "QUESTION"  # a local model answers through the service's read-only endpoints
    uasw record ...      # append a maintenance entry: work orders, components, inspections
    uasw evidence KEY --out pack.html   # the draft evidence pack for OSO #03 of one aircraft
    uasw records KEY --out records.html # the draft records summary of one aircraft
    uasw fleet-records --out fleet-records.html # the same summary for the whole fleet
    uasw verify [--head H]  # the write journal: unchanged since a head, or the first bad link

Environment: UASW_DB (SQLite path, default data/local/fleet.sqlite), UASW_FIXTURES
(showcase excerpts; default the tests/fixtures folder beside the code in a checkout or an
editable install, else tests/fixtures in the working folder), UASW_MAX_LOG_MIB (the size
limit on one log for `ingest` and `serve`, default 512).
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

from uas_workbench.assistant import RefusedCall
from uas_workbench.fleet import load_config
from uas_workbench.fleet.showcase import showcase
from uas_workbench.fleet.synthetic import generate
from uas_workbench.flight.ardupilot import read_dataflash
from uas_workbench.flight.px4 import read_ulog
from uas_workbench.ledger import UnreadableRecord

from .limits import max_log_mib, too_large
from .observability import configure_logging
from .store import (
    AtRest,
    DuplicateFlight,
    Store,
    StoreBusy,
    StoreDamaged,
    checked_at_rest,
    unwritable_side_files,
)

log = logging.getLogger("uasw.cli")

DEFAULT_DB = os.environ.get("UASW_DB", "data/local/fleet.sqlite")


def _default_fixtures() -> Path:
    """`UASW_FIXTURES` when set; else the fixtures beside this code (a checkout, or an editable
    install of one), wherever the command is run from, where `tests/fixtures` in the working
    folder left the showcase aircraft out of a seed run from elsewhere (part of issue #73);
    else `tests/fixtures` in the working folder, as before."""
    named = os.environ.get("UASW_FIXTURES")
    if named:
        return Path(named)
    beside = Path(__file__).resolve().parents[3] / "tests" / "fixtures"
    return beside if beside.is_dir() else Path("tests/fixtures")


DEFAULT_FIXTURES = _default_fixtures()
READERS = {".ulg": read_ulog, ".bin": read_dataflash}


# The stores a command opened, closed by `main` when the command ends however it ends: a
# command that never closes its store leaves a sqlite3 connection to be collected, which
# Python 3.13+ reports as a ResourceWarning (issue #48). One process runs one command.
_opened: list[Store] = []
# The store files this process checked whole and found sound (`open_store`): a command that
# opens one store twice (`record retract` reads its target, then writes) checks it once.
_checked: set[str] = set()


class Refused(Exception):
    """A command refused on its input or on the state of its store: one
    `refused (status): detail` line on stderr and exit 1 from `main`, never a traceback
    (issue #72). The status is the one the service would answer."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def open_store(path: str, *, create: bool = True, reads_only: bool = False) -> Store:
    """The store at `path`, made there on first use by the commands that write (`seed`,
    `serve`, `ingest`, `record`); `ingest` and `record` check their input first, so one that
    is refused makes nothing (issue #83). A command that only reads passes `create=False` and is
    refused (404) when nothing is at `path`, before any file or folder is made: a mistyped
    `--db` made an empty store there, which `uasw verify` then called intact (issue #73).
    Only "nothing is there" is a 404: a path that cannot be looked at (a folder one may not
    read) goes on to the open, which refuses it (503) as before, never a traceback.

    An existing store file is checked whole before the store is opened on it
    (`checked_at_rest`), once per process, by every command, those that only read included
    (issue #107): one that fails is refused (503) naming the cause, with nothing read from it
    as records and nothing written (issues #104, #105), and, beside a `-wal` a hard stop left,
    where the damage is and that the side files go aside with the file (`_damaged`, issue
    #108). A command that only reads passes `reads_only`: its store runs no checkpoint and
    leaves a `-wal` it finds holding anything as it was, whether it answers or is refused.
    Before that check, a store whose `-shm` cannot be written is refused (503) naming the side
    files that cannot be, with nothing opened (`unwritable_side_files`, issue #112)."""
    if path != ":memory:" and not create and _nothing_at(path):
        raise Refused(
            404,
            f"there is no store at {path}; a command that only reads does not make one, so "
            "check the path, or seed or ingest to make the store",
        )
    if path != ":memory:" and path not in _checked and not _nothing_at(path):
        # Before SQLite opens anything, which would make a `-wal` beside the file (issue #112).
        unwritable = unwritable_side_files(path)
        if unwritable is not None:
            raise Refused(503, unwritable)
        with checked_at_rest(path) as found:
            if found is not None:
                raise Refused(503, _damaged(found))
            _checked.add(path)
            # Opened while the check's connection is still open (`checked_at_rest`).
            return _open(path, create, reads_only)
    return _open(path, create, reads_only)


def _damaged(found: AtRest) -> str:
    """The refusal of a store `checked_at_rest` found damaged, and what to do with a good copy.
    With a `-wal` a hard stop left beside the file, it says where the damage is (in the file,
    or only in the `-wal`, the file alone passing) and that the `-wal` and `-shm` go aside with
    the file: put beside them, a good copy would take the `-wal`'s pages in (issue #108).
    Beside a fold of the `-wal` into the file that a hard stop cut short or a reader held back,
    the file alone can fail with nothing damaged, so the line says it cannot tell where the
    damage is (issue #111)."""
    if not found.wal_kept:
        return (
            f"the store file fails SQLite's integrity check ({found.problem}), so it is "
            "damaged; nothing was read from it or written to it; restore it from a good copy"
        )
    if found.file_alone_passes:
        where = (
            f"the store's -wal, which a hard stop left beside the file, fails SQLite's integrity "
            f"check ({found.problem}) where the file alone passes it, so the store is damaged in "
            "writes held only in the -wal"
        )
    elif found.partly_folded:
        where = (
            f"the store fails SQLite's integrity check ({found.problem}), and the file holds "
            "part of what its -wal holds, as a fold of the -wal into the file leaves it when cut "
            "short by a hard stop or held back by a reader of an earlier state, which can leave "
            "the file alone failing the check with nothing damaged, so whether the damage is in "
            "the file or in the -wal cannot be told"
        )
    else:
        where = f"the store file fails SQLite's integrity check ({found.problem}), so it is damaged"
    return (
        f"{where}; nothing was read from the store or written to it, and the -wal is kept as it "
        "was; the -wal and -shm belong to the damaged store: move the file, its -wal and its "
        "-shm aside together, then put a good copy where the file was (a good copy put beside "
        "that -wal would take its pages in)"
    )


def _open(path: str, create: bool, reads_only: bool) -> Store:
    if path != ":memory:" and create:
        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            detail = f"the store {path} cannot be opened: {exc.strerror or exc}"
            raise Refused(503, detail) from exc
    store = Store(path, reads_only=reads_only)
    _opened.append(store)
    return store


def _new(path: str) -> bool:
    """Whether a command that writes would make the store at `path`: nothing is there. Such a
    command checks its input before it opens the store, so a refusal makes nothing there."""
    return path != ":memory:" and _nothing_at(path)


def _nothing_at(path: str) -> bool:
    """Whether nothing is at `path`, by the operating system's own answer: not found, or a
    folder in the path that is a file. Any other failure to look (permission refused) is not
    taken for "nothing there"; `Path.exists` raised it on Python 3.12 and hid it on 3.14."""
    try:
        os.stat(path)
    except (FileNotFoundError, NotADirectoryError):
        return True
    except OSError:
        return False
    except ValueError:  # a path the operating system cannot take at all, such as one with NUL
        return True
    return False


def _when(text: str | None, flag: str) -> datetime | None:
    """A date and time given on the command line, in UTC when it names no offset; refused
    (422), as the service refuses a date it cannot read, when it is not one."""
    if text is None:
        return None
    try:
        when = datetime.fromisoformat(text)
    except ValueError as exc:
        detail = f"{flag} {text!r} is not a date and time in ISO 8601, such as 2026-09-01T09:00Z"
        raise Refused(422, detail) from exc
    return when if when.tzinfo is not None else when.replace(tzinfo=UTC)


def _write(path: Path, text: str) -> None:
    """Write one output file, its folder made first; refused (422) when it cannot be."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except OSError as exc:
        raise Refused(422, f"cannot write {path}: {exc.strerror or exc}") from exc


def seed(store: Store, fixtures: Path) -> tuple[int, int, int] | None:
    """Seed an empty store with the synthetic fleet and the showcase in one transaction
    (`Store.batch`): a seed refused or failing partway writes nothing (issue #72). A store
    that holds anything is left as it is and what it holds is returned; None when seeded."""
    config = load_config()
    fleet = generate(config)
    real = showcase(fixtures) if fixtures.exists() else None
    if real is None:
        # Said, not silent: an installed package has no fixtures beside it (part of #73).
        log.warning(
            "no fixtures: the two showcase aircraft are left out of the seed",
            extra={"fixtures": str(fixtures), "set": "UASW_FIXTURES or --fixtures"},
        )
    with store.batch():
        held = store.holds()
        if any(held):
            return held
        store.add_fleet(fleet)
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
    return None


def cmd_seed(args: argparse.Namespace) -> None:
    held = seed(open_store(args.db), args.fixtures)
    if held is not None:
        aircraft, flights, entries = held
        raise Refused(
            409,
            f"the store already holds {aircraft} aircraft, {flights} flights and {entries} "
            "entries; a seed writes only to an empty store, so nothing was written",
        )


def cmd_serve(args: argparse.Namespace) -> None:
    import uvicorn

    from .app import create_app

    limit_mib = _log_limit()
    store = open_store(args.db)
    seed(store, args.fixtures)  # an empty store only; one that holds anything is left as is
    app = create_app(store, log_limit_mib=limit_mib)
    uvicorn.run(app, host=args.host, port=args.port, log_config=None)


def _log_limit() -> int:
    """The size limit on one log in MiB; one that cannot be read is refused (422) before
    anything is opened (issue #89)."""
    try:
        return max_log_mib()
    except ValueError as exc:
        raise Refused(422, str(exc)) from exc


def cmd_ingest(args: argparse.Namespace) -> None:
    import sys

    # Opened for the first log that reads, so logs that are all refused make no store on a
    # path with nothing at it (issue #83).
    store: Store | None = None
    refused = 0
    limit_mib = _log_limit()
    for path in args.logs:
        if _size(path) > limit_mib << 20:
            # The refusal POST /ingest gives a log over the limit (issue #89), before the
            # reader opens it.
            print(f"refused (413): {path}: {too_large(limit_mib)}", file=sys.stderr)
            refused += 1
            continue
        suffix = path.suffix.lower()
        reader = READERS.get(suffix)
        if reader is None:
            # The refusal POST /ingest gives a file it has no reader for.
            detail = f"unsupported file type {suffix!r}; expected .ulg or .bin"
            print(f"refused (422): {path}: {detail}", file=sys.stderr)
            refused += 1
            continue
        try:
            record = reader(path, licence=args.licence, attribution=args.attribution)
        except Exception as exc:
            # The refusal POST /ingest gives the same file; the next file is still read.
            detail = f"not a readable PX4 ULog or ArduPilot DataFlash log: {exc}"
            print(f"refused (422): {path}: {detail}", file=sys.stderr)
            refused += 1
            continue
        if store is None:
            store = open_store(args.db)
        try:
            # The aircraft's row, when it is new, and the flight: one transaction (issue #69).
            store.add_log(args.aircraft, record)
        except DuplicateFlight as exc:
            log.warning(str(exc))
            continue
        except StoreBusy as exc:
            # The refusal POST /ingest gives while another connection holds the store.
            print(f"refused ({exc.status}): {path}: {exc.detail}", file=sys.stderr)
            refused += 1
            continue
        log.info("ingested", extra={"aircraft_key": args.aircraft, "log_ref": record.log_ref})
    if refused:
        sys.exit(1)


def _size(path: Path) -> int:
    """The file's size; 0 for a path that cannot be looked at, which its reader refuses."""
    try:
        return path.stat().st_size
    except (OSError, ValueError):
        return 0


def cmd_export_static(args: argparse.Namespace) -> None:
    from .static_export import SAMPLE_EVIDENCE_KEY, export_static

    store = open_store(args.db, create=False, reads_only=True)
    if store.get_aircraft(SAMPLE_EVIDENCE_KEY) is None:
        # The static demo shows a sample pack and summary for the seeded fleet's SYN-04; a
        # store without it ended in a KeyError traceback. One line, nothing written.
        raise Refused(
            409,
            f"the static demo is built around the seeded fleet's {SAMPLE_EVIDENCE_KEY}, which "
            "this store does not hold; export a seeded store",
        )
    try:
        out = export_static(store, args.out)
    except OSError as exc:
        detail = f"cannot write the static site to {args.out}: {exc.strerror or exc}"
        raise Refused(422, detail) from exc
    log.info("exported", extra={"out": str(out)})


def cmd_ask(args: argparse.Namespace) -> None:
    """Ask the local model one question through the service's read-only endpoints."""
    import json
    from datetime import UTC, datetime

    from uas_workbench.assistant import OllamaBackend, ask, service_caller
    from uas_workbench.assistant.recording import from_answer, save

    caller = service_caller(args.url)
    as_of = _when(args.as_of, "--as-of") or datetime.now(UTC)
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
        try:
            save(recording, args.record)
        except OSError as exc:
            raise Refused(422, f"cannot write {args.record}: {exc.strerror or exc}") from exc
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

    store = open_store(args.db, create=False, reads_only=True)
    if store.get_aircraft(args.aircraft) is None:
        print(f"refused (404): aircraft {args.aircraft} is not in the store", file=sys.stderr)
        sys.exit(1)
    as_of = _when(args.as_of, "--as-of") or datetime.now(UTC)
    pack = build_pack(store, args.aircraft, load_config(), as_of=as_of, commit=current_commit())
    _write(args.out, render_html(pack))
    written = [str(args.out)]
    if args.json:
        path = args.out.with_suffix(".json")
        _write(path, json.dumps(pack, indent=1, ensure_ascii=False) + "\n")
        written.append(str(path))
    log.info("evidence", extra={"aircraft_key": args.aircraft, "out": written})
    print(f"{pack['title']}: written to {', '.join(written)}. {pack['statement'][1]}")


def cmd_records(args: argparse.Namespace) -> None:
    """Write the draft records summary for one aircraft as HTML, and as JSON with --json."""
    import json
    import sys
    from datetime import UTC, datetime

    from uas_workbench.evidence import build_records, render_records_html

    store = open_store(args.db, create=False, reads_only=True)
    if store.get_aircraft(args.aircraft) is None:
        print(f"refused (404): aircraft {args.aircraft} is not in the store", file=sys.stderr)
        sys.exit(1)
    as_of = _when(args.as_of, "--as-of") or datetime.now(UTC)
    pack = build_records(store, args.aircraft, load_config(), as_of=as_of, commit=current_commit())
    _write(args.out, render_records_html(pack))
    written = [str(args.out)]
    if args.json:
        path = args.out.with_suffix(".json")
        _write(path, json.dumps(pack, indent=1, ensure_ascii=False) + "\n")
        written.append(str(path))
    log.info("records", extra={"aircraft_key": args.aircraft, "out": written})
    print(
        f"{pack['title']}: {args.aircraft}: written to {', '.join(written)}. "
        f"{pack['integrity']['line']}"
    )


def cmd_fleet_records(args: argparse.Namespace) -> None:
    """Write the draft records summary of the whole fleet as HTML, and as JSON with --json."""
    import json
    from datetime import UTC, datetime

    from uas_workbench.evidence import build_fleet_records, render_fleet_records_html

    store = open_store(args.db, create=False, reads_only=True)
    as_of = _when(args.as_of, "--as-of") or datetime.now(UTC)
    summary = build_fleet_records(store, load_config(), as_of=as_of, commit=current_commit())
    _write(args.out, render_fleet_records_html(summary))
    written = [str(args.out)]
    if args.json:
        path = args.out.with_suffix(".json")
        _write(path, json.dumps(summary, indent=1, ensure_ascii=False) + "\n")
        written.append(str(path))
    log.info("fleet records", extra={"aircraft": summary["counts"]["aircraft"], "out": written})
    print(
        f"{summary['title']}: the fleet, {summary['counts']['aircraft']} aircraft: written to "
        f"{', '.join(written)}. {summary['integrity']['line']}"
    )


def cmd_verify(args: argparse.Namespace) -> None:
    """Verify the write journal; exit 1 on a break or when the given head is not in it."""
    import re
    import sys

    if args.head is not None and not re.fullmatch(r"[0-9a-f]{64}", args.head):
        print(
            "refused (422): --head must be a head as verify reports it, 64 hex digits",
            file=sys.stderr,
        )
        sys.exit(1)
    # The file whole first: the walk reads only the rows the chain covers, so it reported the
    # journal intact on a file damaged elsewhere (issue #105). A damaged file is one refusal
    # line of its own, and the walk is not run.
    result = open_store(args.db, create=False, reads_only=True).verify_journal(args.head)
    if result.migration is not None:
        m = result.migration
        print(f"link {m.seq}, recorded {m.recorded_utc}: {m.note}")
    if result.ok:
        print(result.line)
        return
    print(result.line, file=sys.stderr)
    sys.exit(1)


def _record_entry(args: argparse.Namespace, subject: str, kind_: str, **details: object) -> None:
    """Build one entry from the parsed arguments, validate and append it, and print it."""
    import sys
    from datetime import UTC, datetime

    from uas_workbench.ledger import NOTE, Entry, LedgerError, append

    now = datetime.now(UTC).replace(microsecond=0)
    given = _when(args.at, "--at")
    supersedes = getattr(args, "supersedes", None)
    config = load_config()

    def attempt(store: Store) -> Entry:
        at = given or now
        if supersedes is not None and not args.at and kind_ != "retraction":
            # A correction keeps the date of the entry it corrects; without --at, that date.
            corrected = store.entry(supersedes)
            if corrected is not None:
                at = corrected.occurred_utc
        entry = Entry(
            id=None,
            subject=subject,
            kind=kind_,
            occurred_utc=at,
            recorded_utc=now,
            entered_by=args.by,
            statement=getattr(args, "statement", "") or "",
            details={k: v for k, v in details.items() if v is not None},
            supersedes=supersedes,
            reason=getattr(args, "reason", None),
            synthetic=False,
        )
        return append(store, entry, policy=config.life, now=now, tolerance_s=config.tolerance_s)

    try:
        if _new(args.db):
            # Nothing is at the path: check the entry against an empty store first, so a
            # refusal makes no store there (issue #83); one that is accepted makes the store
            # and is checked again against it, as every append is.
            with contextlib.closing(Store(":memory:")) as empty:
                attempt(empty)
        stored = attempt(open_store(args.db))
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
        retracted = open_store(args.db, create=False).entry(args.entry_id)
        if retracted is None:
            import sys

            print(f"refused (404): entry {args.entry_id} does not exist", file=sys.stderr)
            sys.exit(1)
        args.supersedes = args.entry_id
        args.statement = ""
        _record_entry(args, retracted.subject, "retraction")
    elif what == "history":
        store = open_store(args.db, create=False, reads_only=True)
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

    p = sub.add_parser(
        "records",
        help="write the draft records summary of one aircraft (HTML): what the workbench holds "
        "against UAS.SPEC.050(1)(g), with the journal head and verify result",
    )
    p.add_argument("aircraft")
    p.add_argument("--out", type=Path, required=True, help="the HTML file to write")
    p.add_argument("--json", action="store_true", help="also write the summary as JSON next to it")
    p.add_argument("--as-of", help="fixed computation date (ISO 8601, UTC); default now")
    p.set_defaults(func=cmd_records)

    p = sub.add_parser(
        "fleet-records",
        help="write the draft records summary of the whole fleet (HTML): one row per aircraft "
        "with the statuses its own summary gives, fleet counts, one journal head and verify "
        "result for the store",
    )
    p.add_argument("--out", type=Path, required=True, help="the HTML file to write")
    p.add_argument("--json", action="store_true", help="also write the summary as JSON next to it")
    p.add_argument("--as-of", help="fixed computation date (ISO 8601, UTC); default now")
    p.set_defaults(func=cmd_fleet_records)

    p = sub.add_parser(
        "verify",
        help="verify the write journal: every entry and flight record against its hash chain",
    )
    p.add_argument(
        "--head",
        help="a head reported earlier; the result says whether the records are unchanged "
        "since it, which a truncated or recomputed chain fails",
    )
    p.set_defaults(func=cmd_verify)

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
    try:
        args.func(args)
    except UnreadableRecord as exc:
        # A stored record the store cannot read: the same refusal the service answers (503),
        # with the record named; never a traceback. `uasw verify` reports the same row.
        print(f"refused (503): {exc.detail}", file=sys.stderr)
        sys.exit(1)
    except (StoreBusy, StoreDamaged) as exc:
        # Another connection held the store's write lock past the busy timeout, at the open
        # (issue #70) or at a write: the service's 503, one line, nothing written.
        print(f"refused ({exc.status}): {exc.detail}", file=sys.stderr)
        sys.exit(1)
    except Refused as exc:
        # Bad input, or a store in a state the command cannot use (issue #72): one line.
        print(f"refused ({exc.status}): {exc.detail}", file=sys.stderr)
        sys.exit(1)
    except sqlite3.DatabaseError as exc:
        # A store file that is not a database, is damaged, cannot be opened, or is read-only
        # when written (issue #72): the service's 503, one line, nothing written.
        db = getattr(args, "db", None)
        print(f"refused (503): the store {db} cannot be read or written: {exc}", file=sys.stderr)
        sys.exit(1)
    except RefusedCall as exc:
        # The service refused a tool call of `uasw ask` (issue #46), or did not answer, or
        # the model backend failed (issue #49, the subclasses): one line with the status and
        # the sentence, exit 1; the model was not asked again and nothing was recorded.
        print(exc, file=sys.stderr)
        sys.exit(1)
    finally:
        while _opened:
            _opened.pop().close()
        _checked.clear()


if __name__ == "__main__":
    main()
