"""The version the code reports is the version the package declares, and it is the release."""

import re
import tomllib
from datetime import date
from pathlib import Path

from uas_workbench import __version__

ROOT = Path(__file__).resolve().parents[1]
RELEASE = "0.6.2"


def test_code_and_package_declare_the_same_version() -> None:
    declared = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))["project"]["version"]
    assert __version__ == declared


def test_the_version_is_the_release() -> None:
    assert __version__ == RELEASE


def test_the_newest_changelog_section_is_this_version_and_dated() -> None:
    """The CHANGELOG opens with an Unreleased heading, then the section of the version the code
    reports, headed with the date it was prepared: so a version string and its notes cannot go
    out apart, and the date is a real calendar day."""
    text = (ROOT / "CHANGELOG.md").read_text("utf-8")
    headings = re.findall(r"^## (.+?)\s*$", text, re.M)
    assert headings[:1] == ["Unreleased"], headings[:2]
    m = re.fullmatch(r"(\d+\.\d+\.\d+) \((\d{4}-\d{2}-\d{2})\)", headings[1])
    assert m, f"the newest released heading {headings[1]!r} is not 'X.Y.Z (YYYY-MM-DD)'"
    assert m[1] == __version__, (m[1], __version__)
    date.fromisoformat(m[2])
