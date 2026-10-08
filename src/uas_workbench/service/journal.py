"""The write journal: one hash chain over every ledger entry and flight record in the store.

Each row the store writes gets one link, in the order written: a sequence number, the hash of
the row's content, the hash of the link before it, and the hash of the link itself. The head
is the last link's hash. Verifying walks the chain from the first link and recomputes every
hash from the rows as they are now, so a row edited or deleted directly in the SQLite file,
a link moved or inserted, or a row inserted with no link, is reported with the first bad
sequence number and what kind of break it is. Whatever the file holds, verifying reports and
never raises: a record that cannot be parsed or hashed (not JSON, nested too deep, a column
turned into a BLOB), a note that is not the migration note, and a note on a link that is not
a note link are each a `content` break at that link. Every stored journal column is covered:
the sequence, kind, reference and hashes by the link hash, a note link's note by its content
hash, and the note column of any other link by being NULL or a break.

What is hashed is the content the store reads, in one canonical form (JSON with sorted keys,
no spaces, ASCII only), never the bytes on disk: the columns the queries filter on and the
record JSON parsed. So a record re-serialised with the same content keeps its hash, and an
edit to a query column alone (an entry moved to another subject) does not.

The chain has no secret and nothing outside the file. So it cannot tell, on its own, a tail
cut off together with its rows from a shorter history, nor a chain recomputed from the start
by someone with the file and this code from an untouched one; both verify alone and fail only
against a head kept elsewhere and given back (`verify(head=...)`). Nor can it tell an append
made directly in the file, with its link computed as the store would, from one made through
the store. And a hash says that a row is unchanged since its link, never that what the row
states is true.

hashlib and sqlite3 only; no dependency was added.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

VERSION = "uasw-journal/1"  # the first line of every link hash: a change to the form is a new chain
GENESIS = "0" * 64  # the previous hash of the first link, and the head of an empty journal
KINDS = ("entry", "flight", "note")
SCHEMA_VERSION = 1  # PRAGMA user_version once the journal exists; 0 is a 0.5.x store

SCHEMA = """
CREATE TABLE IF NOT EXISTS journal (
    seq INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    ref INTEGER,
    note TEXT,
    content_hash TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    link_hash TEXT NOT NULL,
    UNIQUE (kind, ref)
);
"""

ENTRY_COLUMNS = "id, subject, kind, occurred_utc, recorded_utc, synthetic, supersedes, record"
FLIGHT_COLUMNS = "id, aircraft_key, log_ref, synthetic, utc_start, record"
TABLES = {"entry": ("entries", ENTRY_COLUMNS), "flight": ("flights", FLIGHT_COLUMNS)}

MIGRATION_NOTE = (
    "journal started at migration from a 0.5.x store: {flights} flights and {entries} entries "
    "were written before this link and are linked here as found, so the history before it is "
    "not tamper-evident; a change made to it before this link cannot be detected"
)


# ---- the hashes ------------------------------------------------------------------------


def canonical(content: Any) -> bytes:
    """One byte string per content: sorted keys, no spaces, ASCII escapes."""
    return json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "ascii"
    )


def content_hash(content: Any) -> str:
    return hashlib.sha256(canonical(content)).hexdigest()


def link_hash(seq: int, kind: str, ref: int | None, content: str, prev: str) -> str:
    lines = (VERSION, str(seq), kind, "" if ref is None else str(ref), content, prev)
    return hashlib.sha256("\n".join(lines).encode("ascii")).hexdigest()


def row_content(db: sqlite3.Connection, kind: str, ref: int | None) -> dict[str, Any] | None:
    """The row a link refers to, as the store reads it, or None when it is gone. A record
    column that is no longer JSON raises ValueError: the store could not read it either."""
    table, columns = TABLES[kind]
    row = db.execute(f"SELECT {columns} FROM {table} WHERE id = ?", (ref,)).fetchone()
    if row is None:
        return None
    content = dict(zip(columns.split(", "), row, strict=True))
    content["record"] = json.loads(content["record"])
    return content


# ---- writing ----------------------------------------------------------------------------


def _tail(db: sqlite3.Connection) -> tuple[int, str]:
    row = db.execute("SELECT seq, link_hash FROM journal ORDER BY seq DESC LIMIT 1").fetchone()
    return (int(row[0]), str(row[1])) if row else (0, GENESIS)


def append_link(
    db: sqlite3.Connection, kind: str, ref: int | None, note: dict[str, Any] | None = None
) -> str:
    """Link one row (or a note) after the current tail and return the new head.

    Called inside the transaction that wrote the row, after the write, so the tail is read
    under the same write lock and the link lands with the row or not at all.
    """
    if kind == "note":
        content: Any = note
    else:
        content = row_content(db, kind, ref)
        if content is None:
            raise sqlite3.IntegrityError(
                f"{kind} {ref} is not in the store, so it cannot be linked"
            )
    seq, prev = _tail(db)
    seq += 1
    chash = content_hash(content)
    lhash = link_hash(seq, kind, ref, chash, prev)
    db.execute(
        "INSERT INTO journal (seq, kind, ref, note, content_hash, prev_hash, link_hash) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            seq,
            kind,
            ref,
            canonical(note).decode("ascii") if note is not None else None,
            chash,
            prev,
            lhash,
        ),
    )
    return lhash


def migrate(db: sqlite3.Connection, now: datetime | None = None) -> None:
    """Start the journal on a store that has none. Rows written before it are linked as found,
    after a note that says so; an empty store starts with no note. Idempotent through
    PRAGMA user_version, so a journal emptied later is reported, not rebuilt."""
    if not db.in_transaction:
        db.execute("BEGIN IMMEDIATE")  # the version is read under the write lock
    version = int(db.execute("PRAGMA user_version").fetchone()[0])
    if version >= SCHEMA_VERSION:
        return
    flights = [int(r[0]) for r in db.execute("SELECT id FROM flights ORDER BY id").fetchall()]
    entries = [int(r[0]) for r in db.execute("SELECT id FROM entries ORDER BY id").fetchall()]
    if flights or entries:
        at = (now or datetime.now(UTC)).replace(microsecond=0).isoformat()
        note = {
            "note": MIGRATION_NOTE.format(flights=len(flights), entries=len(entries)),
            "flights": len(flights),
            "entries": len(entries),
            "recorded_utc": at,
        }
        append_link(db, "note", None, note)
        for ref in flights:
            append_link(db, "flight", ref)
        for ref in entries:
            append_link(db, "entry", ref)
    db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


# ---- verifying -------------------------------------------------------------------------


@dataclass(frozen=True)
class Break:
    seq: int  # the first bad sequence number
    kind: str  # sequence, duplicate, previous_hash, missing_row, content, link_hash, unjournaled
    detail: str  # `content` also covers what cannot be read at all, and a note where none belongs


@dataclass(frozen=True)
class Migration:
    seq: int
    recorded_utc: str
    flights: int
    entries: int
    note: str


@dataclass(frozen=True)
class Verification:
    ok: bool  # the chain is intact and, when a head was given, it is in the chain
    head: str  # the last link's hash, or GENESIS when the journal is empty
    length: int
    broken: Break | None  # the first break in the chain, if any
    given: str | None  # the head given to check against
    given_at: int | None  # the sequence number the given head is the hash of, if it is in the chain
    migration: Migration | None  # the note a 0.5.x store was migrated with, if any
    line: str  # what to tell a person: unchanged since a head, or how it is broken


def verify(db: sqlite3.Connection, head: str | None = None) -> Verification:
    """Walk the chain from the first link, recomputing every hash from the rows as they are.

    Reads the whole store once; the caller holds the store's write lock so no write lands
    halfway through and the result describes one state of the file.
    """
    links = db.execute(
        "SELECT seq, kind, ref, note, content_hash, prev_hash, link_hash FROM journal ORDER BY seq"
    ).fetchall()
    broken: Break | None = None
    migration: Migration | None = None
    given_at: int | None = 0 if head == GENESIS else None
    seen: set[tuple[str, int | None]] = set()
    prev = GENESIS
    expected = 1
    for raw_seq, kind, ref, note, chash, phash, lhash in links:
        seq = int(raw_seq)
        if seq != expected:
            broken = Break(expected, "sequence", f"link {expected} is missing; the next is {seq}")
            break
        if kind not in KINDS:
            broken = Break(seq, "link_hash", f"link {seq} has an unknown kind {kind!r}")
            break
        if kind != "note" and (kind, ref) in seen:
            broken = Break(seq, "duplicate", f"link {seq} links {kind} {ref}, already linked")
            break
        seen.add((kind, ref))
        if phash != prev:
            broken = Break(seq, "previous_hash", f"link {seq} does not name link {seq - 1}")
            break
        if kind != "note" and note is not None:
            broken = Break(seq, "content", f"link {seq} holds a note, which only a note link does")
            break
        broken, found = _check_content(db, seq, kind, ref, note, chash)
        if broken is not None:
            break
        if link_hash(seq, kind, ref, chash, phash) != lhash:
            broken = Break(seq, "link_hash", f"link {seq} is not the hash of what it holds")
            break
        if kind == "note":
            if migration is not None:
                broken = Break(seq, "content", f"link {seq} is a second migration note")
                break
            migration = found
        if head is not None and lhash == head:
            given_at = seq
        prev = lhash
        expected = seq + 1
    length = expected - 1
    if broken is None:
        broken = _unjournaled(db, seen, length)
    chain_ok = broken is None
    ok = chain_ok and (head is None or given_at is not None)
    return Verification(
        ok=ok,
        head=prev,
        length=length,
        broken=broken,
        given=head,
        given_at=given_at,
        migration=migration,
        line=_line(chain_ok, prev, length, broken, head, given_at),
    )


class _NotMigration(ValueError):
    """A note that is not the one `migrate` writes."""


def _migration(seq: int, content: Any) -> Migration:
    """The migration note's fields, each checked for its type. A chain recomputed from the
    file can hold any JSON in a note, so a count is taken only when it is a whole number as
    `migrate` wrote it, never as int() of whatever is there."""
    if not isinstance(content, dict):
        raise _NotMigration("it is not a JSON object")
    counts: dict[str, int] = {}
    for name in ("flights", "entries"):
        value = content.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise _NotMigration(f"its {name} count is not a whole number")
        counts[name] = value
    recorded_utc, note = content.get("recorded_utc"), content.get("note")
    if not isinstance(recorded_utc, str) or not isinstance(note, str):
        raise _NotMigration("its time or its text is not a string")
    return Migration(
        seq=seq,
        recorded_utc=recorded_utc,
        flights=counts["flights"],
        entries=counts["entries"],
        note=note,
    )


def _check_content(
    db: sqlite3.Connection, seq: int, kind: str, ref: int | None, note: Any, chash: Any
) -> tuple[Break | None, Migration | None]:
    """Read what link `seq` refers to and check it against the content hash recorded; for a
    note, also take it as the migration note. Whatever the file holds, the answer is a break
    or none: a record that is not JSON, one nested too deep for the parser (RecursionError), a
    column that cannot be hashed (TypeError from json.dumps on a BLOB) and a note that is not
    a migration note (ValueError) are each the content of that link being unreadable, reported
    at the link with the same shape as every other break, so the command, the endpoint and the
    evidence pack that embeds this result never crash on a crafted store."""
    what = "the note" if kind == "note" else f"{kind} {ref}"
    try:
        if kind == "note":
            content: Any = json.loads(note) if note is not None else None
        else:
            content = row_content(db, kind, ref)
        if content is None:
            return Break(seq, "missing_row", f"{what} of link {seq} is not in the store"), None
        if content_hash(content) != chash:
            return Break(seq, "content", f"{what} is not what link {seq} recorded"), None
        return None, _migration(seq, content) if kind == "note" else None
    except _NotMigration as exc:
        return Break(seq, "content", f"{what} of link {seq} is not a migration note: {exc}"), None
    except json.JSONDecodeError:
        return Break(seq, "content", f"{what} of link {seq} is no longer readable JSON"), None
    except RecursionError:
        return Break(seq, "content", f"{what} of link {seq} is nested too deep to read"), None
    except (ValueError, TypeError) as exc:
        return Break(seq, "content", f"{what} of link {seq} cannot be read: {exc}"), None


def _unjournaled(
    db: sqlite3.Connection, seen: set[tuple[str, int | None]], length: int
) -> Break | None:
    """A row with no link was written past the store: reported at the position after the end."""
    for kind, (table, _) in TABLES.items():
        for (ref,) in db.execute(f"SELECT id FROM {table} ORDER BY id").fetchall():
            if (kind, int(ref)) not in seen:
                return Break(length + 1, "unjournaled", f"{kind} {ref} has no journal link")
    return None


def _line(
    chain_ok: bool,
    head: str,
    length: int,
    broken: Break | None,
    given: str | None,
    given_at: int | None,
) -> str:
    links = f"{length} link{'s' if length != 1 else ''}"
    if not chain_ok and broken is not None:
        return f"journal broken at link {broken.seq}: {broken.kind}: {broken.detail}"
    if given is None:
        return (
            f"journal intact: {links}, head {head}. Keep the head: a later verify given it "
            "reports whether the records are unchanged since it, not that they are true."
        )
    if given_at is None:
        return (
            f"head {given} is not in this journal of {links}: the records were truncated or "
            "recomputed below it, or the head is another store's; the chain as it stands is "
            f"intact, head {head}."
        )
    return (
        f"unchanged since head {given} (link {given_at} of {length}): head now {head}. "
        "Unchanged says the records are as they were, not that they are true."
    )
