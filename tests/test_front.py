"""The public front: the top of the README and the opening card of the demo page.

They are what a first-time reader sees, so what they claim is tied here to the code that
makes it true: the records summary's title and its four statuses as the code spells them,
the release they name as the package's version, every `uasw` command they show as one the
parser accepts, every relative link as a file in the tree, and every web address as one the
repository already carries. Neither uses a dash longer than a hyphen, or a word the records
summary never uses about itself.
"""

from __future__ import annotations

import html
import re
import shlex
from collections.abc import Callable
from importlib import resources
from pathlib import Path

import pytest

from uas_workbench import __version__
from uas_workbench.evidence.records import RECORDS_STATUSES, RECORDS_TITLE
from uas_workbench.service.cli import build_parser

ROOT = Path(__file__).resolve().parents[1]
# The README's front ends where its detailed account of each part begins.
DETAIL_HEADING = "\n## What exists today\n"
ABOUT = re.compile(r'<section class="card" id="about">(.*?)</section>', re.S)
# Every web address the front may carry: the CI badge and workflow, the live demo and the
# repository (all in the README or the page before the front was written), the local API
# reference the README's run section names, and the posted check on pull request #110.
KNOWN_URLS = {
    "https://github.com/tornidomaroc-web/uas-sustainment-workbench/actions/workflows/ci.yml/badge.svg",
    "https://github.com/tornidomaroc-web/uas-sustainment-workbench/actions/workflows/ci.yml",
    "https://tornidomaroc-web.github.io/uas-sustainment-workbench/",
    "https://github.com/tornidomaroc-web/uas-sustainment-workbench",
    "https://github.com/tornidomaroc-web/uas-sustainment-workbench/pull/110#issuecomment-6256552924",
    "http://localhost:8000/docs",
}
URL = re.compile(r"https?://[^\s)<>\"'`]+")
OVERCLAIM = re.compile(
    r"\b(compliant|complies|compliance|meets|approved|certified|ready for submission)\b", re.I
)
LONG_DASH = re.compile(f"[{chr(0x2013)}{chr(0x2014)}]")  # the en and the em dash


def readme_front() -> str:
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert DETAIL_HEADING in text
    return text.split(DETAIL_HEADING, 1)[0]


def page_front() -> str:
    page = resources.files("uas_workbench.service").joinpath("site/index.html")
    found = ABOUT.search(page.read_text("utf-8"))
    assert found is not None, "the demo page has no opening card"
    return found.group(1)


def page_front_text() -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", page_front()))


def test_the_records_summary_is_named_as_the_code_names_it() -> None:
    readme = readme_front()
    assert f'"{RECORDS_TITLE}"' in readme
    assert f'"{RECORDS_TITLE}"' in page_front_text()
    for status in RECORDS_STATUSES:
        assert status in readme, status


def test_the_release_named_is_the_package_version() -> None:
    assert f"The latest release is {__version__}." in readme_front()


def test_every_uasw_command_shown_is_one_the_parser_accepts() -> None:
    lines = [
        line.split("  #", 1)[0].strip()
        for line in readme_front().splitlines()
        if line.startswith("uasw ")
    ]
    assert len(lines) >= 3
    for line in lines:
        try:
            build_parser().parse_args(shlex.split(line)[1:])
        except SystemExit:
            pytest.fail(f"the parser refuses {line!r}")


def test_every_relative_link_is_a_file_in_the_tree() -> None:
    links = re.findall(r"\]\(([^)]+)\)", readme_front())
    relative = [link for link in links if not URL.match(link)]
    assert relative
    for link in relative:
        assert (ROOT / link).is_file(), link


def test_every_web_address_is_one_the_repository_already_carries() -> None:
    found = set(URL.findall(readme_front())) | set(URL.findall(page_front()))
    assert found
    assert found <= KNOWN_URLS, found - KNOWN_URLS


@pytest.mark.parametrize("front", [readme_front, page_front_text], ids=["README", "page"])
def test_no_long_dash_and_no_overclaim(front: Callable[[], str]) -> None:
    text = front()
    assert LONG_DASH.search(text) is None
    assert OVERCLAIM.search(text) is None, OVERCLAIM.search(text)
