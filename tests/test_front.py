"""The public front: the top of the README and the opening card of the demo page.

They are what a first-time reader sees, so what they claim is tied here to the code that
makes it true: the records summary's title and its four statuses as the code spells them,
the release they name, in words and in the tag to check out, as the package's version and the
release under way, every `uasw` command they show as one the
parser accepts, every relative link as a file in the tree, and every web address as one the
repository already carries. Neither uses a dash longer than a hyphen, or a word the records
summary never uses about itself. The README's command lines are typed into whatever shell a
reader has, so none chains two commands, each shell has its line to activate the environment,
the command the launcher stands for is named, and a log of the reader's own goes to a store
apart from the demo fleet.
"""

from __future__ import annotations

import html
import re
import shlex
import tomllib
from collections.abc import Callable
from importlib import resources
from pathlib import Path

import pytest
from test_version import RELEASE

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
FENCE = re.compile(r"^```[^\n]*\n(.*?)^```", re.S | re.M)
# The line that activates the environment in each shell a reader may type the front into.
ACTIVATE = {
    "bash or zsh outside Windows": ". .venv/bin/activate",
    "bash on Windows": ". .venv/Scripts/activate",
    "cmd": r".venv\Scripts\activate.bat",
    "PowerShell": r".venv\Scripts\Activate.ps1",
}


def readme_front() -> str:
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert DETAIL_HEADING in text
    return text.split(DETAIL_HEADING, 1)[0]


def front_lines() -> list[str]:
    """Every command line of the front's fenced blocks, its trailing comment cut."""
    return [
        line.split("  #", 1)[0].strip()
        for block in FENCE.findall(readme_front())
        for line in block.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


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
    """The release the front names in words and the tag it says to check out are one release:
    the version the code reports and the release `test_version` holds it to, so a release
    that moves the version and leaves the front naming the one before fails here."""
    front = readme_front()
    for release in {__version__, RELEASE}:
        assert f"The latest release is {release}." in front, release
        assert f"`git checkout v{release}`" in front, release


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


def test_no_line_of_the_front_chains_two_commands() -> None:
    """Windows PowerShell 5.1 cannot parse `&&` or `||` between two commands, so a front line
    that chains them stops a reader there; each command goes on a line of its own."""
    lines = front_lines()
    assert lines
    for line in lines:
        assert "&&" not in line and "||" not in line, line


def test_the_front_activates_the_environment_in_each_shell() -> None:
    lines = front_lines()
    for shell, line in ACTIVATE.items():
        assert line in lines, shell


def test_the_front_names_the_command_the_launcher_stands_for() -> None:
    """Where a launcher is not found or is blocked, the module it runs is the same command."""
    scripts = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))["project"]["scripts"]
    module = scripts["uasw"].removesuffix(":main")
    assert f"`python -m {module}`" in readme_front()


def test_your_own_log_goes_to_a_store_apart_from_the_demo_fleet() -> None:
    """`uasw seed`, and `uasw serve` on an empty store, fill the store with the demo fleet, so
    an `ingest` the front shows names a store of its own with `--db`, and a command of the
    front reads that store back."""
    parsed = [
        build_parser().parse_args(shlex.split(line)[1:])
        for line in front_lines()
        if line.startswith("uasw ")
    ]
    demo = {a.db for a in parsed if a.command == "seed"}
    own = {a.db for a in parsed if a.command == "ingest"}
    assert demo and own, (demo, own)
    assert not own & demo, own & demo
    assert any(a.db in own for a in parsed if a.command in {"records", "serve"})
