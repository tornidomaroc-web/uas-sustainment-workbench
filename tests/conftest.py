"""Every Store a test or a fixture opens is closed when its scope ends.

The suite treats a resource left open as an error (`filterwarnings` in pyproject.toml): Python
3.13+ warns for a sqlite3 connection collected unclosed, and the warning lands on whichever
test is running when the collector gets to it, which is how one leak fails an unrelated test
(issue #48). The product closes what it opens; a test's `Store(":memory:")` is scaffolding,
and this closes it in one place rather than at every site: at the end of the test for a store
the test opened, at the end of the module for one a module-scoped fixture opened.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from uas_workbench.service.store import Store


def _closing_every_store() -> Iterator[None]:
    opened: list[Store] = []
    init = Store.__init__

    def tracking_init(self: Store, path: str = ":memory:") -> None:
        init(self, path)
        opened.append(self)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(Store, "__init__", tracking_init)
        yield
    for store in opened:
        store.close()  # idempotent: a store closed by its test is closed again, harmlessly


@pytest.fixture(scope="module", autouse=True)
def close_module_stores() -> Iterator[None]:
    yield from _closing_every_store()


@pytest.fixture(autouse=True)
def close_test_stores() -> Iterator[None]:
    yield from _closing_every_store()
