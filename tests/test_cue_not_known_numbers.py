"""The cue checker on not-known items: a number in the name is the name's, and a not-known cue
carries no number of its own (the check of 95e1c43 at life/cue.py:243).

A. An inspection whose name holds a number ("24-month inspection", "2-year inspection") with
   no completion on record: the engine's own cue, "24-month insp: none recorded, not known",
   was refused for a missing unit, because the 24 of the name was read as a count.
B. A not-known item has no count to give. "annual insp: 12 days not known" passed, the 12 taken
   from the sentence's "12-calendar-month", and "annual insp: 12 months, not known" was
   refused only for its unit. Both are now refused for carrying a number.
"""

from __future__ import annotations

from datetime import timedelta

from test_life_rules import AS_OF, T0, compute, flight, item, record

from uas_workbench.assistant.tools import BY_NAME
from uas_workbench.life import InspectionDone, LifePolicy, LifeRule, board, due_list
from uas_workbench.life.cue import item_cue, problems, reason_cues

D = timedelta(days=1)
H = 3600.0
NAMED = LifePolicy(
    due_soon_fraction=0.10,
    component_kinds={},
    inspections={
        "24-month inspection": LifeRule("24-month inspection", calendar_months=24, source="t"),
        "2-year inspection": LifeRule("2-year inspection", calendar_months=24, source="t"),
    },
)


def named(name: str):  # type: ignore[no-untyped-def]
    due = due_list(
        "A",
        maintenance=record("A", inspections=()),
        components=(),
        flights_of=lambda key: (),
        policy=NAMED,
        as_of=AS_OF,
    )
    return due, item(due, f"the {name}", "calendar")


# A. The engine's own cue for a not-known item whose name holds a number passes its checker.
def test_a_number_in_the_name_is_not_a_count() -> None:
    shorts = {"24-month inspection": "24-month insp", "2-year inspection": "2-year insp"}
    for name, short in shorts.items():
        due, it = named(name)
        assert it.state == "unknown"
        assert it.message == (
            f"the {name} of aircraft A cannot be counted on 2026-04-01: no completed {name} is "
            "recorded, so its 24-calendar-month interval has no date to count from"
        )
        cue = item_cue(it, due.as_of)
        assert cue == f"{short}: none recorded, not known"
        assert problems(cue, it, due.as_of) == []
    assert reason_cues(due, board(due)) == (
        "24-month insp: none recorded, not known",
        "2-year insp: none recorded, not known",
    )
    # The name excuses its own number once, never a second one.
    due, it = named("24-month inspection")
    assert "a not-known item's cue carries no number: 24" in problems(
        "24-month insp: 24 days, not known", it, due.as_of
    )


# B. A not-known cue that carries a number is refused for it, with or without a unit.
def test_a_not_known_cue_carries_no_number() -> None:
    rec = record("A", inspections=(InspectionDone("100-hour inspection", T0 - D, 0.0),))
    due = compute("A", rec)
    annual = item(due, "the annual inspection", "calendar")
    assert annual.state == "unknown"
    assert problems(item_cue(annual, due.as_of), annual, due.as_of) == []
    for cue in ("annual insp: 12 days not known", "annual insp: 12 months, not known"):
        assert "a not-known item's cue carries no number: 12" in problems(cue, annual, due.as_of)
    # A not-known hours item: the 100 of its name is the name's, and its cue still passes.
    unknown = compute("A", record("A", time_in_service_before_s=50.0 * H, inspections=()),
                      flights={"A": [flight("A", 1, T0, 1800.0)]}, as_of=T0 - D)  # fmt: skip
    hundred = item(unknown, "the 100-hour inspection", "hours")
    cue = item_cue(hundred, unknown.as_of)
    assert cue == "100 h insp: time in service not known"
    assert problems(cue, hundred, unknown.as_of) == []
    assert "a not-known item's cue carries no number: 50.0" in problems(
        "100 h insp: 50.0 h before, not known", hundred, unknown.as_of
    )


# The assistant's tool says when a calendar inspection is not known: at that date.
def test_the_due_tool_says_at_that_date_for_a_calendar_inspection_too() -> None:
    assert (
        "unknown when the time in service at that date is not known or a calendar inspection "
        "has no completion on record to count from at that date"
    ) in BY_NAME["aircraft_due"].description
