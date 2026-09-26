"""The draft evidence pack for OSO #03: honest in its first lines, traceable, never current
about a superseded entry, and holding only what the records support."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from uas_workbench.evidence import OSO_ITEMS, SOURCES, build_pack, ledger_hash, render_html
from uas_workbench.fleet import load_config
from uas_workbench.fleet.showcase import ALFA_KEY, PX4_KEY, showcase
from uas_workbench.fleet.synthetic import generate
from uas_workbench.ledger import Entry, append
from uas_workbench.service.store import Store

FIXTURES = Path(__file__).parent / "fixtures"
CONFIG = load_config()
AS_OF = datetime(2026, 10, 1, tzinfo=UTC)
NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
STATUSES = {"supported", "partly", "not evidenced"}
COMPLIANT = re.compile(r"\bcompliant\b", re.I)
BROKEN = re.compile(r"\[object Object\]|\bundefined\b|\bNaN\b|\bNone\b")


@pytest.fixture(scope="module")
def store() -> Store:
    s = Store(":memory:")
    s.add_fleet(generate(CONFIG))
    s.add_fleet(showcase(FIXTURES))
    return s


def pack(store: Store, key: str, commit: str | None = "abc1234") -> dict[str, Any]:
    return build_pack(store, key, CONFIG, as_of=AS_OF, commit=commit, generated_utc=NOW)


def test_first_lines_are_the_honest_statement_and_the_labels(store: Store) -> None:
    p = pack(store, "SYN-04")
    assert p["title"] == "Draft evidence pack for OSO #03: SYN-04"
    statement = " ".join(p["statement"])
    for phrase in (
        "draft",
        "does not show compliance",
        "claims no robustness level",
        "does not certify airworthiness or return to service",
        "the authority decides",
    ):
        assert phrase in statement, phrase
    assert p["aircraft"]["synthetic"] is True
    assert p["label"] == "Synthetic data: this aircraft, its flights and its records are generated"
    assert p["generated_utc"] == "2026-10-02T12:00:00Z" and p["as_of"] == "2026-10-01T00:00:00Z"
    assert p["commit"] == "abc1234" and p["version"]
    assert COMPLIANT.search(json.dumps(p)) is None  # never "compliant"; a source title may say
    # "Means of Compliance", which is the document's name


def test_sources_are_named_with_edition_and_pages_and_paraphrased(store: Store) -> None:
    ids = {s["id"] for s in SOURCES}
    assert ids == {"annex-e-2.5", "annex-e-1.0", "easa-moc-oso3"}
    for s in SOURCES:
        assert s["title"] and s["edition"] and s["date"] and s["pages"] and s["url"]
    p = pack(store, "SYN-04")
    assert p["sources"] == list(SOURCES)
    assert "2019/947" in p["version_note"] and "2.5" in p["version_note"]
    # No item text is longer than a paraphrase: the JARUS text may be used, not copied.
    for item in OSO_ITEMS:
        assert len(item["text"]) < 260, item["id"]
        assert item["source"] in ids and item["page"]


def test_items_cover_oso_03_and_say_what_each_section_supports(store: Store) -> None:
    p = pack(store, "SYN-04")
    by_id = {i["id"]: i for i in p["items"]}
    assert set(by_id) == {
        "integrity-low",
        "integrity-medium",
        "integrity-high",
        "assurance-1-low",
        "assurance-1-medium",
        "assurance-1-high",
        "assurance-2-low",
        "assurance-2-medium",
        "assurance-2-high",
        "ica-airworthiness-limitations",
    }
    for item in p["items"]:
        assert item["status"] in STATUSES, item
        assert item["text"] and item["reason"]
        if item["status"] == "not evidenced":
            assert item["sections"] == []
        else:
            assert item["sections"], item["id"]
    assert by_id["assurance-1-low"]["status"] == "partly"  # the log yes; instructions and staff no
    assert by_id["integrity-medium"]["status"] == "partly"  # schedule yes; releases and staff no
    assert "release" in by_id["integrity-medium"]["reason"]
    assert by_id["ica-airworthiness-limitations"]["status"] == "supported"
    for item_id in ("integrity-high", "assurance-1-high", "assurance-2-low", "assurance-2-high"):
        assert by_id[item_id]["status"] == "not evidenced", item_id
    assert [i["id"] for i in p["not_evidenced"]] == [
        i["id"] for i in p["items"] if i["status"] == "not evidenced"
    ]
    titles = [s["title"] for s in p["sections"]]
    assert len(titles) == 10 and titles[0].startswith("Draft evidence pack")


def test_usage_programme_components_and_log_come_from_the_records(store: Store) -> None:
    p = pack(store, "SYN-04")
    usage = p["usage"]
    assert usage["time_in_service_s"] > 0 and usage["flights"] and usage["findings"]
    assert usage["unlogged_s"] > 0  # the reconcile finding on SYN-04 counts toward usage
    programme = p["programme"]
    assert programme["status"] == "unserviceable" and programme["status_reasons"]
    assert any(i["state"] == "overdue" for i in programme["items"])
    assert "editable defaults" in programme["source_note"]
    comps = {c["id"]: c for c in p["components"]}
    assert "BAT-04A" in comps and comps["BAT-04A"]["usage"]["cycles"] > 300
    assert [i["aircraft_key"] for i in comps["BAT-04A"]["installations"]] == ["SYN-01", "SYN-04"]
    assert all(e["subject"] == "BAT-04A" for e in comps["BAT-04A"]["entries"])
    log = p["log"]
    assert log and all(e["current"] in (True, False) for e in log)
    stamps = [e["occurred_utc"] for e in log]
    assert stamps == sorted(stamps)
    assert all(e["synthetic"] is True for e in log)


def test_superseded_entries_are_history_with_their_successor(store: Store) -> None:
    p = pack(store, "SYN-01")
    dead = [e for e in p["log"] if not e["current"]]
    assert len(dead) == 1
    (old,) = dead
    assert old["superseded_by"] is not None
    successor = next(e for e in p["log"] if e["id"] == old["superseded_by"])
    assert old["superseded_reason"] == successor["reason"]
    assert "misread" in old["superseded_reason"]
    assert all(e["superseded_by"] is None for e in p["log"] if e["current"])
    html = render_html(p)
    assert "superseded by #" in html
    row = html[html.index(f"#{old['id']}") :]
    assert "superseded by #" in row[:800]


def test_real_aircraft_hold_only_what_their_logs_support(store: Store) -> None:
    for key in (ALFA_KEY, PX4_KEY):
        p = pack(store, key)
        assert p["aircraft"]["synthetic"] is False
        assert p["label"].startswith("Real data")
        assert p["usage"]["flights"]
        assert p["programme"]["status"] == {
            "unknown": "no maintenance record entered for this aircraft"
        }
        assert p["programme"]["items"] == [] and p["components"] == [] and p["log"] == []
        assert "the ledger holds nothing for this aircraft" in p["ledger_note"]
        assert p["usage"]["time_in_service_s"] == {
            "unknown": "no maintenance record entered for this aircraft"
        }
    alfa = pack(store, ALFA_KEY)
    assert any("355 s of flight" in f["message"] for f in alfa["usage"]["findings"])


def test_traceability_hash_covers_every_entry_read_and_changes_when_one_is_added(
    store: Store,
) -> None:
    first = pack(store, "SYN-02")
    again = pack(store, "SYN-02")
    assert first["ledger_hash"] == again["ledger_hash"]
    entries = [e for e in first["log"]]
    canonical = json.dumps(
        [
            {k: v for k, v in e.items() if k not in ("current", "superseded_reason")}
            for e in entries
        ],
        sort_keys=True,
        separators=(",", ":"),
    )
    assert first["ledger_hash"] == hashlib.sha256(canonical.encode()).hexdigest()
    assert first["ledger_hash"] == ledger_hash(entries)
    assert first["counts"] == {
        "entries": len(entries),
        "flights": len(first["usage"]["flights"]),
        "due_items": len(first["programme"]["items"]),
    }
    other = Store(":memory:")
    other.add_fleet(generate(CONFIG))
    append(
        other,
        Entry(
            id=None,
            subject="SYN-02",
            kind="work_order.open",
            occurred_utc=datetime(2026, 9, 1, tzinfo=UTC),
            recorded_utc=NOW,
            entered_by="A. Tester, maintenance",
            statement="a new entry changes the hash",
            details={"state": "deferred"},
            supersedes=None,
            reason=None,
            synthetic=False,
        ),
        policy=CONFIG.life,
        now=NOW,
    )
    changed = build_pack(other, "SYN-02", CONFIG, as_of=AS_OF, commit=None, generated_utc=NOW)
    assert changed["ledger_hash"] != first["ledger_hash"]
    assert changed["commit"] == "unknown"
    assert changed["label"].startswith("Mixed data")


def test_html_is_self_contained_honest_first_and_labelled_on_every_printed_page(
    store: Store,
) -> None:
    p = pack(store, "SYN-04")
    html = render_html(p)
    assert html.startswith("<!doctype html>")
    assert "<script" not in html.lower() and "http" not in html.split("<body")[1].split("<h1")[0]
    body = html.split("<body", 1)[1]
    head_text = re.sub(r"<[^>]+>", " ", body[:2500])
    for phrase in ("Draft evidence pack", "does not show compliance", "the authority decides"):
        assert phrase in head_text, phrase
    assert 'class="print-header"' in html and "position: fixed" in html
    assert html.count("Synthetic data") >= 2  # the fixed print header and the cover
    assert COMPLIANT.search(html) is None
    assert BROKEN.search(re.sub(r"<[^>]+>", " ", body)) is None
    for title in (
        "What this pack holds, item by item",
        "Time in service and usage",
        "Maintenance programme status",
        "Life-limited and tracked components",
        "Maintenance log",
        "Not evidenced by this workbench",
        "Traceability",
    ):
        assert title in html, title
    assert "https://" in html  # the sources are cited with their URLs
    real = render_html(pack(store, ALFA_KEY))
    assert "Real data" in real and "the ledger holds nothing for this aircraft" in real
