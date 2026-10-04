"""Every cue the documentation shows is exactly what the code gives today.

Twice an example cue was left behind when the code or the seed changed: the README's and then
the `life/cue.py` docstring's grounded-flight example kept a flight of the 0.4.0 seed after
0.5.0 had removed it. Each was correct wording for data that no longer existed. Two
properties, each tested on every example found:

  wording     the example is exactly what the cue grammar returns for the part, numbers, unit,
              direction, dates and state the example itself states; a change to the grammar's
              words, its rounding or its choice of form leaves a stale example and fails here
  provenance  the example is a cue the demo fleet, or the three hand-built aircraft that fly
              while grounded (`flown_grounded()`), actually produce at a scene plan time; a
              change to the seed or the rules that removes the case fails here

Where the examples are found, and which property each must keep:

  life/cue.py docstring  its grammar table, every cell. The table illustrates every form,
                         some no record exercises (an hour-limited part past its limit), so
                         every cell keeps the wording; a cell that states a date or a time
                         names a record, and keeps the provenance too.
  README.md              every quoted cue: wording and provenance, because the README tells a
                         reader what the tool says about the demo they can open.
  CHANGELOG.md           the Unreleased section, which describes the code as it is now, and
                         the newest released section, which describes the code as released,
                         so a release that renames Unreleased keeps its examples checked:
                         wording and provenance. An older released section is history: it may
                         quote what an earlier version or seed gave ("1 cycle past" where
                         0.5.0 says "reached", the 0.4.0 seed's flights), and holding it to
                         today's output would force history to be rewritten.

A quotation in a checked section is a claim about the code; history is told in prose, as 0.5.1
tells of "BAT-04A one cycle past" without quoting a cue. The text cannot show whether a quoted
cue that no longer holds is history or a stale example, so a section that must quote an old cue
whole names it in HISTORICAL, which holds it to being in that section and stale, so it can
neither hide a cue that holds nor outlive its text.

A checked section that yields no cue must say so: the newest released section's cue count is
pinned in RELEASED_CUES, so the next release, which makes a new section the newest, fails here
until its count is written down, 0 included. Unreleased has no count, since it often quotes no
cue; if its heading is there, the section must be found.

The plan times are fixed, never the wall clock: an example true only today would be stale
tomorrow. A cue is found in the README and the CHANGELOG by how it begins (a part id, an
inspection, an open work order, a not-known state, a grounded flight), so an example with new
wording is still found, and then fails.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, time, timedelta
from functools import cache
from pathlib import Path

import pytest
from test_cues import PLAN_TIMES
from test_grounded_flights import CONFIG, all_findings, flown_grounded

import uas_workbench.life.cue as cue_module
from uas_workbench.flight import Unknown
from uas_workbench.life import DueItem
from uas_workbench.life.cue import flight_cue, item_cue, reason_cues, status_cue, work_cue
from uas_workbench.life.engine import NO_RECORD
from uas_workbench.life.model import WorkOrder
from uas_workbench.service.app import due_view

ROOT = Path(__file__).resolve().parent.parent
# How a cue begins, whatever follows: wide on purpose, so an example in new words is found.
CUE_START = re.compile(
    r"^(?:[A-Z][A-Z0-9]*-[A-Z0-9-]+ |\d+ h insp|annual insp|AOG awaiting|in maintenance since"
    r"|deferred defect since|not known|flown )"
)
DATED = re.compile(r"\d{4}-\d{2}-\d{2}")
AS_OF = datetime(2026, 8, 1, tzinfo=UTC)  # any instant: only a calendar form reads it


# ---- where the examples are -------------------------------------------------------------


def docstring_examples() -> list[str]:
    """Every cell of the grammar table in the life/cue.py docstring."""
    doc = cue_module.__doc__
    assert doc is not None
    _, _, after = doc.partition("\nGrammar,")
    assert after, "the docstring has no grammar table"
    table = after.split("\n\n")[1]  # the table ends at the first blank line after it
    cells = []
    for line in table.splitlines():
        row = [c.strip() for c in re.split(r" {3,}", line.strip()) if c.strip()]
        assert 1 <= len(row) <= 2, line
        cells.extend(row)
    return cells


def quoted_cues(text: str) -> list[str]:
    """The cues quoted in a markdown text, in "double quotes" or `backticks`; a quotation may
    wrap across one line break."""
    found = []
    for m in re.finditer(r'"([^"\n]+(?:\n[^"\n]+)?)"|`([^`\n]+)`', text):
        quoted = " ".join((m.group(1) or m.group(2)).split())
        if CUE_START.match(quoted):
            found.append(quoted)
    return found


def readme_examples() -> list[str]:
    return quoted_cues((ROOT / "README.md").read_text(encoding="utf-8"))


# The newest released section and the number of cues it quotes. A release that makes a new
# section the newest adds it here, with its count, 0 if it quotes none.
RELEASED_CUES = {"0.5.1": 3, "0.5.2": 0}
# A checked section's whole quotation of a cue the code no longer gives, kept on purpose to say
# what changed: section heading -> the quoted cues. None today.
HISTORICAL: dict[str, frozenset[str]] = {}


def changelog_sections() -> list[tuple[str, str]]:
    """Every `## ` section of the CHANGELOG, newest first, as (heading, text)."""
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    return [
        (m[1].strip(), m[2])
        for m in re.finditer(r"^## ([^\n]+)\n(.*?)(?=^## |\Z)", text, re.S | re.M)
    ]


def checked_sections() -> list[tuple[str, str]]:
    """The Unreleased section if there is one, and the newest released section."""
    sections = changelog_sections()
    unreleased = [s for s in sections[:1] if s[0] == "Unreleased"]
    released = [s for s in sections if s[0] != "Unreleased"]
    return unreleased + released[:1]


def section_examples(heading: str, text: str, historical: dict[str, frozenset[str]]) -> list[str]:
    """The cues quoted in one section, less the ones it names as history."""
    return [c for c in quoted_cues(text) if c not in historical.get(heading, frozenset())]


def changelog_examples() -> list[tuple[str, str]]:
    """(heading, cue) for every cue quoted in a checked section, less the historical ones."""
    return [
        (heading, c)
        for heading, text in checked_sections()
        for c in section_examples(heading, text, HISTORICAL)
    ]


def test_the_examples_are_found() -> None:
    """The extraction itself: if it found nothing, every test below would pass vacuously."""
    cells = docstring_examples()
    assert len(cells) >= 20 and any(c.startswith("flown ") for c in cells), cells
    assert len(readme_examples()) >= 5, readme_examples()
    sections = changelog_sections()
    headings = [h for h, _ in sections]
    assert "Unreleased" not in headings[1:], "Unreleased is not the first CHANGELOG section"
    released = [(h, t) for h, t in sections if h != "Unreleased"]
    assert released, "the CHANGELOG has no released section"
    newest, section = released[0]
    assert newest in RELEASED_CUES, (
        f"{newest} is the newest released section: add it to RELEASED_CUES with the number of "
        "cues it quotes, 0 if none"
    )
    found = section_examples(newest, section, HISTORICAL)
    assert len(found) == RELEASED_CUES[newest], (newest, found)
    text = '"PROP-01 1 h left", `annual insp due by 2027-04-30`, "AOG\nawaiting parts since'
    assert quoted_cues(text + ' 2026-08-14" and "AOG" alone') == [
        "PROP-01 1 h left",
        "annual insp due by 2027-04-30",
        "AOG awaiting parts since 2026-08-14",  # wrapped across a line break
    ]


# ---- wording: the grammar, given what the example states --------------------------------


def _item(
    cid: str | None,
    name: str | None,
    basis: str,
    remaining: float | Unknown,
    limit: float,
    state: str,
) -> DueItem:
    if name is not None:  # "100 h insp" -> "the 100-hour inspection"
        hours = name.removesuffix(" h insp")
        subject = (
            f"the {hours}-hour inspection"
            if hours != name
            else f"the {name.removesuffix(' insp')} inspection"
        )
    else:
        subject = f"component {cid}"
    unit = {"hours": "h", "cycles": "cycles", "calendar": "days"}[basis]
    return DueItem(
        aircraft_key="EX-01",
        subject=subject,
        component_id=cid,
        basis=basis,  # type: ignore[arg-type]
        unit=unit,  # type: ignore[arg-type]
        used=Unknown("example") if isinstance(remaining, Unknown) else 0.0,
        limit=limit,
        remaining=remaining,
        tolerance=0.0,
        state=state,  # type: ignore[arg-type]
        source="example",
        message="",
    )


def _day(text: str) -> datetime:
    return datetime.combine(date.fromisoformat(text), time(), UTC)


ID = r"(?P<id>[A-Z][A-Z0-9]*-[A-Z0-9-]+)"
NAME = r"(?P<name>\d+ h insp|annual insp)"
N = r"(?P<n>\d+(?:\.\d+)?)"
LIM = r"(?P<lim>\d+)"
D = r"(?P<d>\d{4}-\d{2}-\d{2})"


def _parts_cue(text: str) -> str | None:
    """What the grammar gives for a part, a number, a direction and a limit as `text` states
    them; None if `text` states no part-limit form the grammar knows."""
    forms: list[tuple[str, str, str]] = [
        (rf"{ID} {N} h left of {LIM} h", "hours", "left"),
        (rf"{ID} {N} h past {LIM} h limit", "hours", "past"),
        (rf"{ID} reached {LIM} h limit", "hours", "reached"),
        (rf"{ID} {N} cycles? left of {LIM}", "cycles", "left"),
        (rf"{ID} {N} cycles? past {LIM}-cycle limit", "cycles", "past"),
        (rf"{ID} reached {LIM}-cycle limit", "cycles", "reached"),
        # The shorter forms, for an id that would push the longer past the limit; a limit
        # the form does not state is any limit, and with a short id the longer form wins.
        (rf"{ID} {N} h left", "hours", "left"),
        (rf"{ID} {N} h past limit", "hours", "past"),
        (rf"{ID} reached {LIM} h", "hours", "reached"),
        (rf"{ID} {N} cycles? left", "cycles", "left"),
        (rf"{ID} {N} cycles? past limit", "cycles", "past"),
        (rf"{ID} reached {LIM} cycles", "cycles", "reached"),
    ]
    for pattern, basis, way in forms:
        if m := re.fullmatch(pattern, text):
            n = float(m["n"]) if way != "reached" else 0.0
            rem = n if way == "left" else -n
            state = "ok" if way == "left" else "overdue"
            limit = float(m["lim"]) if "lim" in m.groupdict() else 300.0
            return item_cue(_item(m["id"], None, basis, rem, limit, state), AS_OF)
    if m := re.fullmatch(rf"{ID} {N} days? left, due {D}", text):
        n = float(m["n"])
        at = _day(m["d"]) - timedelta(days=n)
        return item_cue(_item(m["id"], None, "calendar", n, 730.0, "ok"), at)
    if m := re.fullmatch(rf"{ID} past calendar limit {D}", text):
        at = _day(m["d"]) + timedelta(days=1)
        return item_cue(_item(m["id"], None, "calendar", -1.0, 730.0, "overdue"), at)
    if m := re.fullmatch(rf"{ID} {N} days? left", text):
        return item_cue(_item(m["id"], None, "calendar", float(m["n"]), 730.0, "ok"), AS_OF)
    if m := re.fullmatch(rf"{ID} past calendar limit", text):
        return item_cue(_item(m["id"], None, "calendar", -1.0, 730.0, "overdue"), AS_OF)
    return None


def _inspection_cue(text: str) -> str | None:
    if m := re.fullmatch(rf"{NAME} {N} h overdue, in tolerance", text):
        item = _item(None, m["name"], "hours", -float(m["n"]), 100.0, "overdue_within_tolerance")
        return item_cue(item, AS_OF)
    if m := re.fullmatch(rf"{NAME} {N} h in tolerance", text):
        item = _item(None, m["name"], "hours", -float(m["n"]), 100.0, "overdue_within_tolerance")
        return item_cue(item, AS_OF)
    if m := re.fullmatch(rf"{NAME} {N} h overdue", text):
        return item_cue(_item(None, m["name"], "hours", -float(m["n"]), 100.0, "overdue"), AS_OF)
    if m := re.fullmatch(rf"{NAME} due in {N} h", text):
        return item_cue(_item(None, m["name"], "hours", float(m["n"]), 100.0, "ok"), AS_OF)
    if m := re.fullmatch(rf"{NAME} due by {D}", text):
        at = _day(m["d"]) - timedelta(days=30)
        return item_cue(_item(None, m["name"], "calendar", 30.0, 12.0, "ok"), at)
    if m := re.fullmatch(rf"{NAME} {N} days? overdue", text):
        return item_cue(_item(None, m["name"], "calendar", -float(m["n"]), 12.0, "overdue"), AS_OF)
    if m := re.fullmatch(rf"{NAME}(?:: time in service)? not known", text):
        unknown = Unknown("time in service not known")
        return item_cue(_item(None, m["name"], "hours", unknown, 100.0, "unknown"), AS_OF)
    return None


def _board_cue(text: str) -> str | None:
    works = {"AOG awaiting parts": "awaiting_parts", "in maintenance": "in_work"}
    works["deferred defect"] = "deferred"
    if m := re.fullmatch(
        rf"(?P<w>AOG awaiting parts|in maintenance|deferred defect) since {D}", text
    ):
        return work_cue(WorkOrder(_day(m["d"]), "example", works[m["w"]], True))  # type: ignore[arg-type]
    if text == "not known: no maintenance record":
        return status_cue(Unknown(NO_RECORD))
    if text == "not known: time in service":
        return status_cue(Unknown("time in service not known"))
    if m := re.fullmatch(
        r"flown (?P<s>unserviceable|in maintenance|AOG) (?P<t>[\d-]+ \d\d:\d\d)", text
    ):
        return flight_cue(m["s"], datetime.strptime(m["t"], "%Y-%m-%d %H:%M").replace(tzinfo=UTC))
    return None


def grammar_gives(text: str) -> str | None:
    """The cue the code gives for what `text` states, or None if `text` states nothing the
    cue grammar has a form for."""
    return _parts_cue(text) or _inspection_cue(text) or _board_cue(text)


# ---- provenance: what the demo and the hand-built aircraft actually produce -------------


@cache
def produced() -> frozenset[str]:
    """Every cue the demo fleet and the three hand-built aircraft produce at every scene plan
    time: each due item's, each reason's, each board state's, and each grounded flight's with
    its cause cues."""
    store = flown_grounded()
    out: set[str] = set()
    for aircraft in store.aircraft():
        for stamp in PLAN_TIMES:
            t = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            due, state, _ = due_view(store, aircraft, CONFIG, t)
            out.update(item_cue(i, t) for i in due.items)
            out.update(reason_cues(due, state))
            out.add(status_cue(state.status))
    for finding in all_findings(store):
        out.add(finding.cue)
        out.update(finding.cause_cues)
    return frozenset(out)


# ---- the tests --------------------------------------------------------------------------

WORDING = [
    *(pytest.param(c, id=f"cue.py: {c}") for c in docstring_examples()),
    *(pytest.param(c, id=f"README: {c}") for c in readme_examples()),
    *(pytest.param(c, id=f"CHANGELOG {h}: {c}") for h, c in changelog_examples()),
]
PROVENANCE = [
    *(pytest.param(c, id=f"cue.py: {c}") for c in docstring_examples() if DATED.search(c)),
    *(pytest.param(c, id=f"README: {c}") for c in readme_examples()),
    *(pytest.param(c, id=f"CHANGELOG {h}: {c}") for h, c in changelog_examples()),
]


@pytest.mark.parametrize("example", WORDING)
def test_a_documented_cue_is_what_the_grammar_gives(example: str) -> None:
    gives = grammar_gives(example)
    assert gives is not None, f"{example!r} states no form the cue grammar has"
    assert gives == example, f"the documentation says {example!r}; the code says {gives!r}"


@pytest.mark.parametrize("example", PROVENANCE)
def test_a_documented_cue_is_one_the_demo_produces(example: str) -> None:
    assert example in produced(), (
        f"{example!r} is not produced by the demo fleet or the hand-built aircraft at any "
        "scene plan time"
    )


def test_the_wording_check_refuses_a_stale_example() -> None:
    """A stale example of each kind is refused: a word the grammar no longer uses, a rounding
    it no longer gives, a form it would not choose, and a record that does not exist."""
    assert grammar_gives("BAT-04A 1 cycles past 300-cycle limit") == (
        "BAT-04A 1 cycle past 300-cycle limit"
    )
    assert grammar_gives("PROP-01 102.90 h left of 300 h") == "PROP-01 102.9 h left of 300 h"
    assert grammar_gives("PROP-01 102.9 h left") == "PROP-01 102.9 h left of 300 h"  # too short
    assert grammar_gives("PROP-01 102.9 h remaining of 300 h") is None
    assert grammar_gives("100 h insp 12 h overdue") == "100 h insp 12.0 h overdue"
    assert "flown unserviceable 2026-08-06 12:09" not in produced()  # the 0.4.0 seed's flight


def test_a_historical_quote_is_stale_and_in_its_section() -> None:
    """HISTORICAL exempts only a quotation that is there and no longer holds, in a checked
    section; and the exemption itself drops that quotation and nothing else."""
    checked = dict(checked_sections())
    for heading, cues in HISTORICAL.items():
        assert heading in checked, f"{heading} is not a checked section; HISTORICAL is not needed"
        for c in cues:
            assert c in quoted_cues(checked[heading]), f"{c!r} is not quoted in {heading}"
            assert grammar_gives(c) != c or c not in produced(), f"{c!r} still holds; check it"
    text = "`BAT-04A 1 cycles past 300-cycle limit` became `BAT-04A reached 300-cycle limit`"
    old = frozenset({"BAT-04A 1 cycles past 300-cycle limit"})
    assert section_examples("9.9.9", text, {"9.9.9": old}) == ["BAT-04A reached 300-cycle limit"]
    assert section_examples("9.9.8", text, {"9.9.9": old}) == quoted_cues(text)
