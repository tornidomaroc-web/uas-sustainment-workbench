"""The write journal: every entry and flight record is linked into one hash chain, and a change
made directly in the SQLite file is reported, with the first bad link and what kind of break.

What the journal detects, each done on the file with sqlite3 and never through the store: an
entry edited, a flight deleted, a link inserted mid-chain, a row inserted with no link, the
journal wiped. What it cannot detect on its own, and the tests say so: a truncated tail and a
chain recomputed by someone with the file and this code both verify alone, and fail only
against a head kept outside the file (`verify --head H`). The chain has no secret.

The integrity line says the records are unchanged since a head, never that they are correct.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from uas_workbench.fleet import load_config
from uas_workbench.fleet.showcase import showcase
from uas_workbench.fleet.synthetic import generate
from uas_workbench.ledger import Entry
from uas_workbench.service.app import create_app
from uas_workbench.service.cli import main
from uas_workbench.service.store import DuplicateFlight, Store

FIXTURES = Path(__file__).parent / "fixtures"
CONFIG = load_config()
HEX64 = re.compile(r"^[0-9a-f]{64}$")
AUTH = {"Authorization": "Bearer t"}  # the write token the test services are given
# The head of a freshly seeded store (synthetic fleet plus the two showcase aircraft), pinned:
# it changes only when the seed, the fixtures, the codecs or the journal's canonical form
# change, each of which CHANGELOG.md must say.
SEEDED_HEAD = "c7726f26f1d1fddd45c679b1207d1eb0cebdf9c14b682f06c28d2d48d4384991"


def seeded_memory() -> Store:
    store = Store(":memory:")
    store.add_fleet(generate(CONFIG))
    store.add_fleet(showcase(FIXTURES))
    return store


@pytest.fixture
def db(tmp_path: Path) -> str:
    path = str(tmp_path / "fleet.sqlite")
    main(["--db", path, "seed", "--fixtures", str(FIXTURES)])
    return path


def raw(path: str) -> sqlite3.Connection:
    """What anyone with the file has: a plain connection, no store, no rules."""
    return sqlite3.connect(path)


def seq_of(path: str, kind: str, ref: int) -> int:
    with raw(path) as c:
        row = c.execute(
            "SELECT seq FROM journal WHERE kind = ? AND ref = ?", (kind, ref)
        ).fetchone()
    assert row is not None, (kind, ref)
    return int(row[0])


def entry(subject: str, statement: str, at: datetime) -> Entry:
    return Entry(
        id=None,
        subject=subject,
        kind="work_order.open",
        occurred_utc=at,
        recorded_utc=at,
        entered_by="A. Tester, maintenance",
        statement=statement,
        details={"state": "deferred"},
        supersedes=None,
        reason=None,
        synthetic=False,
    )


# ---- a fresh store ----------------------------------------------------------------------


def test_a_fresh_seeded_store_verifies_and_its_head_is_deterministic() -> None:
    first, second = seeded_memory(), seeded_memory()
    a, b = first.verify_journal(), second.verify_journal()
    assert a.ok and b.ok and a.broken is None
    assert HEX64.match(a.head) and a.head == b.head
    assert a.length == b.length == first.entry_count() + first.flight_count()
    assert a.migration is None  # nothing was written before the journal existed
    assert a.head == SEEDED_HEAD
    first.close()
    second.close()


def test_every_entry_and_flight_has_one_link_and_the_chain_is_dense(db: str) -> None:
    store = Store(db)
    with raw(db) as c:
        links = c.execute("SELECT seq, kind, ref FROM journal ORDER BY seq").fetchall()
        entries = c.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
        flights = c.execute("SELECT COUNT(*) FROM flights").fetchone()[0]
    assert [s for s, _, _ in links] == list(range(1, len(links) + 1))
    assert {k for _, k, _ in links} == {"entry", "flight"}
    assert sorted(r for _, k, r in links if k == "entry") == list(range(1, entries + 1))
    assert sorted(r for _, k, r in links if k == "flight") == list(range(1, flights + 1))
    assert store.verify_journal().length == len(links)
    store.close()


def test_a_record_re_serialised_with_the_same_content_is_not_a_change(db: str) -> None:
    """The content hash is over the canonical form of what the store reads, not over the bytes
    on disk: key order and whitespace in the stored JSON are not part of the record."""
    store = Store(db)
    before = store.verify_journal().head
    with raw(db) as c:
        (text,) = c.execute("SELECT record FROM entries WHERE id = 2").fetchone()
        shuffled = json.dumps(dict(reversed(list(json.loads(text).items()))), indent=3)
        c.execute("UPDATE entries SET record = ? WHERE id = 2", (shuffled,))
        (text,) = c.execute("SELECT record FROM flights WHERE id = 2").fetchone()
        c.execute(
            "UPDATE flights SET record = ? WHERE id = 2",
            (json.dumps(json.loads(text), ensure_ascii=False, indent=1),),
        )
    after = store.verify_journal()
    assert after.ok and after.head == before
    store.close()


# ---- tampering done directly in SQLite -------------------------------------------------


def test_an_entry_edited_in_sqlite_is_detected_at_its_link(db: str) -> None:
    store = Store(db)
    assert store.verify_journal().ok
    with raw(db) as c:
        c.execute(
            "UPDATE entries SET record = replace(record, '[synthetic]', '[edited]') WHERE id = 3"
        )
    result = store.verify_journal()
    assert not result.ok and result.broken is not None
    assert result.broken.seq == seq_of(db, "entry", 3)
    assert result.broken.kind == "content"
    assert "entry 3" in result.broken.detail
    store.close()


def test_an_edit_to_a_query_column_alone_is_detected(db: str) -> None:
    """The columns the queries filter on are part of the content: moving an entry to another
    subject hides it from that aircraft's history without touching its JSON."""
    store = Store(db)
    with raw(db) as c:
        c.execute("UPDATE entries SET subject = 'SYN-07' WHERE id = 4")
    result = store.verify_journal()
    assert not result.ok and result.broken is not None
    assert (result.broken.seq, result.broken.kind) == (seq_of(db, "entry", 4), "content")
    store.close()


