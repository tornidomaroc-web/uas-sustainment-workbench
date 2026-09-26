"""The HTTP API over the seeded fleet plus the real showcase excerpts."""

import json
import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from uas_workbench.fleet import load_config
from uas_workbench.fleet.showcase import ALFA_KEY, PX4_KEY, showcase
from uas_workbench.fleet.synthetic import generate
from uas_workbench.service.app import create_app
from uas_workbench.service.observability import JsonFormatter
from uas_workbench.service.store import Store

FIXTURES = Path(__file__).parent / "fixtures"
CONFIG = load_config()
PX4_FIXTURE = FIXTURES / "px4" / "flight_review_board_validation_2026-06-12_excerpt.ulg"
NO_RECORD = "no maintenance record entered for this aircraft"


@pytest.fixture(scope="module")
def client() -> TestClient:
    store = Store(":memory:")
    store.add_fleet(generate(CONFIG))
    store.add_fleet(showcase(FIXTURES))
    # A user on this machine: with no write token set, loopback may write (ingest).
    return TestClient(create_app(store, write_token=None), client=("127.0.0.1", 50000))


def test_health_and_docs(client: TestClient) -> None:
    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/docs").status_code == 200
    assert client.get("/openapi.json").json()["info"]["title"]


def test_aircraft_list_marks_synthetic_and_real(client: TestClient) -> None:
    items = client.get("/aircraft").json()
    assert len(items) == CONFIG.aircraft + 2
    by_key = {a["key"]: a for a in items}
    assert by_key[ALFA_KEY]["synthetic"] is False
    assert by_key[PX4_KEY]["synthetic"] is False
    assert sum(a["synthetic"] for a in items) == CONFIG.aircraft
    for a in items:
        assert isinstance(a["synthetic"], bool)
        assert a["status"] in CONFIG.states or set(a["status"]) == {"unknown"}
        assert a["flights"] >= 1
        assert "licence" in a and "attribution" in a


def test_flights_carry_explicit_unknowns_and_the_synthetic_flag(client: TestClient) -> None:
    flights = client.get(f"/aircraft/{ALFA_KEY}/flights").json()
    assert [f["log_ref"] for f in flights] == [
        "2018-07-30_16-30-14",
        "2018-07-30_16-46-36",
        "2018-07-30_17-28-50",
    ]
    first = flights[0]
    assert first["synthetic"] is False
    assert first["arm_cycles"] == {
        "unknown": "ARMING_REQUIRE=0: the aircraft is always armed and logs no arm events"
    }
    assert first["battery_mah"] == {"unknown": "battery monitor disabled (BATT_MONITOR=0)"}
    assert first["landings"] == 1 and first["boot_count"] == 326
    first_synthetic = next(a["key"] for a in client.get("/aircraft").json() if a["synthetic"])
    synthetic = client.get(f"/aircraft/{first_synthetic}/flights").json()
    assert all(f["synthetic"] is True for f in synthetic)


def test_reconcile_reports_the_unlogged_alfa_flight(client: TestClient) -> None:
    result = client.get(f"/aircraft/{ALFA_KEY}/reconcile").json()
    assert result["aircraft_key"] == ALFA_KEY and result["synthetic"] is False
    assert [f["message"] for f in result["findings"]] == [
        "355 s of flight on aircraft alfa-fixed-wing is not covered by any log "
        "(after 2018-07-30_16-46-36, before 2018-07-30_17-28-50)"
    ]
    assert result["unchecked"] == {
        "2018-07-30_17-28-50": "last known log of this aircraft; the counter was not seen again"
    }
    assert [round(c["unlogged_s"], 1) for c in result["coverage"]] == [-8.4, 355.0]


def test_fleet_findings_cover_synthetic_and_real_aircraft(client: TestClient) -> None:
    findings = client.get("/fleet/findings").json()
    assert any(f["aircraft_key"] == ALFA_KEY and f["synthetic"] is False for f in findings)
    assert any(f["synthetic"] is True and f["kind"] == "unlogged_flight" for f in findings)
    assert all({"aircraft_key", "log_ref", "kind", "seconds", "message", "synthetic"} <= set(f)
               for f in findings)  # fmt: skip


