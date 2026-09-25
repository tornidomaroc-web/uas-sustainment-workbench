import json
from pathlib import Path

from uas_workbench.fleet import load_config
from uas_workbench.fleet.showcase import ALFA_KEY, showcase
from uas_workbench.fleet.synthetic import generate
from uas_workbench.service.static_export import export_static
from uas_workbench.service.store import Store

FIXTURES = Path(__file__).parent / "fixtures"


def test_static_export_is_the_same_data_as_the_api(tmp_path: Path) -> None:
    store = Store(":memory:")
    store.add_fleet(generate(load_config()))
    store.add_fleet(showcase(FIXTURES))

    out = export_static(store, tmp_path / "site")

    assert (out / "index.html").exists()
    data = json.loads((out / "fleet.json").read_text(encoding="utf-8"))
    assert "synthetic" in data["notice"].lower()
    assert data["generated_utc"]
    alfa = next(a for a in data["aircraft"] if a["key"] == ALFA_KEY)
    assert alfa["synthetic"] is False
    assert alfa["flights"] == 3  # the count, as the API returns it
    assert len(alfa["records"]) == 3
    assert all(isinstance(a["flights"], int) for a in data["aircraft"])
    assert round(alfa["reconcile"]["findings"][0]["seconds"]) == 355
    assert all(isinstance(a["synthetic"], bool) for a in data["aircraft"])
    assert any(f["synthetic"] for f in data["findings"])
    html = (out / "index.html").read_text(encoding="utf-8")
    assert "fleet.json" in html and "synthetic" in html.lower()

    # The due view travels with the page: per aircraft, and the fleet's due-soon and overdue
    # items at the top, worst first, computed at the stated time.
    assert data["as_of"]
    assert all(i["state"] != "ok" for i in data["due"])
    assert any(i["state"] == "overdue" for i in data["due"])
    assert any(i["state"] == "due_soon" for i in data["due"])
    syn04 = next(a for a in data["aircraft"] if a["key"] == "SYN-04")
    assert syn04["status"] == "unserviceable" and syn04["status_reasons"]
    assert syn04["due"]["items"] and syn04["due"]["as_of"] == data["as_of"]
    assert alfa["status"] == {"unknown": "no maintenance record entered for this aircraft"}
    assert alfa["due"]["items"] == []