def test_a_flight_deleted_in_sqlite_is_detected_at_its_link(db: str) -> None:
    store = Store(db)
    with raw(db) as c:
        c.execute("DELETE FROM flights WHERE id = 5")
    result = store.verify_journal()
    assert not result.ok and result.broken is not None
    assert result.broken.seq == seq_of(db, "flight", 5)
    assert result.broken.kind == "missing_row"
    assert "flight 5" in result.broken.detail
    store.close()


def test_a_link_inserted_mid_chain_is_detected_at_the_link_after_it(db: str) -> None:
    """Someone with the file and this code inserts an entry and links it at position k, with
    every hash of that link computed correctly, shifting the links after it by one. Link k
    holds; link k+1 names link k's predecessor as its own and breaks."""
    from uas_workbench.service import journal  # the attacker's copy of the algorithm

    store = Store(db)
    k = 10
    with raw(db) as c:
        (new_id,) = c.execute("SELECT MAX(id) + 1 FROM entries").fetchone()
        c.execute(
            "INSERT INTO entries (id, subject, kind, occurred_utc, recorded_utc, synthetic, "
            "supersedes, record) SELECT ?, subject, kind, occurred_utc, recorded_utc, synthetic, "
            "supersedes, replace(record, '\"id\": 1,', '\"id\": ' || ? || ',') "
            "FROM entries WHERE id = 1",
            (new_id, new_id),
        )
        c.execute("UPDATE journal SET seq = -seq WHERE seq >= ?", (k,))
        c.execute("UPDATE journal SET seq = -seq + 1 WHERE seq < 0")
        (prev,) = c.execute("SELECT link_hash FROM journal WHERE seq = ?", (k - 1,)).fetchone()
        content = journal.content_hash(journal.row_content(c, "entry", new_id))
        link = journal.link_hash(k, "entry", new_id, content, prev)
        c.execute(
            "INSERT INTO journal (seq, kind, ref, note, content_hash, prev_hash, link_hash) "
            "VALUES (?, 'entry', ?, NULL, ?, ?, ?)",
            (k, new_id, content, prev, link),
        )
    result = store.verify_journal()
    assert not result.ok and result.broken is not None
    assert (result.broken.seq, result.broken.kind) == (k + 1, "previous_hash")
    store.close()


