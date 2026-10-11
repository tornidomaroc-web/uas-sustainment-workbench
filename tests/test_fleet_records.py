"""The fleet records summary: one row per aircraft from the per-aircraft summary, one integrity
line for the whole store, fleet-level counts; every row, the projection behind it and the
integrity line read inside one snapshot that holds no write lock, and never a journal walk per
aircraft or per call.

Every assertion holds on Windows and Linux alike: no path, no line ending, no OS text."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from html import unescape
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from uas_workbench.evidence import (
    FLEET_RECORDS_SECTIONS,
    RECORDS_ITEMS,
    RECORDS_STATUSES,
    RECORDS_TITLE,
    SPAN_STATUSES,
    build_fleet_records,
    build_pack,
    build_records,
    render_fleet_records_html,
)
from uas_workbench.evidence import fleet_records as fleet_module
from uas_workbench.fleet import load_config
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
AT_WRITE = datetime(2026, 9, 20, 9, 0, tzinfo=UTC)
WAIT_S = 10.0
FORBIDDEN = re.compile(
    r"\b(compliant|complies|meets|approved|certified|ready for submission)\b", re.I
)
BROKEN = re.compile(r"\[object Object\]|\bundefined\b|\bNaN\b|\bNone\b")
SEEDED_HEAD = "c7726f26f1d1fddd45c679b1207d1eb0cebdf9c14b682f06c28d2d48d4384991"
SYN04_OSO_HASH = "3515dcb1fc7f1282c73fad1c6fbc9ebe39df61537c5629145a4b55d96f42eabf"
POINTS = ("training-records", "maintenance-activities", "operations-occurrences")
SEEDED_KEYS = (
    ALFA_KEY,
    PX4_KEY,
    "SYN-01",
    "SYN-02",
    "SYN-03",
    "SYN-04",
    "SYN-05",
    "SYN-06",
    "SYN-07",
)


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


def fleet(store: Store, as_of: datetime = AS_OF) -> dict[str, Any]:
    return build_fleet_records(store, CONFIG, as_of=as_of, commit="abc1234", generated_utc=NOW)


def every_text(summary: dict[str, Any]) -> list[str]:
    """Every form the summary is shown in: the data and the page."""
    return [json.dumps(summary, ensure_ascii=False), render_fleet_records_html(summary)]


def entry(subject: str, statement: str = "a record", at: datetime = AT_WRITE) -> Entry:
    return Entry(
        id=None,
        subject=subject,
        kind="work_order.open",
        occurred_utc=at,
        recorded_utc=at,
        entered_by="A. Tester, maintenance",
        statement=statement,
        details={"state": "deferred", "work_id": "WO-9"},
        supersedes=None,
        reason=None,
        synthetic=False,
    )


def correction_of(store: Store, key: str) -> Entry:
    """A correction of the aircraft's last inspection entry, which nothing supersedes and which
    supersedes nothing: one more entry held, one more superseded."""
    inspected = [e for e in store.entries(key) if e.kind == "inspection.done"][-1]
    assert inspected.id is not None and inspected.supersedes is None
    return Entry(
        id=None,
        subject=key,
        kind=inspected.kind,
        occurred_utc=inspected.occurred_utc,
        recorded_utc=AT_WRITE,
        entered_by="A. Tester, maintenance",
        statement="the hours at the inspection, read again",
        details={**inspected.details, "at_hours_s": float(inspected.details["at_hours_s"]) + 60.0},
        supersedes=inspected.id,
        reason="a typo in the hours",
        synthetic=False,
    )


def by_key(summary: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows = {row["key"]: row for row in summary["rows"]}
    assert len(rows) == len(summary["rows"]), "a key appears twice"
    return rows


def count_walks(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Every walk of the journal from now on."""
    seen: list[int] = []
    walk = journal.verify

    def counted(db: sqlite3.Connection, head: str | None = None) -> journal.Verification:
        seen.append(1)
        return walk(db, head)

    monkeypatch.setattr(journal, "verify", counted)
    return seen


# ---- the words ---------------------------------------------------------------------------


