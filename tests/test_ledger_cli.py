"""uasw record: the same append and the same refusals as the API, on a store file."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from uas_workbench.service.cli import main
from uas_workbench.service.store import Store

FIXTURES = Path(__file__).parent / "fixtures"
BY = ["--by", "A. Tester, maintenance"]
AT = ["--at", "2026-09-01T09:00:00Z"]


@pytest.fixture
def db(tmp_path: Path) -> str:
    path = str(tmp_path / "fleet.sqlite")
    main(["--db", path, "seed", "--fixtures", str(FIXTURES)])
    return path


def run(db: str, *args: str) -> None:
    main(["--db", db, "record", *args])


def test_work_order_round_trip(db: str, capsys: pytest.CaptureFixture[str]) -> None:
    run(
        db,
        "work-order",
        "open",
        "SYN-01",
        "--state",
        "awaiting_parts",
        *BY,
        *AT,
        "--statement",
        "replacement propeller set on order",
    )
    out = capsys.readouterr().out
    assert "WO-" in out and "does not certify" in out
    wid = next(w for w in out.split() if w.startswith("WO-")).strip(".,")
    run(
        db,
        "work-order",
        "state",
        "SYN-01",
        wid,
        "--state",
        "in_work",
        *BY,
        "--at",
        "2026-09-02T09:00:00Z",
        "--statement",
        "parts received",
    )
    run(
        db,
        "work-order",
        "close",
        "SYN-01",
        wid,
        *BY,
        "--at",
        "2026-09-03T09:00:00Z",
        "--statement",
        "propeller set replaced",
    )
    store = Store(db)
    assert store.maintenance("SYN-01").work_orders == ()  # type: ignore[union-attr]
    assert [w.state for w in store.projection().work_orders["SYN-01"] if w.work_id == wid] == [
        "in_work"
    ]
    run(db, "history", "SYN-01")
    out = capsys.readouterr().out
    assert wid in out and "propeller set replaced" in out


def test_component_lifecycle_and_inspection(db: str, capsys: pytest.CaptureFixture[str]) -> None:
    run(
        db,
        "component",
        "register",
        "NEW-1",
        "--kind",
        "battery pack",
        "--since",
        "2026-08-01",
        "--hours-before",
        "1.5",
        "--cycles-before",
        "12",
        *BY,
        "--at",
        "2026-08-01T00:00:00Z",
        "--statement",
        "new pack received",
    )
    run(db, "component", "install", "NEW-1", "SYN-01", *BY, *AT, "--statement", "fitted")
    run(
        db,
        "inspection",
        "SYN-01",
        "100-hour inspection",
        *BY,
        "--at",
        "2026-09-02T09:00:00Z",
        "--statement",
        "100-hour inspection completed",
    )
    run(
        db,
        "time-in-service",
        "SYN-01",
        "42.5",
        *BY,
        "--at",
        "2026-08-01T00:00:00Z",
        "--statement",
        "hours from the previous logbook",
    )
    store = Store(db)
    (c,) = [c for c in store.components() if c.id == "NEW-1"]
    assert (c.hours_s_before, c.cycles_before, c.installations[0].aircraft_key) == (
        5400.0,
        12,
        "SYN-01",
    )
    record = store.maintenance("SYN-01")
    assert record is not None and record.time_in_service_before_s == 42.5 * 3600
    assert record.inspections[-1].name == "100-hour inspection"
    run(
        db,
        "component",
        "remove",
        "NEW-1",
        "SYN-01",
        *BY,
        "--at",
        "2026-09-05T09:00:00Z",
        "--statement",
        "removed for storage",
    )
    (c,) = [c for c in Store(db).components() if c.id == "NEW-1"]
    assert c.installations[0].to_utc is not None


def test_a_refusal_exits_non_zero_with_the_sentence(
    db: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc:
        run(db, "work-order", "close", "SYN-01", "WO-none", *BY, *AT, "--statement", "x")
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "WO-none" in err and "409" in err
    with pytest.raises(SystemExit):
        run(
            db,
            "work-order",
            "open",
            "alfa-fixed-wing",
            "--state",
            "in_work",
            *BY,
            *AT,
            "--statement",
            "x",
        )
    assert "public" in capsys.readouterr().err


def test_evidence_command_writes_html_and_json(db: str, tmp_path: Path) -> None:
    out = tmp_path / "SYN-04.html"
    main(["--db", db, "evidence", "SYN-04", "--out", str(out), "--json",
          "--as-of", "2026-10-01T00:00:00Z"])  # fmt: skip
    html = out.read_text(encoding="utf-8")
    assert html.startswith("<!doctype html>") and "Draft evidence pack for OSO #03: SYN-04" in html
    data = json.loads((tmp_path / "SYN-04.json").read_text(encoding="utf-8"))
    assert data["as_of"] == "2026-10-01T00:00:00Z" and data["ledger_hash"]
    assert data["commit"] != "" and len(data["commit"]) >= 7  # from git here, or "unknown"
    with pytest.raises(SystemExit):
        main(["--db", db, "evidence", "NO-SUCH", "--out", str(tmp_path / "x.html")])


def test_correct_and_retract(db: str, capsys: pytest.CaptureFixture[str]) -> None:
    run(db, "time-in-service", "SYN-02", "10", *BY, *AT, "--statement", "first entry")
    first = int(capsys.readouterr().out.split("entry ")[1].split()[0])
    run(
        db,
        "time-in-service",
        "SYN-02",
        "20",
        *BY,
        *AT,
        "--statement",
        "corrected",
        "--supersedes",
        str(first),
        "--reason",
        "typo",
    )
    second = int(capsys.readouterr().out.split("entry ")[1].split()[0])
    assert Store(db).maintenance("SYN-02").time_in_service_before_s == 20 * 3600  # type: ignore[union-attr]
    run(db, "retract", str(second), *BY, "--reason", "the first was right")
    assert Store(db).maintenance("SYN-02").time_in_service_before_s == 10 * 3600  # type: ignore[union-attr]
    run(db, "history", "SYN-02", "--all")
    out = capsys.readouterr().out
    assert "superseded by" in out and "the first was right" in out
