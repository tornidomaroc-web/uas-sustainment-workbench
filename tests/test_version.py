"""The version the code reports is the version the package declares, and it is the release."""

import tomllib
from pathlib import Path

from uas_workbench import __version__

ROOT = Path(__file__).resolve().parents[1]
RELEASE = "0.1.0"


def test_code_and_package_declare_the_same_version() -> None:
    declared = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))["project"]["version"]
    assert __version__ == declared


def test_the_version_is_the_first_release() -> None:
    assert __version__ == RELEASE