def test_the_title_is_the_same_draft_wording_as_the_per_aircraft_summary(store: Store) -> None:
    s = fleet(store)
    assert s["title"] == RECORDS_TITLE == "Draft records summary for the operator's own review"
    assert s["scope"] == "fleet"
    assert [sec["title"] for sec in s["sections"]] == list(FLEET_RECORDS_SECTIONS)
    assert s["sections"][0]["title"] == RECORDS_TITLE
    html = render_fleet_records_html(s)
    body = unescape(re.sub(r"<[^>]+>", " ", html.split("<body", 1)[1]))
    assert RECORDS_TITLE in body[:600]
    assert "does not say that any record is true" in body[:1500]
    assert "the authority decides" in body[:1500]
    assert unescape(html.split("</title>", 1)[0]).endswith(RECORDS_TITLE + ": the fleet")


def test_no_fleet_output_carries_a_forbidden_phrase(store: Store) -> None:
    for text in every_text(fleet(store)):
        found = FORBIDDEN.search(text)
        assert found is None, found.group(0)


def test_the_command_line_the_service_and_the_export_carry_no_forbidden_phrase(
    db: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "fleet-records.html"
    main(["--db", db, "fleet-records", "--out", str(out), "--json", "--as-of", AS_OF_TEXT])
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
    for path in ("/fleet/records", "/fleet/records.html"):
        r = client.get(path, params={"as_of": AS_OF_TEXT})
        assert r.status_code == 200, r.text[:200]
        texts.append(r.text)
    site = export_static(store, tmp_path / "site")
    texts.append((site / "records" / "fleet.html").read_text(encoding="utf-8"))
    texts.append((site / "records" / "fleet.json").read_text(encoding="utf-8"))
    for text in texts:
        found = FORBIDDEN.search(text)
        assert found is None, found.group(0)
    store.close()


def test_every_status_and_span_word_is_from_the_per_aircraft_vocabulary(store: Store) -> None:
    s = fleet(store)
    assert RECORDS_STATUSES == (
        "supported by these records",
        "partly",
        "not evidenced by this workbench",
        "not known",
    )
    assert SPAN_STATUSES == ("covered", "not yet 3 years", "not known")
    assert s["rows"], "no row"
    for row in s["rows"]:
        assert set(row["statuses"]) == set(POINTS)
        for point, status in row["statuses"].items():
            assert status in RECORDS_STATUSES, (row["key"], point, status)
            assert row["reasons"][point], (row["key"], point)
        assert row["span"] in SPAN_STATUSES, row["key"]
        assert row["span_reason"], row["key"]
    # The counts are keyed by exactly the vocabulary, every word present, nothing else.
    for point in POINTS:
        assert tuple(s["counts"]["by_point"][point]) == RECORDS_STATUSES, point
    assert tuple(s["counts"]["span"]) == SPAN_STATUSES
    assert [item["id"] for item in s["items"]] == [item["id"] for item in RECORDS_ITEMS]
    html = render_fleet_records_html(s)
    assert BROKEN.search(re.sub(r"<[^>]+>", " ", html.split("<body", 1)[1])) is None
    shown = re.findall(r'class="status [^"]*">([^<]+)<', html)
    assert shown and set(shown) <= set(RECORDS_STATUSES), shown
    spans = re.findall(r'class="span [^"]*">([^<]+)<', html)
    assert spans and set(spans) <= set(SPAN_STATUSES), spans


# ---- the rows are the per-aircraft summary, not a second implementation -------------------


def test_every_row_equals_the_per_aircraft_summary_of_that_aircraft(store: Store) -> None:
    s = fleet(store)
    rows = by_key(s)
    assert tuple(rows) == SEEDED_KEYS  # the store's order, every seeded aircraft once
    for key in SEEDED_KEYS:
        one = build_records(store, key, CONFIG, as_of=AS_OF, commit="abc1234", generated_utc=NOW)
        row = rows[key]
        for item in one["items"]:
            assert row["statuses"][item["id"]] == item["status"], (key, item["id"])
            assert row["reasons"][item["id"]] == item["reason"], (key, item["id"])
        assert row["span"] == one["retention"]["status"], key
        assert row["span_reason"] == one["retention"]["reason"], key
        assert row["earliest_utc"] == one["retention"]["earliest_utc"], key
        assert row["entries"] == one["maintenance"]["entries"], key
        assert row["flights"] == one["operations"]["flights"], key
        assert row["label"] == one["aircraft"]["label"] and row["data"] == one["label"], key
        assert row["synthetic"] is one["aircraft"]["synthetic"], key
        assert s["integrity"] == one["integrity"], key
        assert s["as_of"] == one["as_of"] and s["commit"] == one["commit"], key


def test_the_fleet_counts_add_up_to_the_rows(store: Store) -> None:
    s = fleet(store)
    rows = s["rows"]
    c = s["counts"]
    assert c["aircraft"] == len(rows) == 9
    assert c["synthetic"] == sum(r["synthetic"] for r in rows) == 7
    assert c["real"] == sum(not r["synthetic"] for r in rows) == 2
    for point in POINTS:
        assert sum(c["by_point"][point].values()) == len(rows), point
        for status in RECORDS_STATUSES:
            assert c["by_point"][point][status] == sum(
                r["statuses"][point] == status for r in rows
            ), (point, status)
    for status in SPAN_STATUSES:
        assert c["span"][status] == sum(r["span"] == status for r in rows), status
    assert c["rows_not_known"] == sum(
        "not known" in r["statuses"].values() or r["span"] == "not known" for r in rows
    )
    # The demo: training never; maintenance on the six synthetic aircraft with an entry by the
    # computation date (SYN-06 has none, nor has a showcase aircraft); operations partly on
    # every aircraft, each having a log; the span covered on the one aircraft whose logs are
    # 8 years old, not yet 3 years on the rest; nothing not known.
    assert c["by_point"]["training-records"]["not evidenced by this workbench"] == 9
    assert c["by_point"]["maintenance-activities"]["supported by these records"] == 6
    assert c["by_point"]["maintenance-activities"]["not evidenced by this workbench"] == 3
    assert c["by_point"]["operations-occurrences"]["partly"] == 9
    assert c["span"] == {"covered": 1, "not yet 3 years": 8, "not known": 0}
    assert by_key(s)[ALFA_KEY]["span"] == "covered" and by_key(s)["SYN-06"]["entries"] == 0
    assert c["rows_not_known"] == 0
    assert s["readable"]["ok"] is True and s["readable"]["unreadable"] is None


# ---- integrity: one line for the whole store ----------------------------------------------


def test_one_integrity_line_for_the_whole_store_says_unchanged_when_the_journal_verifies(
    store: Store,
) -> None:
    s = fleet(store)
    integrity = s["integrity"]
    assert integrity["ok"] is True and integrity["head"] == SEEDED_HEAD
    assert integrity["length"] == store.entry_count() + store.flight_count()
    assert integrity["line"].startswith(f"unchanged since head {SEEDED_HEAD}")
    assert "journal broken" not in integrity["line"]
    html = render_fleet_records_html(s)
    assert html.count("unchanged since head") == 1


def test_a_broken_journal_is_one_break_line_never_unchanged(db: str) -> None:
    with raw(db) as c:
        c.execute("UPDATE journal SET link_hash = ? WHERE seq = 3", ("0" * 64,))
    store = Store(db)
    s = fleet(store)
    integrity = s["integrity"]
    assert integrity["ok"] is False and integrity["broken"]["seq"] == 3
    assert integrity["line"].startswith("journal broken at link 3")
    for text in every_text(s):
        assert "unchanged since head" not in text
        assert "journal broken at link 3" in text
        assert FORBIDDEN.search(text) is None
    # Every row still comes from the per-aircraft summary: the break is the store's, not a row's.
    assert len(s["rows"]) == 9 and s["counts"]["rows_not_known"] == 0
    store.close()


# ---- what cannot be read ----------------------------------------------------------------


def test_an_unreadable_entry_leaves_the_ledger_points_not_known_on_every_row(db: str) -> None:
    """The fleet rule, as the per-aircraft summary and the fleet liveness rule have it: liveness
    is decided over the whole ledger, so one entry the store cannot read leaves maintenance
    activities and the span not known on every row, the record named; the flights still read,
    so operations stay what the logs show. The summary is still written, with the record named
    at fleet level, never a 503: it reports what the records show, and that is the report."""
    with raw(db) as c:
        c.execute("UPDATE entries SET record = ? WHERE id = 4", ("[" * 100_000 + "]" * 100_000,))
    store = Store(db)
    s = fleet(store)
    rows = by_key(s)
    assert len(rows) == 9
    for key, row in rows.items():
        assert row["statuses"]["maintenance-activities"] == "not known", key
        assert "entry 4" in row["reasons"]["maintenance-activities"], key
        assert row["span"] == "not known" and "entry 4" in row["span_reason"], key
        assert row["statuses"]["operations-occurrences"] == "partly", key
        assert isinstance(row["entries"], dict) and "unknown" in row["entries"], key
    c = s["counts"]
    assert c["by_point"]["maintenance-activities"]["not known"] == 9
    assert c["span"]["not known"] == 9 and c["rows_not_known"] == 9
    assert s["readable"]["ok"] is False and "entry 4" in s["readable"]["unreadable"]
    assert "entry 4" in s["readable"]["line"]
    assert s["integrity"]["ok"] is False and s["integrity"]["broken"]["kind"] == "content"
    for text in every_text(s):
        assert "entry 4" in text and BROKEN.search(re.sub(r"<[^>]+>", " ", text)) is None
        assert "unchanged since head" not in text and FORBIDDEN.search(text) is None
    client = TestClient(create_app(store, write_token="t"))
    r = client.get("/fleet/records", params={"as_of": AS_OF_TEXT})
    assert r.status_code == 200, r.text[:200]
    assert r.json()["counts"]["rows_not_known"] == 9
    assert client.get("/aircraft").status_code == 503  # the board keeps its liveness rule
    store.close()


def test_an_unreadable_flight_leaves_only_its_aircraft_not_known(db: str) -> None:
    with raw(db) as c:
        c.execute("UPDATE flights SET record = 'not json' WHERE id = 2")
    store = Store(db)
    s = fleet(store)
    rows = by_key(s)
    hit = rows["SYN-01"]
    assert hit["statuses"]["operations-occurrences"] == "not known"
    assert "flight 2" in hit["reasons"]["operations-occurrences"]
    assert hit["span"] == "not known" and "flight 2" in hit["span_reason"]
    assert hit["statuses"]["maintenance-activities"] == "supported by these records"
    for key, row in rows.items():
        if key == "SYN-01":
            continue
        assert "not known" not in row["statuses"].values(), key
        assert row["span"] != "not known", key
    c = s["counts"]
    assert c["by_point"]["operations-occurrences"]["not known"] == 1
    assert c["by_point"]["operations-occurrences"]["partly"] == 8
    assert c["span"]["not known"] == 1 and c["rows_not_known"] == 1
    assert s["readable"]["ok"] is False and "flight 2" in s["readable"]["unreadable"]
    for text in every_text(s):
        assert "flight 2" in text and FORBIDDEN.search(text) is None
    store.close()


# ---- objection 1: the cost -------------------------------------------------------------------


def test_building_the_fleet_summary_walks_the_journal_zero_times_for_an_unauthenticated_caller(
    db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nine aircraft, ten requests of the data and ten of the page, no token: zero walks. The
    integrity line is the result the store already holds."""
    store = Store(db)
    app = create_app(store, write_token="t")
    client = TestClient(app, client=("172.17.0.1", 40000))  # not loopback, no token
    walks = count_walks(monkeypatch)
    heads = set()
    for _ in range(10):
        body = client.get("/fleet/records").json()
        heads.add(body["integrity"]["head"])
        assert len(body["rows"]) == 9
        assert client.get("/fleet/records.html").status_code == 200
    assert heads == {SEEDED_HEAD}
    assert sum(walks) == 0, f"{sum(walks)} walks for 20 unauthenticated fleet requests"
    assert client.get("/journal/verify").status_code == 401  # the guarded route stays guarded
    store.close()


def test_an_outside_commit_costs_the_next_fleet_build_exactly_one_walk_not_one_per_aircraft(
    db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Store(db)
    store.journal_state()  # walked once, as the service does at startup
    other = Store(db)
    other.append_entry(entry("SYN-02", "committed by another connection"))
    other.close()
    walks = count_walks(monkeypatch)
    first = fleet(store)
    assert sum(walks) == 1, f"{sum(walks)} walks for one fleet build after one outside commit"
    assert first["integrity"]["ok"] and first["integrity"]["length"] == store.entry_count() + (
        store.flight_count()
    )
    assert first["integrity"]["head"] != SEEDED_HEAD
    second = fleet(store)
    assert sum(walks) == 1 and second["integrity"] == first["integrity"]
    store.close()


# ---- objection 2: one consistent read, and no write lock -------------------------------------


def test_a_correction_landing_between_two_rows_never_gives_a_mixed_summary(
    db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A correction of SYN-01's first entry is committed by another connection after the first
    row is built. The summary must equal the one built before the correction or the one built
    after it: never SYN-01's row from one state with the integrity line, the projection behind
    the later rows or the counts from the other."""
    store = Store(db)
    store.journal_state()
    before = fleet(store)
    fired: list[bool] = []
    build = build_records  # the one `fleet_records` calls

    def land_then_build(*args: Any, **kwargs: Any) -> dict[str, Any]:
        if not fired:
            fired.append(True)
            other = Store(db)
            other.append_entry(correction_of(store, "SYN-01"))
            other.close()
        return build(*args, **kwargs)

    monkeypatch.setattr(fleet_module, "build_records", land_then_build)
    during = fleet(store)
    monkeypatch.undo()
    after = fleet(store)
    assert fired
    assert after["integrity"]["length"] == before["integrity"]["length"] + 1
    assert by_key(after)["SYN-01"]["entries"] == by_key(before)["SYN-01"]["entries"] + 1
    assert during in (before, after), (
        "a state the store never held: SYN-01 row with "
        f"{by_key(during)['SYN-01']['entries']} entries and the integrity line at "
        f"{during['integrity']['length']} links, where the store held "
        f"({by_key(before)['SYN-01']['entries']}, {before['integrity']['length']}) before the "
        f"correction and ({by_key(after)['SYN-01']['entries']}, {after['integrity']['length']}) "
        "after it"
    )
    store.close()


def test_a_write_landing_mid_build_reaches_no_row_and_not_the_integrity_line(
    db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two writes are injected after the first row is built: one committed by another
    connection, which lands in the file at once, and one through the store itself from another
    thread, which lands at once too, since the build holds no write lock. Neither reaches the
    summary under construction: every row and the integrity line describe the state the build
    began on. The next build has both."""
    store = Store(db)
    before = fleet(store)
    assert before["integrity"]["head"] == SEEDED_HEAD
    fired: list[bool] = []
    through_store = threading.Thread(
        None, lambda: store.append_entry(entry("SYN-02", "through the store, mid-build"))
    )
    build = build_records  # the one `fleet_records` calls

    def write_then_build(*args: Any, **kwargs: Any) -> dict[str, Any]:
        if not fired:
            fired.append(True)
            other = Store(db)
            other.append_entry(entry("SYN-02", "by another connection, mid-build"))
            other.close()
            through_store.start()
            through_store.join(WAIT_S)
        return build(*args, **kwargs)

    monkeypatch.setattr(fleet_module, "build_records", write_then_build)
    during = fleet(store)
    monkeypatch.undo()
    assert fired and not through_store.is_alive(), "the write through the store waited"
    assert during == before, "a write that landed mid-build reached the summary"
    after = fleet(store)
    assert (
        after["integrity"]["ok"]
        and after["integrity"]["length"] == before["integrity"]["length"] + 2
    )
    assert after["integrity"]["head"] != SEEDED_HEAD
    assert by_key(after)["SYN-02"]["entries"] == by_key(before)["SYN-02"]["entries"] + 2
    assert by_key(after)["SYN-02"]["statuses"] == by_key(before)["SYN-02"]["statuses"]
    store.close()


def test_a_write_issued_during_a_build_does_not_wait_for_it_and_is_in_the_next_one(
    db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The build is held open at its first read of the aircraft; a write through the store
    issued meanwhile finishes while the build is still held, so the build held no write lock;
    the summary is the state the build began on, and the next one has the write."""
    store = Store(db)
    started, release = threading.Event(), threading.Event()
    aircraft = store.aircraft

    def held() -> Any:
        started.set()
        release.wait(WAIT_S)
        return aircraft()

    monkeypatch.setattr(store, "aircraft", held)
    results: list[dict[str, Any]] = []
    building = threading.Thread(None, lambda: results.append(fleet(store)))
    writing = threading.Thread(None, lambda: store.append_entry(entry("SYN-03", "lands")))
    building.start()
    assert started.wait(WAIT_S)
    writing.start()
    writing.join(WAIT_S)
    assert not writing.is_alive(), "the write waited for the build"
    assert building.is_alive(), "the build was not held open"
    release.set()
    building.join(WAIT_S)
    assert not building.is_alive() and results
    assert results[0]["integrity"]["head"] == SEEDED_HEAD
    assert by_key(results[0])["SYN-03"]["entries"] == by_key(fleet(store))["SYN-03"]["entries"] - 1
    store.close()


# ---- the surfaces: service, command line, static export ----------------------------------


def test_the_routes_answer_json_and_html(store: Store) -> None:
    client = TestClient(create_app(store, write_token="t"))
    r = client.get("/fleet/records", params={"as_of": AS_OF_TEXT})
    assert r.status_code == 200
    body = r.json()
    assert body["title"] == RECORDS_TITLE and body["as_of"] == AS_OF_TEXT
    assert body["integrity"]["head"] == SEEDED_HEAD and len(body["rows"]) == 9
    page = client.get("/fleet/records.html", params={"as_of": AS_OF_TEXT})
    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
    assert page.text.startswith("<!doctype html>") and RECORDS_TITLE in unescape(page.text)
    paths = client.get("/openapi.json").json()["paths"]
    assert "/fleet/records" in paths and "/fleet/records.html" in paths
    from uas_workbench.assistant.tools import TOOLS

    assert not any("records" in t.path for t in TOOLS)  # the assistant gains no tool


def test_the_command_writes_the_summary(
    db: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "pack" / "fleet-records.html"
    main(["--db", db, "fleet-records", "--out", str(out), "--json", "--as-of", AS_OF_TEXT])
    printed = capsys.readouterr().out
    assert RECORDS_TITLE in printed and "9 aircraft" in printed
    assert out.read_text(encoding="utf-8").startswith("<!doctype html>")
    written = json.loads(out.with_suffix(".json").read_text(encoding="utf-8"))
    assert written["title"] == RECORDS_TITLE and written["as_of"] == AS_OF_TEXT
    assert written["integrity"]["head"] == SEEDED_HEAD and len(written["rows"]) == 9


def test_the_static_export_carries_the_fleet_summary_beside_the_sample(
    store: Store, tmp_path: Path
) -> None:
    out = export_static(store, tmp_path / "site")
    data = json.loads((out / "fleet.json").read_text(encoding="utf-8"))
    assert data["fleet_records"] == {"html": "records/fleet.html", "json": "records/fleet.json"}
    assert data["sample_records"]["aircraft_key"] == "SYN-04"  # the per-aircraft sample stays
    summary = json.loads((out / "records" / "fleet.json").read_text(encoding="utf-8"))
    assert summary["title"] == RECORDS_TITLE and len(summary["rows"]) == 9
    assert summary["as_of"] == data["as_of"] and summary["generated_utc"] == data["generated_utc"]
    assert summary["integrity"]["head"] == SEEDED_HEAD
    html = (out / "records" / "fleet.html").read_text(encoding="utf-8")
    assert html.startswith("<!doctype html>") and FORBIDDEN.search(html) is None
    page = (out / "index.html").read_text(encoding="utf-8")
    assert "fleet_records" in page and "fleet-records" in page
    # The OSO #03 sample and the per-aircraft sample are what they were.
    oso = json.loads((out / "evidence" / "SYN-04.json").read_text(encoding="utf-8"))
    assert oso["ledger_hash"] == SYN04_OSO_HASH
    one = json.loads((out / "records" / "SYN-04.json").read_text(encoding="utf-8"))
    assert one["integrity"]["head"] == SEEDED_HEAD and "rows" not in one


def test_the_oso_pack_and_the_journal_head_are_unchanged(store: Store) -> None:
    p = build_pack(store, "SYN-04", CONFIG, as_of=AS_OF, commit="abc1234", generated_utc=NOW)
    assert p["ledger_hash"] == SYN04_OSO_HASH
    assert store.verify_journal().head == SEEDED_HEAD
