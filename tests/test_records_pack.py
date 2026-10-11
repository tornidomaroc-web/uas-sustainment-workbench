"""The draft records summary of one aircraft: what this workbench holds against the record
keeping points of UAS.SPEC.050(1)(g) of Regulation (EU) 2019/947, with the write journal's
head and verify result in it.

What it must do: say in its title that it is a draft for the operator's own review; keep to
four statuses and three span words; say "unchanged since head H" when the journal verifies
and name the break when it does not; say "not known" for whatever a record the store cannot
read leaves unknown; never state or imply that a record is true or that anything is
compliant, approved or certified; and never let an unauthenticated caller make the service
walk the whole journal on every request.

Every assertion here holds on Windows and Linux alike: no path, no OS error text, no line
ending is compared; files are read back as UTF-8 and parsed.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from uas_workbench.evidence import (
    RECORDS_STATUSES,
    RECORDS_TITLE,
    RETENTION_YEARS,
    SPAN_STATUSES,
    build_pack,
    build_records,
    render_records_html,
)
from uas_workbench.fleet import load_config
from uas_workbench.fleet.model import Aircraft
from uas_workbench.fleet.showcase import ALFA_KEY, PX4_KEY, showcase
from uas_workbench.fleet.synthetic import generate
from uas_workbench.ledger import Entry
from uas_workbench.service import journal
from uas_workbench.service.app import create_app
from uas_workbench.service.cli import main
from uas_workbench.service.static_export import export_static
from uas_workbench.service.store import Store

FIXTURES = Path(__file__).parent / "fixtures"
CONFIG = load_config()
AS_OF = datetime(2026, 10, 1, tzinfo=UTC)
NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
AS_OF_TEXT = "2026-10-01T00:00:00Z"
AUTH = {"Authorization": "Bearer t"}
HEX64 = re.compile(r"^[0-9a-f]{64}$")
# The words no pack output may carry, as whole words, in any case: a records tool gathers and
# never finds. "compliance" in "does not show compliance" is not "compliant"; this is a word
# list, not a stem list, and the test says exactly which word it found.
FORBIDDEN = re.compile(
    r"\b(compliant|complies|meets|approved|certified|ready for submission)\b", re.I
)
BROKEN = re.compile(r"\[object Object\]|\bundefined\b|\bNaN\b|\bNone\b")
SEEDED_HEAD = "c7726f26f1d1fddd45c679b1207d1eb0cebdf9c14b682f06c28d2d48d4384991"
SYN04_OSO_HASH = "3515dcb1fc7f1282c73fad1c6fbc9ebe39df61537c5629145a4b55d96f42eabf"


@pytest.fixture(scope="module")
def store() -> Store:
    s = Store(":memory:")
    s.add_fleet(generate(CONFIG))
    s.add_fleet(showcase(FIXTURES))
    return s


@pytest.fixture
def db(tmp_path: Path) -> str:
    path = str(tmp_path / "fleet.sqlite")
    main(["--db", path, "seed", "--fixtures", str(FIXTURES)])
    return path


@contextmanager
def raw(path: str) -> Iterator[sqlite3.Connection]:
    """What anyone with the file has: a plain connection, no store, no rules."""
    connection = sqlite3.connect(path)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def records(store: Store, key: str, as_of: datetime = AS_OF) -> dict[str, Any]:
    return build_records(store, key, CONFIG, as_of=as_of, commit="abc1234", generated_utc=NOW)


def every_text(pack: dict[str, Any]) -> list[str]:
    """Every form a pack is shown in: the data and the page."""
    return [json.dumps(pack, ensure_ascii=False), render_records_html(pack)]


def file_head(path: str) -> str:
    with raw(path) as c:
        row = c.execute("SELECT link_hash FROM journal ORDER BY seq DESC LIMIT 1").fetchone()
    return str(row[0])


def entry(subject: str, at: datetime, statement: str = "a record") -> Entry:
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


def one_aircraft_store(first_record: datetime | None) -> Store:
    """A store with one aircraft that has no flight and, when a date is given, one maintenance
    entry at that date; nothing else."""
    s = Store(":memory:")
    s.add_aircraft(
        Aircraft(
            key="ONE-01",
            label="one aircraft",
            source="px4",
            synthetic=True,
            licence="none",
            attribution="generated for a test",
        )
    )
    if first_record is not None:
        s.append_entry(entry("ONE-01", first_record))
    return s


# ---- the words ---------------------------------------------------------------------------


def test_the_title_is_exactly_the_draft_wording(store: Store) -> None:
    p = records(store, "SYN-04")
    assert p["title"] == "Draft records summary for the operator's own review"
    assert p["title"] == RECORDS_TITLE
    assert p["aircraft"]["key"] == "SYN-04"
    statement = " ".join(p["statement"])
    assert "draft" in statement and "operator's own review" in statement
    assert "does not certify airworthiness or return to service" in statement
    assert "the authority decides" in statement
    html = render_records_html(p)
    body = re.sub(r"<[^>]+>", " ", html.split("<body", 1)[1])
    assert body.strip().startswith(("Synthetic data", RECORDS_TITLE))
    assert RECORDS_TITLE in body[:600] and "SYN-04" in body[:900]


@pytest.mark.parametrize("key", ["SYN-01", "SYN-04", "SYN-06", ALFA_KEY, PX4_KEY])
def test_no_pack_output_carries_a_forbidden_phrase(store: Store, key: str) -> None:
    """Over the data, the page, and for the demo sample the command line and the service."""
    for text in every_text(records(store, key)):
        found = FORBIDDEN.search(text)
        assert found is None, found.group(0)


def test_the_command_line_and_the_service_outputs_carry_no_forbidden_phrase(
    db: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "records.html"
    main(["--db", db, "records", "SYN-04", "--out", str(out), "--json", "--as-of", AS_OF_TEXT])
    captured = capsys.readouterr()
    assert "written to" in captured.out
    texts = [
        captured.out,
        captured.err,
        out.read_text(encoding="utf-8"),
        out.with_suffix(".json").read_text(encoding="utf-8"),
    ]
    store = Store(db)
    client = TestClient(create_app(store, write_token="t"))
    for path in ("/aircraft/SYN-04/records", "/aircraft/SYN-04/records.html"):
        r = client.get(path, params={"as_of": AS_OF_TEXT})
        assert r.status_code == 200, r.text[:200]
        texts.append(r.text)
    for text in texts:
        found = FORBIDDEN.search(text)
        assert found is None, found.group(0)
    store.close()


def test_the_existing_oso_pack_carries_no_forbidden_phrase_either(store: Store) -> None:
    """The same word list over the OSO #03 pack, whose source titles say "Means of Compliance"
    (a document's name) and whose statement says "does not show compliance"."""
    p = build_pack(store, "SYN-04", CONFIG, as_of=AS_OF, commit="abc1234", generated_utc=NOW)
    from uas_workbench.evidence import render_html

    for text in (json.dumps(p, ensure_ascii=False), render_html(p)):
        found = FORBIDDEN.search(text)
        assert found is None, found.group(0)


@pytest.mark.parametrize("key", ["SYN-01", "SYN-04", "SYN-06", ALFA_KEY, PX4_KEY])
def test_every_status_is_one_of_four_words_and_every_span_one_of_three(
    store: Store, key: str
) -> None:
    assert RECORDS_STATUSES == (
        "supported by these records",
        "partly",
        "not evidenced by this workbench",
        "not known",
    )
    assert SPAN_STATUSES == ("covered", "not yet 3 years", "not known")
    p = records(store, key)
    assert p["items"], key
    for item in p["items"]:
        assert item["status"] in RECORDS_STATUSES, item
        assert item["reason"], item["id"]
        assert item["point"].startswith("UAS.SPEC.050(1)(g)"), item["id"]
    assert p["retention"]["status"] in SPAN_STATUSES
    assert p["retention"]["minimum_years"] == RETENTION_YEARS == 3
    html = render_records_html(p)
    assert BROKEN.search(re.sub(r"<[^>]+>", " ", html.split("<body", 1)[1])) is None
    # The page shows each status as one of the four words, nothing in between.
    shown = re.findall(r'class="status [^"]*">([^<]+)<', html)
    assert shown and set(shown) <= set(RECORDS_STATUSES), shown


def test_what_each_aircraft_evidences(store: Store) -> None:
    """A synthetic aircraft with a ledger and logs: maintenance activities are supported by
    these records, operations partly (the logs are information on operations and what the
    autopilot reported; no occurrence report is held), training not evidenced. A real
    showcase aircraft has logs and no ledger."""
    by_id = {i["id"]: i for i in records(store, "SYN-04")["items"]}
    assert set(by_id) == {"training-records", "maintenance-activities", "operations-occurrences"}
    assert by_id["training-records"]["status"] == "not evidenced by this workbench"
    assert by_id["maintenance-activities"]["status"] == "supported by these records"
    assert by_id["operations-occurrences"]["status"] == "partly"
    assert "occurrence" in by_id["operations-occurrences"]["reason"]
    real = {i["id"]: i for i in records(store, ALFA_KEY)["items"]}
    assert real["maintenance-activities"]["status"] == "not evidenced by this workbench"
    assert real["operations-occurrences"]["status"] == "partly"
    p = records(store, "SYN-04")
    assert p["maintenance"]["entries"] == 6
    assert p["operations"]["flights"] == 3
    assert p["operations"]["fault_events"] == []  # SYN-04's logs report none
    assert p["operations"]["reconcile_findings"]  # the unlogged flight on SYN-04
    assert p["operations"]["grounded_findings"] == []


def test_the_sources_are_cited_and_paraphrased_and_no_other_oso_is_named(store: Store) -> None:
    p = records(store, "SYN-04")
    assert p["sources"] and all(s["url"].startswith("https://") for s in p["sources"])
    joined = json.dumps(p)
    assert "2019/947" in joined and "UAS.SPEC.050" in joined and "2020/639" in joined
    assert "OSO #23" not in joined and "OSO#23" not in joined
    for item in p["items"]:
        assert len(item["text"]) < 300, item["id"]  # a paraphrase, not a copy
    assert "paraphrase" in " ".join(s["note"] for s in p["sources"]).lower()


# ---- integrity: the journal head and the verify result -----------------------------------


def test_the_integrity_section_says_unchanged_since_head_when_the_journal_verifies(
    store: Store,
) -> None:
    p = records(store, "SYN-04")
    integrity = p["integrity"]
    assert integrity["ok"] is True and integrity["broken"] is None
    assert HEX64.match(integrity["head"]) and integrity["head"] == SEEDED_HEAD
    assert integrity["length"] == store.entry_count() + store.flight_count()
    assert integrity["line"].startswith(f"unchanged since head {SEEDED_HEAD}")
    assert "not that they are true" in integrity["line"]
    html = render_records_html(p)
    assert f"unchanged since head {SEEDED_HEAD}" in html


def test_a_broken_journal_is_shown_as_a_break_never_as_unchanged(db: str) -> None:
    with raw(db) as c:
        c.execute(
            "UPDATE entries SET record = replace(record, '[synthetic]', '[edited]') WHERE id = 3"
        )
        (seq,) = c.execute("SELECT seq FROM journal WHERE kind = 'entry' AND ref = 3").fetchone()
    store = Store(db)
    p = records(store, "SYN-04")
    integrity = p["integrity"]
    assert integrity["ok"] is False
    assert integrity["broken"] == {
        "seq": seq,
        "kind": "content",
        "detail": integrity["broken"]["detail"],
    }
    assert "entry 3" in integrity["broken"]["detail"]
    assert integrity["line"].startswith(f"journal broken at link {seq}: content:")
    for text in every_text(p):
        assert "unchanged since head" not in text
        assert f"journal broken at link {seq}" in text
    # The same from the service, to a caller with no token.
    client = TestClient(create_app(store, write_token="t"))
    body = client.get("/aircraft/SYN-04/records").json()
    assert body["integrity"]["ok"] is False and body["integrity"]["broken"]["seq"] == seq
    store.close()


def test_a_journal_wiped_or_truncated_is_reported_as_the_journal_says(db: str) -> None:
    with raw(db) as c:
        c.execute("DELETE FROM journal")
    store = Store(db)
    integrity = records(store, "SYN-04")["integrity"]
    assert integrity["ok"] is False and integrity["broken"]["kind"] == "unjournaled"
    assert integrity["length"] == 0
    store.close()


# ---- the 3-year span ---------------------------------------------------------------------


def test_the_span_of_the_demo_records_is_not_yet_3_years(store: Store) -> None:
    p = records(store, "SYN-04")
    r = p["retention"]
    assert r["status"] == "not yet 3 years"
    assert r["earliest_utc"] == "2026-01-19T00:00:00Z" and r["earliest_kind"] == "entry"
    assert r["threshold_utc"] == "2023-10-01T00:00:00Z"  # as_of less 3 calendar years
    assert r["days"] == (AS_OF - datetime(2026, 1, 19, tzinfo=UTC)).days
    assert "3 years" in r["reason"]


def test_a_real_aircraft_whose_logs_go_back_8_years_is_covered_and_one_of_this_year_is_not(
    store: Store,
) -> None:
    alfa = records(store, ALFA_KEY)["retention"]
    assert alfa["status"] == "covered" and alfa["earliest_kind"] == "flight"
    assert alfa["earliest_utc"].startswith("2018-07-30")
    px4 = records(store, PX4_KEY)["retention"]
    assert px4["status"] == "not yet 3 years" and px4["earliest_utc"].startswith("2026-06-12")


@pytest.mark.parametrize(
    ("first", "as_of", "status"),
    [
        # Exactly 3 calendar years is the minimum reached: covered.
        (datetime(2023, 10, 1, tzinfo=UTC), AS_OF, "covered"),
        # One second inside 3 years: not yet.
        (datetime(2023, 10, 1, 0, 0, 1, tzinfo=UTC), AS_OF, "not yet 3 years"),
        # One day over: covered.
        (datetime(2023, 9, 30, tzinfo=UTC), AS_OF, "covered"),
        # A record on a leap day; 3 years later there is no 29 February, so the threshold is
        # 28 February 2027: on 28 February 2027 the span is covered, and on 27 it is not.
        (datetime(2024, 2, 29, tzinfo=UTC), datetime(2027, 2, 28, tzinfo=UTC), "covered"),
        (datetime(2024, 2, 29, tzinfo=UTC), datetime(2027, 2, 27, tzinfo=UTC), "not yet 3 years"),
        # as_of on a leap day: 3 years back is 28 February, the last day that exists.
        (datetime(2025, 2, 28, tzinfo=UTC), datetime(2028, 2, 29, tzinfo=UTC), "covered"),
        (datetime(2025, 3, 1, tzinfo=UTC), datetime(2028, 2, 29, tzinfo=UTC), "not yet 3 years"),
        # A record dated after as_of does not count: nothing is held at as_of, so not known.
        (AS_OF + timedelta(days=1), AS_OF, "not known"),
    ],
)
def test_span_boundaries(first: datetime, as_of: datetime, status: str) -> None:
    s = one_aircraft_store(first)
    r = records(s, "ONE-01", as_of)["retention"]
    assert r["status"] == status, r
    if status != "not known":
        assert r["earliest_utc"] == first.strftime("%Y-%m-%dT%H:%M:%SZ")
        assert r["days"] == (as_of - first).days
    s.close()


def test_an_aircraft_with_no_record_has_a_span_that_is_not_known() -> None:
    s = one_aircraft_store(None)
    p = records(s, "ONE-01")
    r = p["retention"]
    assert r["status"] == "not known"
    assert r["earliest_utc"] is None and r["days"] is None
    assert "no record" in r["reason"]
    assert {i["status"] for i in p["items"]} == {"not evidenced by this workbench"}
    s.close()


# ---- a record the store cannot read: "not known", never a guess --------------------------


def test_an_unreadable_entry_gives_not_known_and_names_the_record(db: str) -> None:
    with raw(db) as c:
        c.execute("UPDATE entries SET record = ? WHERE id = 4", ("[" * 100_000 + "]" * 100_000,))
    store = Store(db)
    p = records(store, "SYN-01")
    by_id = {i["id"]: i for i in p["items"]}
    assert by_id["maintenance-activities"]["status"] == "not known"
    assert "entry 4" in by_id["maintenance-activities"]["reason"]
    assert "cannot be read" in by_id["maintenance-activities"]["reason"]
    assert p["maintenance"]["entries"] == {"unknown": by_id["maintenance-activities"]["reason"]}
    # The span needs the earliest record, which the ledger may hold: not known, with the record.
    assert p["retention"]["status"] == "not known"
    assert "entry 4" in p["retention"]["reason"]
    # The flights read: operations are reported, and each log is not judged for grounding.
    assert by_id["operations-occurrences"]["status"] == "partly"
    assert p["operations"]["flights"] == 3
    # The journal names the same row as a content break.
    assert p["integrity"]["ok"] is False and p["integrity"]["broken"]["kind"] == "content"
    for text in every_text(p):
        assert "entry 4" in text and BROKEN.search(re.sub(r"<[^>]+>", " ", text)) is None
        assert FORBIDDEN.search(text) is None
    store.close()


def test_an_unreadable_flight_gives_not_known_for_operations_and_the_span(db: str) -> None:
    with raw(db) as c:
        c.execute("UPDATE flights SET record = 'not json' WHERE id = 2")
    store = Store(db)
    p = records(store, "SYN-01")
    by_id = {i["id"]: i for i in p["items"]}
    assert by_id["operations-occurrences"]["status"] == "not known"
    assert "flight 2" in by_id["operations-occurrences"]["reason"]
    assert isinstance(p["operations"]["flights"], dict) and "unknown" in p["operations"]["flights"]
    assert p["retention"]["status"] == "not known" and "flight 2" in p["retention"]["reason"]
    assert by_id["maintenance-activities"]["status"] == "supported by these records"
    client = TestClient(create_app(store, write_token="t"))
    r = client.get("/aircraft/SYN-01/records", params={"as_of": AS_OF_TEXT})
    assert r.status_code == 200, r.text[:200]  # the pack is written; it says what it cannot know
    assert r.json()["retention"]["status"] == "not known"
    store.close()


# ---- the cost of verify is bounded ---------------------------------------------------------


def count_walks(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Every walk of the journal from now on."""
    seen: list[int] = []
    walk = journal.verify

    def counted(db: sqlite3.Connection, head: str | None = None) -> journal.Verification:
        seen.append(1)
        return walk(db, head)

    monkeypatch.setattr(journal, "verify", counted)
    return seen


def test_an_unauthenticated_caller_never_makes_the_service_walk_the_journal(
    db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pack needs no token, so it answers from a verify result the store already holds:
    walked once at startup and again only after another connection has changed the file,
    never because the pack was asked for again. Twenty requests, zero walks."""
    store = Store(db)
    app = create_app(store, write_token="t")
    client = TestClient(app, client=("172.17.0.1", 40000))  # not loopback, no token
    walks = count_walks(monkeypatch)
    heads = set()
    for _ in range(10):
        heads.add(client.get("/aircraft/SYN-04/records").json()["integrity"]["head"])
        assert client.get("/aircraft/SYN-04/records.html").status_code == 200
    assert heads == {SEEDED_HEAD}
    assert sum(walks) == 0, f"{sum(walks)} walks for 20 unauthenticated requests"
    assert client.get("/journal/verify").status_code == 403  # the guarded route stays guarded
    store.close()


def test_a_write_through_the_store_moves_the_head_without_a_walk(
    db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The store appended the link itself, so the chain it verified is still intact with one
    more link: the head moves, and no row is read again."""
    store = Store(db)
    client = TestClient(create_app(store, write_token="t"))
    assert client.get("/aircraft/SYN-02/records").json()["integrity"]["head"] == SEEDED_HEAD
    walks = count_walks(monkeypatch)
    r = client.post(
        "/entries",
        json={
            "subject": "SYN-02",
            "kind": "work_order.open",
            "occurred_utc": "2026-09-20T09:00:00Z",
            "entered_by": "A. Tester, maintenance",
            "statement": "a write made through the store",
            "details": {"state": "in_work"},
        },
        headers=AUTH,
    )
    assert r.status_code == 201, r.text
    integrity = client.get("/aircraft/SYN-02/records").json()["integrity"]
    assert integrity["ok"] is True
    assert integrity["head"] == file_head(db) != SEEDED_HEAD
    assert integrity["length"] == store.entry_count() + store.flight_count()
    assert sum(walks) == 0, f"{sum(walks)} walks after a write through the store"
    store.close()


def test_an_edit_made_in_the_file_is_met_with_exactly_one_walk(
    db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Store(db)
    client = TestClient(create_app(store, write_token="t"))
    assert client.get("/aircraft/SYN-04/records").json()["integrity"]["ok"] is True
    walks = count_walks(monkeypatch)
    with raw(db) as c:
        c.execute("DELETE FROM flights WHERE id = 5")
    first = client.get("/aircraft/SYN-04/records").json()["integrity"]
    assert first["ok"] is False and first["broken"]["kind"] == "missing_row"
    for _ in range(5):
        assert client.get("/aircraft/SYN-04/records").json()["integrity"] == first
    assert sum(walks) == 1, f"{sum(walks)} walks for one edit and six requests"
    store.close()


def test_the_cached_result_is_the_walk_itself(db: str) -> None:
    """What the pack shows is exactly what `uasw verify` and GET /journal/verify compute."""
    store = Store(db)
    cached = store.journal_state()
    walked = store.verify_journal()
    assert cached == walked
    with raw(db) as c:
        c.execute("UPDATE journal SET link_hash = ? WHERE seq = 12", ("00" * 32,))
    assert store.journal_state() == store.verify_journal()
    assert store.journal_state().broken is not None
    store.close()


# ---- the surfaces: service, command line, static export ----------------------------------


def test_the_routes_answer_json_and_html_and_404_an_unknown_aircraft(store: Store) -> None:
    client = TestClient(create_app(store, write_token="t"))
    r = client.get("/aircraft/SYN-04/records", params={"as_of": AS_OF_TEXT})
    assert r.status_code == 200
    body = r.json()
    assert body["title"] == RECORDS_TITLE and body["as_of"] == AS_OF_TEXT
    assert body["integrity"]["head"] == SEEDED_HEAD
    page = client.get("/aircraft/SYN-04/records.html", params={"as_of": AS_OF_TEXT})
    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
    assert page.text.startswith("<!doctype html>") and RECORDS_TITLE in page.text
    assert client.get("/aircraft/no-such/records").status_code == 404
    assert client.get("/aircraft/no-such/records.html").status_code == 404
    paths = client.get("/openapi.json").json()["paths"]
    assert "/aircraft/{key}/records" in paths and "/aircraft/{key}/records.html" in paths
    from uas_workbench.assistant.tools import TOOLS

    assert not any("records" in t.path for t in TOOLS)  # the assistant gains no tool


def test_the_command_writes_the_pack_and_refuses_an_unknown_aircraft(
    db: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "pack" / "records.html"
    main(["--db", db, "records", "SYN-04", "--out", str(out), "--json", "--as-of", AS_OF_TEXT])
    assert RECORDS_TITLE in capsys.readouterr().out
    assert out.read_text(encoding="utf-8").startswith("<!doctype html>")
    written = json.loads(out.with_suffix(".json").read_text(encoding="utf-8"))
    assert written["title"] == RECORDS_TITLE and written["as_of"] == AS_OF_TEXT
    assert written["integrity"]["head"] == SEEDED_HEAD
    with pytest.raises(SystemExit) as exit_:
        main(["--db", db, "records", "no-such", "--out", str(tmp_path / "x.html")])
    assert exit_.value.code == 1
    assert "refused (404)" in capsys.readouterr().err


def test_the_static_export_carries_one_sample_for_the_synthetic_aircraft(
    store: Store, tmp_path: Path
) -> None:
    out = export_static(store, tmp_path / "site")
    data = json.loads((out / "fleet.json").read_text(encoding="utf-8"))
    assert data["sample_records"] == {
        "aircraft_key": "SYN-04",
        "html": "records/SYN-04.html",
        "json": "records/SYN-04.json",
    }
    sample = json.loads((out / "records" / "SYN-04.json").read_text(encoding="utf-8"))
    assert sample["title"] == RECORDS_TITLE
    assert sample["as_of"] == data["as_of"] and sample["generated_utc"] == data["generated_utc"]
    assert sample["integrity"]["head"] == SEEDED_HEAD
    html = (out / "records" / "SYN-04.html").read_text(encoding="utf-8")
    assert html.startswith("<!doctype html>") and "Synthetic data" in html
    assert FORBIDDEN.search(html) is None
    assert not (out / "records" / f"{ALFA_KEY}.html").exists()  # no real aircraft sample
    page = (out / "index.html").read_text(encoding="utf-8")
    assert "sample_records" in page and "sample-records" in page
    # The OSO #03 sample is what it was: its pinned ledger hash holds.
    oso = json.loads((out / "evidence" / "SYN-04.json").read_text(encoding="utf-8"))
    assert oso["ledger_hash"] == SYN04_OSO_HASH
    assert "integrity" not in oso and "records" not in data["sample_evidence"]


def test_the_oso_pack_and_the_journal_head_are_unchanged(store: Store) -> None:
    p = build_pack(store, "SYN-04", CONFIG, as_of=AS_OF, commit="abc1234", generated_utc=NOW)
    assert p["ledger_hash"] == SYN04_OSO_HASH
    assert store.verify_journal().head == SEEDED_HEAD
