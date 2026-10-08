"""A stored record the store cannot read (issue #43) never crashes the service at startup, a
read endpoint, the command line or the evidence pack; and it is never skipped either. It is
named to the operator (which table, which row, which aircraft or component, and why it could
not be read), every read that depends on it says so instead of answering with a number
computed without it, and `uasw verify` keeps reporting it as a content break at its link.

Every craft below is done on the SQLite file with a plain connection, never through the store:
an entry nested 100,000 deep (json.loads raises RecursionError), a flight record that is not
JSON, an entry whose details hold a word where the projection reads a number, and an entry
whose time is a number where the codec reads an ISO string.
"""

from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from uas_workbench.evidence import build_pack, render_html
from uas_workbench.fleet import load_config
from uas_workbench.service.app import create_app
from uas_workbench.service.cli import main
from uas_workbench.service.store import Store

FIXTURES = Path(__file__).parent / "fixtures"
CONFIG = load_config()
AUTH = {"Authorization": "Bearer t"}
AS_OF = "2026-10-01T00:00:00Z"
BROKEN = re.compile(r"\[object Object\]|\bundefined\b|\bNaN\b|\bNone\b")
COMPLIANT = re.compile(r"\bcompliant\b", re.I)
# The reads of the service that depend on the whole ledger (liveness is decided over every
# entry, and a component move names another aircraft, so one unreadable entry leaves no
# projected record known), and the ones that depend on the flight records of SYN-01.
LEDGER_READS = (
    "/aircraft",
    "/aircraft/SYN-01",
    "/aircraft/SYN-01/due",
    "/aircraft/SYN-01/grounded-flights",
    "/fleet/due",
    "/fleet/grounded-flights",
    "/entries",
    "/entries?aircraft=SYN-01",
    "/entries/1",
    "/components",
    "/components/BAT-01A",
)
FLIGHT_READS = (
    "/aircraft",
    "/aircraft/SYN-01",
    "/aircraft/SYN-01/flights",
    "/aircraft/SYN-01/reconcile",
    "/aircraft/SYN-01/due",
    "/aircraft/SYN-01/grounded-flights",
    "/fleet/findings",
    "/fleet/due",
    "/fleet/grounded-flights",
)
# An inspection recorded without its hours derives them from the flights of its aircraft, and
# is judged against the projection: it reads both, so a crafted store refuses it either way.
INSPECTION = {
    "subject": "SYN-01",
    "kind": "inspection.done",
    "occurred_utc": "2026-09-20T09:00:00Z",
    "entered_by": "A. Tester, maintenance",
    "statement": "nothing is written on a store that cannot be read",
    "details": {"name": "100-hour inspection"},
}


@pytest.fixture
def db(tmp_path: Path) -> str:
    path = str(tmp_path / "fleet.sqlite")
    main(["--db", path, "seed", "--fixtures", str(FIXTURES)])
    return path


def raw(path: str) -> sqlite3.Connection:
    """What anyone with the file has: a plain connection, no store, no rules."""
    return sqlite3.connect(path)


# ---- the crafts: each returns the words the operator must be told ------------------------


def craft_nested_entry(path: str) -> tuple[str, ...]:
    """Entry 4 (time_in_service.set on SYN-01) nested 100,000 deep: issue #43 as filed."""
    with raw(path) as c:
        c.execute("UPDATE entries SET record = ? WHERE id = 4", ("[" * 100_000 + "]" * 100_000,))
    return ("entry 4", "SYN-01", "nested too deep")


def craft_not_json_flight(path: str) -> tuple[str, ...]:
    """Flight 2 of SYN-01 replaced by text that is not JSON."""
    with raw(path) as c:
        c.execute("UPDATE flights SET record = 'not json' WHERE id = 2")
    return ("flight 2", "SYN-01", "not JSON")


def craft_wrong_type_in_details(path: str) -> tuple[str, ...]:
    """Entry 5 (the live time_in_service.set on SYN-01; it supersedes entry 4) keeps its
    shape, but the hours the projection reads as a number are a word: the codec reads it,
    project() cannot."""
    with raw(path) as c:
        (text,) = c.execute("SELECT record FROM entries WHERE id = 5").fetchone()
        data = json.loads(text)
        assert data["kind"] == "time_in_service.set" and data["supersedes"] == 4
        data["details"]["before_s"] = "many"
        c.execute("UPDATE entries SET record = ? WHERE id = 5", (json.dumps(data),))
    return ("entry 5", "SYN-01", "before_s")


def craft_wrong_type_in_a_field(path: str) -> tuple[str, ...]:
    """Entry 4's time is a number where the codec reads an ISO string."""
    with raw(path) as c:
        (text,) = c.execute("SELECT record FROM entries WHERE id = 4").fetchone()
        data = json.loads(text)
        data["occurred_utc"] = 20260419
        c.execute("UPDATE entries SET record = ? WHERE id = 4", (json.dumps(data),))
    return ("entry 4", "SYN-01", "occurred_utc")