def test_a_link_inserted_mid_chain_with_wrong_hashes_breaks_at_itself(db: str) -> None:
    store = Store(db)
    k = 7
    with raw(db) as c:
        c.execute("UPDATE journal SET seq = -seq WHERE seq >= ?", (k,))
        c.execute("UPDATE journal SET seq = -seq + 1 WHERE seq < 0")
        c.execute(
            "INSERT INTO journal (seq, kind, ref, note, content_hash, prev_hash, link_hash) "
            "VALUES (?, 'entry', 9999, NULL, ?, ?, ?)",
            (k, "ab" * 32, "cd" * 32, "ef" * 32),
        )
    result = store.verify_journal()
    assert not result.ok and result.broken is not None
    assert (result.broken.seq, result.broken.kind) == (k, "previous_hash")
    store.close()


def test_a_row_inserted_without_a_link_is_detected_past_the_chain(db: str) -> None:
    store = Store(db)
    length = store.verify_journal().length
    with raw(db) as c:
        (new_id,) = c.execute("SELECT MAX(id) + 1 FROM entries").fetchone()
        c.execute(
            "INSERT INTO entries (id, subject, kind, occurred_utc, recorded_utc, synthetic, "
            "supersedes, record) SELECT ?, subject, kind, occurred_utc, recorded_utc, synthetic, "
            "supersedes, record FROM entries WHERE id = 1",
            (new_id,),
        )
    result = store.verify_journal()
    assert not result.ok and result.broken is not None
    assert result.broken.seq == length + 1 and result.broken.kind == "unjournaled"
    assert f"entry {new_id}" in result.broken.detail
    store.close()


def test_a_record_that_is_no_longer_json_is_a_content_break_not_a_crash(db: str) -> None:
    store = Store(db)
    with raw(db) as c:
        c.execute("UPDATE flights SET record = 'not json' WHERE id = 4")
    result = store.verify_journal()
    assert not result.ok and result.broken is not None
    assert (result.broken.seq, result.broken.kind) == (seq_of(db, "flight", 4), "content")
    assert "no longer readable JSON" in result.broken.detail
    store.close()


def test_a_link_whose_own_hash_was_edited_is_detected(db: str) -> None:
    store = Store(db)
    with raw(db) as c:
        c.execute("UPDATE journal SET link_hash = ? WHERE seq = 12", ("00" * 32,))
    result = store.verify_journal()
    assert not result.ok and result.broken is not None
    assert (result.broken.seq, result.broken.kind) == (12, "link_hash")
    store.close()


def test_a_missing_link_is_a_sequence_break(db: str) -> None:
    store = Store(db)
    with raw(db) as c:
        c.execute("DELETE FROM journal WHERE seq = 9")
    result = store.verify_journal()
    assert not result.ok and result.broken is not None
    assert (result.broken.seq, result.broken.kind) == (9, "sequence")
    store.close()


def test_a_wiped_journal_is_reported_not_rebuilt(db: str) -> None:
    """Deleting every link leaves the rows unjournaled: the store does not rebuild a chain
    over them on open, since a rebuilt chain would verify as if nothing had happened."""
    with raw(db) as c:
        c.execute("DELETE FROM journal")
    store = Store(db)
    result = store.verify_journal()
    assert not result.ok and result.broken is not None
    assert result.length == 0
    assert (result.broken.seq, result.broken.kind) == (1, "unjournaled")
    store.close()


# ---- what verifies alone and fails only against an earlier head ---------------------------


