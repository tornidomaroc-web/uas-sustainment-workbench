"""Children run the code under test.

Several tests launch `uasw` in a process of its own, often in a working folder of its own.
Such a child imports whatever `uas_workbench` its own `sys.path` finds first. With a relative
`PYTHONPATH` (say `src`, given from a clone's root) and the child in another folder, that was
the editable install of another checkout, and those tests then checked that checkout's code
while reporting on this one: a verification saw 19 of 27 fail on code that was correct.

`pinned_pythonpath` puts the folder this test process imported `uas_workbench` from first on
`PYTHONPATH`, every entry absolute; `tests/conftest.py` applies it before any test runs, so
every child inherits it wherever it starts. `check_children_run_this_code` starts one child in
a folder of its own and fails the run, naming both folders, when that child imports anything
else (an interpreter started with `-I` or `-E`, a `.pth` file that wins, another Python).
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path

import uas_workbench


class ChildCodeMismatch(AssertionError):
    """A child process would import another `uas_workbench` than the one under test."""


def _same(path: str | Path) -> str:
    return os.path.normcase(os.path.realpath(path))


def under_test() -> Path:
    """The `uas_workbench` package this test process imported."""
    return Path(_same(Path(uas_workbench.__file__).parent))


def pinned_pythonpath(current: str | None) -> str:
    """`current` with the folder `uas_workbench` was imported from first and every entry made
    absolute against the working folder now, so a child elsewhere resolves them the same."""
    first = _same(under_test().parent)
    rest = [_same(Path(p).resolve()) for p in (current or "").split(os.pathsep) if p]
    return os.pathsep.join([first, *[p for p in rest if p != first]])


def child_package(cwd: Path | None = None, env: Mapping[str, str] | None = None) -> Path:
    """The `uas_workbench` package a child started in `cwd` (a new folder by default) with
    `env` (this process's by default) imports."""
    probe = "import pathlib, uas_workbench; print(pathlib.Path(uas_workbench.__file__).parent)"
    with tempfile.TemporaryDirectory() as fresh:
        r = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=cwd or fresh,
            env=dict(os.environ if env is None else env),
            capture_output=True,
            text=True,
            timeout=120,
        )
    if r.returncode != 0:
        raise ChildCodeMismatch(f"a child process cannot import uas_workbench: {r.stderr[-300:]}")
    return Path(_same(r.stdout.strip()))


def check_children_run_this_code(env: Mapping[str, str] | None = None) -> None:
    """Fail, naming both, when a child process would run other code than this process."""
    found, expected = child_package(env=env), under_test()
    if found != expected:
        raise ChildCodeMismatch(
            f"a uasw child process would import uas_workbench from {found}, not the code under "
            f"test at {expected}; tests that launch uasw would check the wrong code"
        )
