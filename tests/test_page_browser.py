"""The generated page, loaded in a real headless browser the way a visitor sees it.

Data tests cannot catch a template that prints an object where a number belongs. This test
serves the static export over HTTP, renders it in Chromium and fails on anything a visitor
would notice: "[object Object]", "undefined", "NaN", an empty cell, a console error, a
finding that is not visible at the top, a status that is not civil vocabulary.

Needs `pip install playwright && playwright install chromium` (the CI job does this);
skips with a reason otherwise, so the plain unit-test run stays dependency-free.
"""

from __future__ import annotations

import http.server
import re
import threading
from collections.abc import Iterator
from functools import partial
from pathlib import Path
from typing import Any

import pytest

from uas_workbench.fleet import load_config
from uas_workbench.fleet.showcase import ALFA_KEY, showcase
from uas_workbench.fleet.synthetic import generate
from uas_workbench.service.static_export import export_static
from uas_workbench.service.store import Store

pytestmark = pytest.mark.browser
playwright = pytest.importorskip("playwright.sync_api", reason="playwright not installed")

FIXTURES = Path(__file__).parent / "fixtures"
CONFIG = load_config()
BROKEN = re.compile(r"\[object Object\]|\bundefined\b|\bNaN\b")
ALFA_SENTENCE = "355 s of flight on aircraft alfa-fixed-wing is not covered by any log"


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture(scope="module")
def site_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    store = Store(":memory:")
    store.add_fleet(generate(CONFIG))
    store.add_fleet(showcase(FIXTURES))
    site = export_static(store, tmp_path_factory.mktemp("site"))
    handler = partial(_QuietHandler, directory=str(site))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/"
    server.shutdown()


@pytest.fixture(scope="module")
def page(site_url: str) -> Iterator[Any]:
    with playwright.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as exc:  # no browser binary on this machine
            pytest.skip(f"chromium could not be launched: {str(exc).splitlines()[0]}")
        page = browser.new_page()
        problems: list[str] = []
        page.on("console", lambda m: problems.append(f"console {m.type}: {m.text}")
                if m.type in ("error", "warning") else None)  # fmt: skip
        page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
        page.goto(site_url, wait_until="networkidle")
        page.wait_for_selector("#fleet tbody tr")
        page.problems = problems
        yield page
        browser.close()


def cells(page: Any, selector: str) -> list[str]:
    return [t.strip() for t in page.locator(selector).all_inner_texts()]


def test_no_raw_objects_or_console_errors(page: Any) -> None:
    body = page.inner_text("body")
    assert BROKEN.search(body) is None, BROKEN.search(body)
    assert page.problems == []


def test_findings_are_readable_at_the_top(page: Any) -> None:
    findings = page.locator("#findings li")
    assert findings.count() >= 3
    assert ALFA_SENTENCE in " ".join(findings.all_inner_texts())
    top = page.locator("#findings").bounding_box()
    fleet = page.locator("#fleet").bounding_box()
    assert top is not None and fleet is not None and top["y"] < fleet["y"]


def test_every_fleet_cell_is_filled_and_status_is_civil(page: Any) -> None:
    rows = page.locator("#fleet tbody tr")
    assert rows.count() == CONFIG.aircraft + 2
    for text in cells(page, "#fleet tbody td"):
        assert text, "empty cell in the fleet table"
    statuses = cells(page, "#fleet tbody td.status")
    allowed = set(CONFIG.states) | {"unknown"}
    for status in statuses:
        assert status.split("\n")[0] in allowed, status
    labels = cells(page, "#fleet tbody td.data")
    assert sum("synthetic" in t for t in labels) == CONFIG.aircraft
    assert sum("real" in t for t in labels) == 2


def test_clicking_an_aircraft_shows_its_flights(page: Any) -> None:
    page.locator(f"#fleet tbody tr[data-key='{ALFA_KEY}']").click()
    page.wait_for_selector("#flights tbody tr")
    rows = page.locator("#flights tbody tr")
    assert rows.count() == 3
    for text in cells(page, "#flights tbody td"):
        assert text and BROKEN.search(text) is None, text
    assert "2018-07-30_16-46-36" in page.inner_text("#flights")
    assert page.problems == []