def test_board_states_are_computed_with_their_reasons(client: TestClient) -> None:
    by_key = {a["key"]: a for a in client.get("/aircraft").json()}
    assert by_key["SYN-01"]["status"] == "serviceable"
    assert by_key["SYN-01"]["status_reasons"] == []
    assert by_key["SYN-03"]["status"] == "serviceable with deferred defects"
    assert by_key["SYN-04"]["status"] == "unserviceable"
    assert by_key["SYN-05"]["status"] == "in maintenance"
    assert by_key["SYN-07"]["status"] == "AOG"
    assert by_key["SYN-06"]["status"] == {
        "unknown": "no maintenance record entered for this aircraft"
    }
    for key in (ALFA_KEY, PX4_KEY):
        assert by_key[key]["status"] == {
            "unknown": "no maintenance record entered for this aircraft"
        }
        assert by_key[key]["status_reasons"] == []
    for a in by_key.values():
        assert isinstance(a["status_reasons"], list)
        assert isinstance(a["overdue"], int) and isinstance(a["due_soon"], int)
    assert by_key["SYN-04"]["overdue"] >= 2
    assert by_key["SYN-02"]["due_soon"] >= 1
    reasons = by_key["SYN-04"]["status_reasons"]
    assert any("cycles past its" in r and "battery pack" in r for r in reasons)
    assert any("calendar-month life limit on 2026-" in r for r in reasons)


def test_due_view_per_aircraft_lists_every_item_with_its_source(client: TestClient) -> None:
    due = client.get("/aircraft/SYN-04/due").json()
    assert due["aircraft_key"] == "SYN-04" and due["synthetic"] is True
    assert due["status"] == "unserviceable" and due["as_of"]
    assert due["time_in_service_s"] > 0
    states = {i["state"] for i in due["items"]}
    assert "overdue" in states
    for i in due["items"]:
        assert {"subject", "basis", "unit", "used", "limit", "remaining", "tolerance",
                "state", "source", "message", "component_id"} <= set(i)  # fmt: skip
        assert i["state"] in ("ok", "due_soon", "overdue_within_tolerance", "overdue")
        assert "http" in i["source"] and i["message"]
    cycles = next(i for i in due["items"] if i["basis"] == "cycles" and i["state"] == "overdue")
    assert cycles["message"] in due["status_reasons"]

    fixed = client.get("/aircraft/SYN-04/due", params={"as_of": "2026-10-01T00:00:00Z"}).json()
    assert fixed["as_of"].startswith("2026-10-01")

    real = client.get(f"/aircraft/{ALFA_KEY}/due").json()
    assert real["synthetic"] is False and real["items"] == []
    assert real["status"] == {"unknown": "no maintenance record entered for this aircraft"}
    assert real["time_in_service_s"] == {"unknown": NO_RECORD}
    assert client.get("/aircraft/no-such-aircraft/due").status_code == 404


def test_fleet_due_view_is_the_due_soon_and_overdue_items_worst_first(client: TestClient) -> None:
    items = client.get("/fleet/due").json()
    assert items and all(i["state"] != "ok" for i in items)
    order = ["overdue", "overdue_within_tolerance", "due_soon"]
    ranks = [order.index(i["state"]) for i in items]
    assert ranks == sorted(ranks)
    assert {i["aircraft_key"] for i in items} >= {"SYN-02", "SYN-03", "SYN-04"}
    assert all(isinstance(i["synthetic"], bool) for i in items)


def test_unknown_aircraft_is_404(client: TestClient) -> None:
    response = client.get("/aircraft/no-such-aircraft/flights")
    assert response.status_code == 404
    assert "no-such-aircraft" in response.json()["detail"]


def test_ingest_parses_a_real_log_and_keeps_no_raw_copy(client: TestClient) -> None:
    with PX4_FIXTURE.open("rb") as f:
        response = client.post(
            "/ingest",
            data={"aircraft_key": "ingested-01", "licence": "CC BY 4.0", "attribution": "test"},
            files={"log": (PX4_FIXTURE.name, f, "application/octet-stream")},
        )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["synthetic"] is False and body["source"] == "px4"
    assert body["flight_time_s"] == pytest.approx(199.1, abs=0.1)
    flights = client.get("/aircraft/ingested-01/flights").json()
    assert [f["log_ref"] for f in flights] == [PX4_FIXTURE.stem]

    bad = client.post(
        "/ingest",
        data={"aircraft_key": "ingested-01"},
        files={"log": ("junk.ulg", b"not a log at all", "application/octet-stream")},
    )
    assert bad.status_code == 422
    assert "not a readable" in bad.json()["detail"].lower()


def test_metrics_and_request_ids(client: TestClient) -> None:
    response = client.get("/aircraft")
    assert response.headers["x-request-id"]
    text = client.get("/metrics").text
    assert "uasw_http_requests_total" in text
    assert "uasw_flights_stored" in text
    assert 'uasw_aircraft_by_status{status="unserviceable"} 1.0' in text
    assert 'path="/aircraft"' in text


def test_log_lines_are_json() -> None:
    record = logging.LogRecord("uasw", logging.INFO, __file__, 1, "request", None, None)
    record.request_id = "abc"
    record.duration_ms = 3.2
    line = json.loads(JsonFormatter().format(record))
    assert line["message"] == "request" and line["level"] == "INFO"
    assert line["request_id"] == "abc" and line["duration_ms"] == 3.2
    assert "ts" in line
