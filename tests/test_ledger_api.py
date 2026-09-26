"""Writes through the API: guarded, validated, auditable, and never a 500."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from uas_workbench.fleet import load_config
from uas_workbench.fleet.showcase import ALFA_KEY, showcase
from uas_workbench.fleet.synthetic import generate
from uas_workbench.service.app import create_app
from uas_workbench.service.store import Store

FIXTURES = Path(__file__).parent / "fixtures"
CONFIG = load_config()
TOKEN = "test-write-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
T = "2026-09-01T09:00:00Z"


def seeded() -> Store:
    store = Store(":memory:")
    store.add_fleet(generate(CONFIG))
    store.add_fleet(showcase(FIXTURES))
    return store


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(create_app(seeded(), write_token=TOKEN))


def body(
    subject: str, kind: str, payload: dict[str, Any] | None = None, **extra: Any
) -> dict[str, Any]:
    return {
        "subject": subject,
        "kind": kind,
        "occurred_utc": T,
        "entered_by": "A. Tester, maintenance",
        "statement": "work done as described",
        "payload": payload or {},
        **extra,
    }


def test_writes_need_the_token_when_one_is_set(client: TestClient) -> None:
    b = body("SYN-01", "work_order.open", {"state": "in_work"})
    assert client.post("/entries", json=b).status_code == 401
    assert (
        client.post("/entries", json=b, headers={"Authorization": "Bearer wrong"}).status_code
        == 401
    )
    with (FIXTURES / "px4" / "flight_review_board_validation_2026-06-12_excerpt.ulg").open(
        "rb"
    ) as f:
        r = client.post(
            "/ingest",
            data={"aircraft_key": "x"},
            files={"log": ("a.ulg", f, "application/octet-stream")},
        )
    assert r.status_code == 401  # every write is guarded, ingest included
    assert client.get("/entries").status_code == 200  # reads stay open


def test_without_a_token_only_loopback_may_write() -> None:
    app = create_app(seeded(), write_token=None)
    b = body("SYN-01", "work_order.open", {"state": "in_work"})
    remote = TestClient(app, client=("172.17.0.1", 40000))
    r = remote.post("/entries", json=b)
    assert r.status_code == 403 and "UASW_WRITE_TOKEN" in r.json()["detail"]
    local = TestClient(app, client=("127.0.0.1", 40000))
    assert local.post("/entries", json=b).status_code == 201


def test_an_entry_is_appended_and_read_back_with_the_note(client: TestClient) -> None:
    r = client.post(
        "/entries",
        json=body(
            "SYN-01",
            "work_order.open",
            {"state": "awaiting_parts"},
            statement="replacement propeller set on order",
        ),
        headers=AUTH,
    )
    assert r.status_code == 201, r.text
    e = r.json()
    assert e["id"] >= 1 and e["synthetic"] is False and e["superseded_by"] is None
    assert e["payload"]["work_id"] == f"WO-{e['id']}"
    assert "does not certify airworthiness" in e["note"]
    assert client.get(f"/entries/{e['id']}").json() == e
    listed = client.get("/entries", params={"subject": "SYN-01"}).json()
    assert e in listed and all(x["subject"] == "SYN-01" for x in listed)
    aircraft = client.get("/aircraft/SYN-01").json()
    assert aircraft["status"] == "AOG"
    assert any("replacement propeller set on order" in s for s in aircraft["status_reasons"])
    assert client.get("/entries/999999").status_code == 404


def test_refusals_carry_their_status_and_a_sentence(client: TestClient) -> None:
    cases = [
        (body("NO-SUCH", "work_order.open", {"state": "in_work"}), 404),
        (body(ALFA_KEY, "work_order.open", {"state": "in_work"}), 403),
        (body("SYN-01", "work_order.close", {"work_id": "WO-none"}), 409),
        (
            body(
                "SYN-01",
                "work_order.open",
                {"state": "in_work"},
                occurred_utc="2099-01-01T00:00:00Z",
            ),
            422,
        ),
        (body("SYN-01", "time_in_service.set", {"before_s": -5}), 422),
        (body("SYN-01", "work_order.open", {"state": "in_work"}, entered_by=""), 422),
        (body("SYN-01", "no.kind"), 422),
        ({"garbage": True}, 422),
    ]
    for payload, status in cases:
        r = client.post("/entries", json=payload, headers=AUTH)
        assert r.status_code == status, (payload, r.text)
        assert r.status_code != 500
        detail = r.json()["detail"]
        assert detail and isinstance(detail, str | list)
    r = client.post(
        "/entries", content=b"not json", headers={**AUTH, "Content-Type": "application/json"}
    )
    assert r.status_code == 422


def test_corrections_and_history_through_the_api(client: TestClient) -> None:
    first = client.post(
        "/entries", json=body("SYN-03", "time_in_service.set", {"before_s": 100.0}), headers=AUTH
    ).json()
    second = client.post(
        "/entries",
        json=body(
            "SYN-03",
            "time_in_service.set",
            {"before_s": 200.0},
            supersedes=first["id"],
            reason="typo in the hours",
        ),
        headers=AUTH,
    ).json()
    assert second["supersedes"] == first["id"] and second["reason"] == "typo in the hours"
    live = client.get("/entries", params={"subject": "SYN-03"}).json()
    assert [e["id"] for e in live if e["kind"] == "time_in_service.set"] == [second["id"]]
    everything = client.get(
        "/entries", params={"subject": "SYN-03", "include_superseded": "true"}
    ).json()
    old = next(e for e in everything if e["id"] == first["id"])
    assert old["superseded_by"] == second["id"]
    assert client.get("/aircraft/SYN-03/due").json()["time_in_service_s"] > 200.0


def test_components_are_readable_with_their_state_and_history(client: TestClient) -> None:
    items = client.get("/components").json()
    assert any(c["id"] == "BAT-04A" and c["installed_on"] == "SYN-04" for c in items)
    one = client.get("/components/BAT-04A").json()
    assert one["kind"] == "battery pack" and one["synthetic"] is True
    assert [i["aircraft_key"] for i in one["installations"]] == ["SYN-01", "SYN-04"]
    assert one["entries"] and all(e["subject"] == "BAT-04A" for e in one["entries"])
    assert client.get("/components/NO-PART").status_code == 404
    r = client.post(
        "/entries",
        json=body(
            "NEW-9",
            "component.register",
            {
                "kind": "propeller set",
                "in_service_since": "2026-08-01",
                "hours_s_before": 0,
                "cycles_before": 0,
            },
        ),
        headers=AUTH,
    )
    assert r.status_code == 201, r.text
    assert client.get("/components/NEW-9").json()["installed_on"] is None


def test_the_openapi_marks_writes_and_the_assistant_has_no_write_tool(client: TestClient) -> None:
    from uas_workbench.assistant.tools import TOOLS

    paths = client.get("/openapi.json").json()["paths"]
    assert "post" in paths["/entries"] and "get" in paths["/entries"]
    assert {t.path for t in TOOLS}.isdisjoint({"/entries", "/ingest", "/components"})