CRAFTS = [
    craft_nested_entry,
    craft_not_json_flight,
    craft_wrong_type_in_details,
    craft_wrong_type_in_a_field,
]
IDS = [c.__name__.removeprefix("craft_") for c in CRAFTS]
Craft = Callable[[str], tuple[str, ...]]


def reads_of(words: tuple[str, ...]) -> tuple[str, ...]:
    return LEDGER_READS if words[0].startswith("entry") else FLIGHT_READS


def named(text: str, words: tuple[str, ...]) -> None:
    """The operator is told the row, the aircraft and why; what to run next is named too."""
    for word in words:
        assert word in text, (word, text)
    assert "cannot be read" in text, text
    assert "uasw verify" in text, text


# ---- at startup -----------------------------------------------------------------------


@pytest.mark.parametrize("craft", CRAFTS, ids=IDS)
def test_the_service_starts_on_a_crafted_store_and_names_the_record(
    db: str, craft: Craft, caplog: pytest.LogCaptureFixture
) -> None:
    """create_app refreshes the gauges, which projects every entry: on main this is the
    RecursionError of issue #43 at startup. The service must start, say what it could not
    read, and give no gauge a number computed without the record."""
    words = craft(db)
    store = Store(db)
    with caplog.at_level(logging.WARNING, logger="uasw"):
        app = create_app(store, write_token="t")
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "the unreadable record is named at startup"
    said = " ".join(f"{r.getMessage()} {r.__dict__.get('detail', '')}" for r in warnings)
    named(said, words)
    client = TestClient(create_app(store, write_token="t"))
    # /health answers, says the store is not fully readable, and names the record.
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["status"] != "ok"
    named(health.json()["unreadable"], words)
    # The gauges: a board state that cannot be computed is counted as unknown, and a count
    # that cannot be computed is NaN, never 0.
    metrics = client.get("/metrics").text
    unknown = re.search(r'uasw_aircraft_by_status\{status="unknown"\} (\S+)', metrics)
    assert unknown is not None and float(unknown[1]) >= 1
    due_items = re.search(r'uasw_due_items\{state="overdue"\} (\S+)', metrics)
    assert due_items is not None and math.isnan(float(due_items[1]))
    assert client.get("/journal/verify", headers=AUTH).json()["broken"]["kind"] == "content"
    _ = app
    store.close()


# ---- under a running service, the file edited after start -------------------------------


@pytest.mark.parametrize("craft", CRAFTS, ids=IDS)
def test_reads_under_a_running_service_say_what_cannot_be_read_never_500(
    db: str, craft: Craft
) -> None:
    store = Store(db)
    client = TestClient(create_app(store, write_token="t"))
    for path in (*LEDGER_READS, *FLIGHT_READS):
        assert client.get(path).status_code == 200, path
    assert client.get("/health").json()["status"] == "ok"
    words = craft(db)
    for path in reads_of(words):
        r = client.get(path)
        assert r.status_code == 503, (path, r.status_code, r.text[:200])
        named(r.json()["detail"], words)
        assert "Traceback" not in r.text
    for path in (f"/aircraft/SYN-01/due?as_of={AS_OF}", f"/aircraft?as_of={AS_OF}&cues=true"):
        assert client.get(path).status_code == 503, path
    health = client.get("/health")
    assert health.status_code == 200 and health.json()["status"] != "ok"
    named(health.json()["unreadable"], words)
    # A write is judged against the projection, which cannot be read: refused, nothing written.
    before = client.get("/journal/verify", headers=AUTH).json()["length"]
    r = client.post("/entries", json=INSPECTION, headers=AUTH)
    assert r.status_code == 503, r.text
    named(r.json()["detail"], words)
    assert client.get("/journal/verify", headers=AUTH).json()["length"] == before
    # The reads that need no record still answer: a flight list of another aircraft, the
    # reconciliation of another aircraft, and verify, which reports the break at its link.
    assert client.get("/aircraft/SYN-02/flights").status_code == 200
    assert client.get("/aircraft/SYN-02/reconcile").status_code == 200
    verify = client.get("/journal/verify", headers=AUTH).json()
    assert verify["ok"] is False and verify["broken"]["kind"] == "content"
    assert client.get("/metrics").status_code == 200
    store.close()


# ---- the command line ------------------------------------------------------------------


