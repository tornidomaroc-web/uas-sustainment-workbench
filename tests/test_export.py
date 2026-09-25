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
    assert len(alfa["flights"]) == 3
    assert alfa["reconcile"]["findings"][0]["seconds"] == 355.0
    assert all(isinstance(a["synthetic"], bool) for a in data["aircraft"])
    assert any(f["synthetic"] for f in data["findings"])
    html = (out / "index.html").read_text(encoding="utf-8")
    assert "fleet.json" in html and "synthetic" in html.lower()
