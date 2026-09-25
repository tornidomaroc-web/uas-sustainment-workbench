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


@pytest.fixture(scope="module")
def client() -> TestClient:
    store = Store(":memory:")
    store.add_fleet(generate(CONFIG))
    store.add_fleet(showcase(FIXTURES))
    return TestClient(create_app(store))


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
    synthetic = client.get(f"/aircraft/{client.get('/aircraft').json()[0]['key']}/flights").json()
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
    assert 'path="/aircraft"' in text


def test_log_lines_are_json() -> None:
    record = logging.LogRecord("uasw", logging.INFO, __file__, 1, "request", None, None)
    record.request_id = "abc"
    record.duration_ms = 3.2
    line = json.loads(JsonFormatter().format(record))
    assert line["message"] == "request" and line["level"] == "INFO"
    assert line["request_id"] == "abc" and line["duration_ms"] == 3.2
    assert "ts" in line
