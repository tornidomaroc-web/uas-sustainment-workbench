"""`uasw` closes every store a command opens, however the command ends (issue #48).

Checked here directly, on every Python version: tests/conftest.py closes every Store at the end
of a test, so a command that stopped closing its store would still pass the suite's
ResourceWarning filter. This test looks at the stores before that teardown runs.
"""

from __future__ import annotations

import contextlib
import sqlite3
from pathlib import Path

import pytest

from uas_workbench.service.cli import main
from uas_workbench.service.store import Store

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def opened(monkeypatch: pytest.MonkeyPatch) -> list[Store]:
    """Every Store opened while the test runs, in order."""
    stores: list[Store] = []
    init = Store.__init__

    def tracking(self: Store, path: str = ":memory:") -> None:
        init(self, path)
        stores.append(self)

    monkeypatch.setattr(Store, "__init__", tracking)
    return stores


def is_closed(store: Store) -> bool:
    try:
        store.entry_count()
    except sqlite3.ProgrammingError:
        return True
    return False


@pytest.mark.parametrize(
    "command",
    [
        ["verify"],
        ["evidence", "SYN-04", "--out", "{tmp}/pack.html", "--as-of", "2026-10-01T00:00:00Z"],
        ["ingest", "SYN-01", "{tmp}/notes.txt"],  # refused, exit 1
    ],
    ids=["verify", "evidence", "ingest-refused"],
)
def test_every_store_a_command_opens_is_closed_when_it_ends(
    tmp_path: Path, opened: list[Store], command: list[str]
) -> None:
    db = str(tmp_path / "fleet.sqlite")
    (tmp_path / "notes.txt").write_text("not a log", encoding="utf-8")
    main(["--db", db, "seed", "--fixtures", str(FIXTURES)])
    argv = ["--db", db, *(part.replace("{tmp}", str(tmp_path)) for part in command)]
    with contextlib.suppress(SystemExit):
        main(argv)
    assert len(opened) >= 2, "seed and the command each open the store"
    assert all(is_closed(s) for s in opened), "a store a command opened is still open"
