"""The workbench needs pyulog 1.3.0 or later (issue #121).

On 1.2.4 a format definition may declare an array of any size, and reading the log takes time
that grows with that size, whatever the file's own: 33 s for a 139-byte file declaring 10
million elements, measured 2026-10-21. 1.3.0 (PX4/pyulog#128) marks such a file corrupt, and
the workbench refuses it at once, as it refuses any log pyulog reports damaged.
"""

from __future__ import annotations

import re
import tomllib
from importlib.metadata import version
from pathlib import Path

import pytest
from test_flight_px4 import ATTR
from test_ulog_damaged import DAMAGED

from builders.ulog import ULogBuilder
from uas_workbench.flight.px4 import read_ulog

ROOT = Path(__file__).resolve().parents[1]
S = 1_000_000


def declared_floor() -> tuple[int, ...]:
    dependencies = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))["project"][
        "dependencies"
    ]
    (pyulog,) = [d for d in dependencies if re.match(r"pyulog\b", d)]
    m = re.fullmatch(r"pyulog>=(\d+)\.(\d+)\.(\d+),<2", pyulog)
    assert m, pyulog
    return tuple(int(part) for part in m.groups())


def test_the_package_declares_pyulog_1_3_0_or_later() -> None:
    assert declared_floor() == (1, 3, 0)


def test_the_installed_pyulog_is_one_the_package_allows() -> None:
    """An environment installed before the floor moved still holds 1.2.4; `python -m pip
    install -e ".[dev]"` replaces it."""
    installed = tuple(int(part) for part in version("pyulog").split(".")[:3])
    assert installed >= declared_floor(), version("pyulog")


@pytest.mark.parametrize("elements", [65_536, 100_000])
def test_a_definition_declaring_a_huge_array_is_refused_as_damaged(
    elements: int, tmp_path: Path
) -> None:
    """A header, one definition and a subscription to it, no data. 1.2.4 reads them, taking
    longer the larger the array (0.18 s for 100 000, 2.3 s for a million), and the log is
    refused for holding no data; 1.3.0 marks it corrupt, and the log is refused as damaged."""
    log = ULogBuilder(start_us=S).format("huge", [f"uint8_t[{elements}] x"])
    log.subscribe("huge")
    with pytest.raises(ValueError) as refused:
        read_ulog(log.write(tmp_path / "huge.ulg"), **ATTR)
    assert str(refused.value) == DAMAGED