def test_a_truncated_tail_verifies_alone_and_fails_against_the_earlier_head(db: str) -> None:
    store = Store(db)
    before = store.verify_journal()
    with raw(db) as c:
        (seq, kind, ref) = c.execute(
            "SELECT seq, kind, ref FROM journal ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        c.execute("DELETE FROM journal WHERE seq = ?", (seq,))
        c.execute(f"DELETE FROM {'entries' if kind == 'entry' else 'flights'} WHERE id = ?", (ref,))
    alone = store.verify_journal()
    assert alone.ok and alone.length == before.length - 1 and alone.head != before.head
    against = store.verify_journal(head=before.head)
    assert not against.ok and against.broken is None  # the chain is intact as far as it goes
    assert against.given == before.head and against.given_at is None
    assert "not in this journal" in against.line
    # The head it still holds is found, at its own position.
    found = store.verify_journal(head=alone.head)
    assert found.ok and found.given_at == alone.length
    earlier = store.verify_journal(head=_link_hash_at(db, 5))
    assert earlier.ok and earlier.given_at == 5
    assert "unchanged since head" in earlier.line
    store.close()


def test_a_chain_recomputed_from_the_file_verifies_alone_and_fails_against_the_earlier_head(
    db: str,
) -> None:
    """Someone with the file and this code edits an entry and recomputes every link after
    it, as the store itself would have. Nothing in the file can tell; a head written down
    before the edit can."""
    from uas_workbench.service import journal  # the attacker's copy of the algorithm

    store = Store(db)
    before = store.verify_journal()
    with raw(db) as c:
        c.execute(
            "UPDATE entries SET record = replace(record, '[synthetic]', '[edited]') WHERE id = 3"
        )
        prev = journal.GENESIS
        for seq, kind, ref, note in c.execute(
            "SELECT seq, kind, ref, note FROM journal ORDER BY seq"
        ).fetchall():
            content = journal.content_hash(
                json.loads(note) if kind == "note" else journal.row_content(c, kind, ref)
            )
            link = journal.link_hash(seq, kind, ref, content, prev)
            c.execute(
                "UPDATE journal SET content_hash = ?, prev_hash = ?, link_hash = ? WHERE seq = ?",
                (content, prev, link, seq),
            )
            prev = link
    alone = store.verify_journal()
    assert alone.ok and alone.length == before.length and alone.head != before.head
    against = store.verify_journal(head=before.head)
    assert not against.ok and against.given_at is None and against.broken is None
    store.close()


def _link_hash_at(path: str, seq: int) -> str:
    with raw(path) as c:
        (h,) = c.execute("SELECT link_hash FROM journal WHERE seq = ?", (seq,)).fetchone()
    return str(h)


# ---- writes: atomic with their link, serialised under the threaded service ----------------


def test_a_refused_write_leaves_no_link(db: str) -> None:
    store = Store(db)
    before = store.verify_journal()
    record = store.flights("SYN-01")[0]
    with pytest.raises(DuplicateFlight):
        store.add_flight("SYN-01", record)
    after = store.verify_journal()
    assert after.ok and after.head == before.head and after.length == before.length
    store.close()


def test_a_row_is_not_written_when_its_link_cannot_be(
    db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from uas_workbench.service import journal

    store = Store(db)
    before = store.verify_journal()
    entries, flights = store.entry_count(), store.flight_count()

    def refuse(*args: object, **kwargs: object) -> str:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(journal, "append_link", refuse)
    with pytest.raises(sqlite3.OperationalError):
        store.append_entry(entry("SYN-01", "never stored", datetime(2026, 9, 1, tzinfo=UTC)))
    monkeypatch.undo()
    assert (store.entry_count(), store.flight_count()) == (entries, flights)
    after = store.verify_journal()
    assert after.ok and after.head == before.head
    store.close()


def test_concurrent_appends_keep_one_dense_chain(db: str) -> None:
    """The service answers on a thread pool over one connection; appends from many threads
    land one after another, each with the link that names the one before it."""
    store = Store(db)
    before = store.verify_journal()
    at = datetime(2026, 9, 1, tzinfo=UTC)
    errors: list[BaseException] = []

    def worker(n: int) -> None:
        try:
            for i in range(10):
                store.append_entry(entry("SYN-01", f"thread {n} entry {i}", at))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    after = store.verify_journal()
    assert after.ok and after.length == before.length + 80
    assert store.verify_journal(head=before.head).given_at == before.length
    store.close()


# ---- a 0.5.x store ------------------------------------------------------------------------


def make_0_5_store(path: str) -> tuple[int, int]:
    """A store as 0.5.2 left it: the same rows, no journal table, no schema version."""
    with raw(path) as c:
        flights = int(c.execute("SELECT COUNT(*) FROM flights").fetchone()[0])
        entries = int(c.execute("SELECT COUNT(*) FROM entries").fetchone()[0])
        c.execute("DROP TABLE journal")
        c.execute("PRAGMA user_version = 0")
    return flights, entries


def test_a_0_5_store_migrates_with_a_recorded_note_and_then_verifies(db: str) -> None:
    flights, entries = make_0_5_store(db)
    store = Store(db)
    result = store.verify_journal()
    assert result.ok and result.length == 1 + flights + entries
    assert result.migration is not None
    assert result.migration.seq == 1
    assert (result.migration.flights, result.migration.entries) == (flights, entries)
    assert "not tamper-evident" in result.migration.note
    assert datetime.fromisoformat(result.migration.recorded_utc).tzinfo is not None
    with raw(db) as c:
        (kind, note) = c.execute("SELECT kind, note FROM journal WHERE seq = 1").fetchone()
    assert kind == "note" and "not tamper-evident" in note
    # Opening it again migrates nothing twice, and an append continues the chain.
    store.close()
    store = Store(db)
    assert store.verify_journal().length == result.length
    store.append_entry(entry("SYN-01", "after migration", datetime(2026, 9, 1, tzinfo=UTC)))
    after = store.verify_journal(head=result.head)
    assert after.ok and after.length == result.length + 1 and after.given_at == result.length
    store.close()


def test_an_edit_before_the_migration_is_linked_as_found(db: str) -> None:
    """What the note means: a change made to a 0.5.x store before it was first opened by this
    version is linked as it is found, and verifies."""
    make_0_5_store(db)
    with raw(db) as c:
        c.execute(
            "UPDATE entries SET record = replace(record, '[synthetic]', '[edited]') WHERE id = 3"
        )
    store = Store(db)
    result = store.verify_journal()
    assert result.ok and result.migration is not None
    store.close()


def test_an_empty_0_5_store_starts_a_journal_with_no_note(tmp_path: Path) -> None:
    path = str(tmp_path / "empty.sqlite")
    Store(path).close()
    with raw(path) as c:
        c.execute("DROP TABLE journal")
        c.execute("PRAGMA user_version = 0")
    store = Store(path)
    result = store.verify_journal()
    assert result.ok and result.length == 0 and result.migration is None
    store.close()


# ---- a crafted store: verify reports, never crashes --------------------------------------


def recompute_chain(c: sqlite3.Connection) -> None:
    """Every link's hashes recomputed from the rows as they are, as the store would have
    written them: what someone with the file and this code does after an edit."""
    from uas_workbench.service import journal

    prev = journal.GENESIS
    for seq, kind, ref, note in c.execute(
        "SELECT seq, kind, ref, note FROM journal ORDER BY seq"
    ).fetchall():
        content = journal.content_hash(
            json.loads(note) if kind == "note" else journal.row_content(c, kind, ref)
        )
        link = journal.link_hash(seq, kind, ref, content, prev)
        c.execute(
            "UPDATE journal SET content_hash = ?, prev_hash = ?, link_hash = ? WHERE seq = ?",
            (content, prev, link, seq),
        )
        prev = link


def craft_nested_record(path: str) -> tuple[int, str]:
    """An entry's record replaced by JSON nested 100,000 deep: json.loads raises
    RecursionError on it."""
    with raw(path) as c:
        c.execute("UPDATE entries SET record = ? WHERE id = 3", ("[" * 100_000 + "]" * 100_000,))
    return seq_of(path, "entry", 3), "entry 3"


def craft_bytes_column(path: str) -> tuple[int, str]:
    """A flight's query column replaced by a BLOB: the row reads, and json.dumps raises
    TypeError on it."""
    with raw(path) as c:
        c.execute("UPDATE flights SET aircraft_key = X'00ff' WHERE id = 4")
    return seq_of(path, "flight", 4), "flight 4"


def craft_note_counts(path: str) -> tuple[int, str]:
    """The chain recomputed from the file behind a note whose counts are words, not numbers:
    int('many') raises ValueError."""
    with raw(path) as c:
        links = c.execute("SELECT seq, kind, ref FROM journal ORDER BY seq").fetchall()
        c.execute("DELETE FROM journal")
        note = {"note": "x", "flights": "many", "entries": "some", "recorded_utc": "2026"}
        c.execute(
            "INSERT INTO journal (seq, kind, ref, note, content_hash, prev_hash, link_hash) "
            "VALUES (1, 'note', NULL, ?, '', '', '')",
            (json.dumps(note),),
        )
        for seq, kind, ref in links:
            c.execute(
                "INSERT INTO journal (seq, kind, ref, note, content_hash, prev_hash, link_hash) "
                "VALUES (?, ?, ?, NULL, '', '', '')",
                (seq + 1, kind, ref),
            )
        recompute_chain(c)
    return 1, "the note"


def craft_note_counts_wrong_type(path: str) -> tuple[int, str]:
    """As above with counts that int() would accept: a list, a bool and a float are not
    counts either, and the note must not be trusted."""
    with raw(path) as c:
        links = c.execute("SELECT seq, kind, ref FROM journal ORDER BY seq").fetchall()
        c.execute("DELETE FROM journal")
        note = {"note": "x", "flights": [1], "entries": True, "recorded_utc": 2026}
        c.execute(
            "INSERT INTO journal (seq, kind, ref, note, content_hash, prev_hash, link_hash) "
            "VALUES (1, 'note', NULL, ?, '', '', '')",
            (json.dumps(note),),
        )
        for seq, kind, ref in links:
            c.execute(
                "INSERT INTO journal (seq, kind, ref, note, content_hash, prev_hash, link_hash) "
                "VALUES (?, ?, ?, NULL, '', '', '')",
                (seq + 1, kind, ref),
            )
        recompute_chain(c)
    return 1, "the note"


CRAFTS = [craft_nested_record, craft_bytes_column, craft_note_counts, craft_note_counts_wrong_type]


@pytest.mark.parametrize("craft", CRAFTS, ids=[c.__name__ for c in CRAFTS])
def test_a_crafted_store_is_a_content_break_at_its_link_never_a_crash(
    db: str, craft: Callable[[str], tuple[int, str]], capsys: pytest.CaptureFixture[str]
) -> None:
    """ValueError, TypeError and RecursionError raised while reading or hashing what a link
    refers to are the store's content being unreadable, and are reported as such: the same
    result shape as every other break, from the library, the command and the endpoint. Part 2
    embeds this result in the evidence pack, which must not crash on a crafted store either."""
    store = Store(db)
    client = TestClient(create_app(store, write_token="t"))  # running when the file is edited
    assert store.verify_journal().ok
    seq, what = craft(db)
    result = store.verify_journal()
    assert not result.ok and result.broken is not None
    assert (result.broken.seq, result.broken.kind) == (seq, "content")
    assert what in result.broken.detail
    assert result.line.startswith(f"journal broken at link {seq}: content:")
    assert result.migration is None  # a note that cannot be read is not a migration
    with pytest.raises(SystemExit) as exit_:
        main(["--db", db, "verify"])
    assert exit_.value.code == 1
    assert capsys.readouterr().err.startswith(f"journal broken at link {seq}: content:")
    r = client.get("/journal/verify", headers=AUTH)
    assert r.status_code == 200, r.text
    assert r.json()["broken"] == {"seq": seq, "kind": "content", "detail": result.broken.detail}
    store.close()


def test_a_note_on_a_link_that_is_not_a_note_is_a_break(db: str) -> None:
    """Every stored journal column is hashed or refused. A note link's note is its hashed
    content; on an entry or flight link the column is NULL, and any value put there is a
    break at that link, since nothing in the chain covers it."""
    store = Store(db)
    assert store.verify_journal().ok
    with raw(db) as c:
        c.execute("UPDATE journal SET note = 'anything at all' WHERE seq = 5")
    result = store.verify_journal()
    assert not result.ok and result.broken is not None
    assert (result.broken.seq, result.broken.kind) == (5, "content")
    assert "note" in result.broken.detail
    with raw(db) as c:
        c.execute("UPDATE journal SET note = NULL WHERE seq = 5")
        c.execute("UPDATE journal SET note = '' WHERE seq = 9")  # empty is still a value
    result = store.verify_journal()
    assert not result.ok and result.broken is not None and result.broken.seq == 9
    store.close()


# ---- the endpoint and the command -----------------------------------------------------


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(create_app(seeded_memory(), write_token="t"))


def test_the_endpoint_reports_head_length_and_the_result(client: TestClient) -> None:
    r = client.get("/journal/verify", headers=AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["broken"] is None and body["migration"] is None
    assert HEX64.match(body["head"]) and body["length"] > 0
    assert body["given"] is None and body["given_at"] is None
    assert "unchanged" not in body["line"] or "head" in body["line"]
    same = client.get("/journal/verify", params={"head": body["head"]}, headers=AUTH).json()
    assert same["ok"] is True and same["given_at"] == body["length"]
    assert f"unchanged since head {body['head']}" in same["line"]
    other = client.get("/journal/verify", params={"head": "f" * 64}, headers=AUTH).json()
    assert other["ok"] is False and other["given_at"] is None and other["broken"] is None
    assert "not in this journal" in other["line"]
    assert client.get("/journal/verify", params={"head": "zz"}, headers=AUTH).status_code == 422
    assert "/journal/verify" in client.get("/openapi.json").json()["paths"]


def test_the_endpoint_is_guarded_like_a_write() -> None:
    """Verifying reads every row and holds the store's write lock meanwhile, so a caller who
    can reach it can stall writes for as long as the walk takes; with a token set it needs the
    token, and with none set it answers this machine only, exactly as POST /entries does."""
    with_token = TestClient(create_app(seeded_memory(), write_token="t"))
    assert with_token.get("/journal/verify").status_code == 401
    assert (
        with_token.get("/journal/verify", headers={"Authorization": "Bearer wrong"}).status_code
        == 401
    )
    assert with_token.get("/journal/verify", headers=AUTH).status_code == 200
    assert with_token.get("/entries").status_code == 200  # reads stay open
    app = create_app(seeded_memory(), write_token=None)
    remote = TestClient(app, client=("172.17.0.1", 40000))
    r = remote.get("/journal/verify")
    assert r.status_code == 403 and "UASW_WRITE_TOKEN" in r.json()["detail"]
    local = TestClient(app, client=("127.0.0.1", 40000))
    assert local.get("/journal/verify").status_code == 200


def test_the_endpoint_reads_the_store_as_it_is_and_never_claims_truth(tmp_path: Path) -> None:
    path = str(tmp_path / "fleet.sqlite")
    main(["--db", path, "seed", "--fixtures", str(FIXTURES)])
    store = Store(path)
    client = TestClient(create_app(store, write_token="t"))
    ok = client.get("/journal/verify", headers=AUTH).json()
    assert ok["ok"] is True
    assert "not that they are true" in ok["line"]
    assert not re.search(r"\b(correct|proves?|proof)\b", ok["line"])
    with raw(path) as c:
        c.execute("DELETE FROM flights WHERE id = 2")
    broken = client.get("/journal/verify", headers=AUTH).json()
    assert broken["ok"] is False
    assert broken["broken"] == {
        "seq": seq_of(path, "flight", 2),
        "kind": "missing_row",
        "detail": broken["broken"]["detail"],
    }
    assert "flight 2" in broken["broken"]["detail"]
    assert broken["line"].startswith(f"journal broken at link {seq_of(path, 'flight', 2)}")
    store.close()


def test_existing_endpoints_carry_no_journal_field(client: TestClient) -> None:
    """The journal is read at /journal/verify only; every other response keeps its shape."""
    assert set(client.get("/health").json()) == {"status", "version", "aircraft", "flights"}
    entry_keys = set(client.get("/entries").json()[0])
    assert not {k for k in entry_keys if "hash" in k or "journal" in k or "seq" in k}
    flight_keys = set(client.get("/aircraft/SYN-01/flights").json()[0])
    assert not {k for k in flight_keys if "hash" in k or "journal" in k or "seq" in k}


def test_the_command_verifies_and_exits_non_zero_on_a_break(
    db: str, capsys: pytest.CaptureFixture[str]
) -> None:
    main(["--db", db, "verify"])
    out = capsys.readouterr().out
    head = re.search(r"head ([0-9a-f]{64})", out)
    assert head is not None and "intact" in out and "unchanged since head" not in out
    assert "not that they are" in out  # the line never claims the records are correct
    main(["--db", db, "verify", "--head", head[1]])
    out = capsys.readouterr().out
    assert f"unchanged since head {head[1]}" in out
    with pytest.raises(SystemExit) as exit_:
        main(["--db", db, "verify", "--head", "e" * 64])
    assert exit_.value.code == 1
    assert "not in this journal" in capsys.readouterr().err
    with pytest.raises(SystemExit) as exit_:
        main(["--db", db, "verify", "--head", "not-a-head"])
    assert exit_.value.code == 1
    assert "refused (422)" in capsys.readouterr().err
    with raw(db) as c:
        c.execute("UPDATE entries SET record = replace(record, '[synthetic]', '[x]') WHERE id = 6")
    with pytest.raises(SystemExit) as exit_:
        main(["--db", db, "verify"])
    assert exit_.value.code == 1
    err = capsys.readouterr().err
    assert err.startswith(f"journal broken at link {seq_of(db, 'entry', 6)}: content")


def test_the_command_reports_a_migration(db: str, capsys: pytest.CaptureFixture[str]) -> None:
    flights, entries = make_0_5_store(db)
    main(["--db", db, "verify"])
    out = capsys.readouterr().out
    assert "intact" in out
    assert f"{flights} flights and {entries} entries" in out and "not tamper-evident" in out