@pytest.mark.parametrize("craft", CRAFTS, ids=IDS)
def test_the_command_line_refuses_with_the_record_named(
    db: str, craft: Craft, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    words = craft(db)
    commands = [
        [
            "record",
            "inspection",
            "SYN-01",
            "100-hour inspection",
            "--by",
            "A. Tester, maintenance",
            "--statement",
            "nothing is written",
            "--at",
            "2026-09-20T09:00:00Z",
        ],
        ["export-static", "--out", str(tmp_path / "site")],
    ]
    if words[0].startswith("entry"):
        commands.append(["record", "history", "SYN-01"])
    for command in commands:
        with pytest.raises(SystemExit) as exit_:
            main(["--db", db, *command])
        assert exit_.value.code == 1, command
        err = capsys.readouterr().err
        assert err.startswith("refused (503): "), (command, err)
        named(err, words)
        assert "Traceback" not in err
    with raw(db) as c:
        assert c.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 60  # nothing written
    with pytest.raises(SystemExit) as exit_:
        main(["--db", db, "verify"])
    assert exit_.value.code == 1
    assert "content" in capsys.readouterr().err


# ---- the evidence pack -----------------------------------------------------------------


@pytest.mark.parametrize("craft", CRAFTS, ids=IDS)
def test_the_evidence_pack_says_not_known_and_names_the_record(
    db: str, craft: Craft, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The pack is still written, since part 2 of 0.6.0 puts the verify result in it and a
    damaged store is where that matters; but nothing in it is computed without the record:
    what depended on it is unknown with the record named, and every item it would have
    supported is not evidenced for that reason. The wording keeps to the allowed statuses."""
    words = craft(db)
    store = Store(db)
    at = datetime(2026, 10, 1, tzinfo=UTC)
    pack = build_pack(store, "SYN-01", CONFIG, as_of=at, commit="abc1234", generated_utc=at)
    dumped = json.dumps(pack)
    named(dumped, words)
    assert COMPLIANT.search(dumped) is None
    assert pack["title"] == "Draft evidence pack for OSO #03: SYN-01"
    for item in pack["items"]:
        assert item["status"] in {"supported", "partly", "not evidenced"}
    affected = [i for i in pack["items"] if "cannot be read" in i["reason"]]
    assert affected and all(i["status"] == "not evidenced" for i in affected)
    assert all(i["sections"] == [] for i in affected)
    usage, programme = pack["usage"], pack["programme"]
    # Every value that depended on the record is {"unknown": <the record named>}.
    candidates = (
        pack["ledger_hash"],
        pack["counts"]["entries"],
        pack["counts"]["flights"],
        pack["counts"]["due_items"],
        usage["time_in_service_s"],
        usage["logged_s"],
        programme["status"],
    )
    unknown = [v for v in candidates if isinstance(v, dict) and "unknown" in v]
    assert len(unknown) >= 4, candidates
    for v in unknown:
        named(v["unknown"], words)
    # Whatever depends on the projection is unknown for every craft; the rest follows the
    # table the record is in.
    assert programme["status"] in unknown and usage["time_in_service_s"] in unknown
    assert pack["counts"]["due_items"] in unknown
    assert programme["items"] == [] and programme["status_reasons"] == []
    if words[0].startswith("entry"):
        assert pack["log"] == [] and pack["components"] == []
        assert "cannot be read" in pack["ledger_note"]
        assert "cannot be read" in pack["label"]
        # The flights are readable: they are listed, and each log is not judged, with why.
        assert len(usage["flights"]) == 3
        grounded = usage["grounded_flights"]
        assert grounded["judged"] == 0 and grounded["findings"] == []
        assert len(grounded["not_judged"]) == 3
        named(grounded["not_judged"][0]["why"], words)
    else:
        assert isinstance(usage["flights"], dict) and "unknown" in usage["flights"]
        assert isinstance(pack["counts"]["flights"], dict)
        grounded = usage["grounded_flights"]
        assert isinstance(grounded, dict) and "unknown" in grounded
        assert pack["log"]  # the ledger is readable: the log is shown
    html = render_html(pack)
    text = re.sub(r"<[^>]+>", " ", html.split("<body", 1)[1])
    named(text, words)
    assert BROKEN.search(text) is None, BROKEN.search(text)
    assert COMPLIANT.search(html) is None
    # The same pack from the command line and from the service, never a crash.
    out = tmp_path / "pack.html"
    main(["--db", db, "evidence", "SYN-01", "--out", str(out), "--json", "--as-of", AS_OF])
    assert "written to" in capsys.readouterr().out
    written = json.loads(out.with_suffix(".json").read_text(encoding="utf-8"))
    named(json.dumps(written), words)
    client = TestClient(create_app(store, write_token="t"))
    r = client.get(f"/aircraft/SYN-01/evidence?as_of={AS_OF}")
    assert r.status_code == 200, r.text[:200]
    named(json.dumps(r.json()), words)
    assert client.get(f"/aircraft/SYN-01/evidence.html?as_of={AS_OF}").status_code == 200
    store.close()


# ---- what stays as it was --------------------------------------------------------------


def test_a_readable_store_answers_exactly_as_before(db: str) -> None:
    """On a clean store nothing changes: /health keeps its four keys and says ok, no response
    carries a new field, and the pinned pack hash and journal head hold elsewhere."""
    store = Store(db)
    client = TestClient(create_app(store, write_token="t"))
    health = client.get("/health").json()
    assert set(health) == {"status", "version", "aircraft", "flights"}
    assert (health["status"], health["aircraft"], health["flights"]) == ("ok", 9, 34)
    for path in (*LEDGER_READS, *FLIGHT_READS):
        assert client.get(path).status_code == 200, path
    store.close()
