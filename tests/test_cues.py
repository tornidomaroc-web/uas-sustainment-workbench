"""A short cue beside every sentence the engine emits, for a board that cannot carry paragraphs.

Every due item and every board reason keeps its full sentence exactly as before and gains a
cue of at most 40 characters, generated from the same data. The properties under test:

  length       every cue the seeded fleet produces, at every scene plan time and at now, is
               at or under the limit
  consistency  a cue names the same part or inspection, the same numbers, the same unit and
               the same state as its sentence, never stronger and never weaker; a not-known
               item's cue says not known
  parallel     the board's reason cues stand one for one beside its reasons
  grounding    a cue adds no number, id, date or name absent from the records: the assistant's
               own grounding check verifies every cue against the item it belongs to
  opt-in       the GET responses are byte for byte what they were unless `cues=true` is asked,
               so the recorded assistant runs and the evidence pack are untouched
  mutation     a cue that breaks each property is refused by the same checker the tests use
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from uas_workbench.assistant.agent import Call, ground
from uas_workbench.fleet import load_config
from uas_workbench.fleet.showcase import showcase
from uas_workbench.fleet.synthetic import generate
from uas_workbench.life import Board, DueItem, DueList
from uas_workbench.life.cue import CUE_LIMIT, item_cue, problems, reason_cues
from uas_workbench.service.app import create_app, due_view
from uas_workbench.service.store import Store

FIXTURES = Path(__file__).parent / "fixtures"
# The scene's plan times: one before every dated log, the minute on each side of every board
# change, and one after them all (the scene project's fixture plan at service 0.3.0).
PLAN_TIMES = [
    "2026-08-03T00:00:00Z", "2026-08-04T07:59:00Z", "2026-08-04T08:00:00Z", "2026-08-05T07:59:00Z",
    "2026-08-05T08:00:00Z", "2026-08-06T11:07:00Z", "2026-08-06T11:08:00Z", "2026-08-07T08:27:00Z",
    "2026-08-07T08:28:00Z", "2026-08-08T13:59:00Z", "2026-08-08T14:00:00Z", "2026-08-10T13:59:00Z",
    "2026-08-10T14:00:00Z", "2026-08-11T08:59:00Z", "2026-08-11T09:00:00Z", "2026-08-13T07:59:00Z",
    "2026-08-13T08:00:00Z", "2026-08-13T08:59:00Z", "2026-08-13T09:00:00Z", "2026-08-14T20:34:00Z",
    "2026-08-14T20:35:00Z", "2026-08-31T23:59:00Z", "2026-09-01T00:00:00Z", "2026-10-01T00:00:00Z",
]  # fmt: skip


def _stamp(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


@pytest.fixture(scope="module")
def store() -> Store:
    s = Store(":memory:")
    s.add_fleet(generate(load_config()))
    s.add_fleet(showcase(FIXTURES))
    return s


@pytest.fixture(scope="module")
def client(store: Store) -> Iterator[TestClient]:
    with TestClient(create_app(store)) as c:
        yield c


def every_due(store: Store) -> list[tuple[str, datetime, DueList, Board]]:
    """The due list and board of every aircraft at every plan time and at now."""
    cfg = load_config()
    out = []
    for a in store.aircraft():
        for t in [*(_stamp(s) for s in PLAN_TIMES), datetime.now(UTC)]:
            due, state, _ = due_view(store, a, cfg, t)
            out.append((a.key, t, due, state))
    return out


# ---- length -----------------------------------------------------------------------------


def test_every_item_cue_is_at_or_under_the_limit(store: Store) -> None:
    seen = 0
    for key, t, due, _ in every_due(store):
        for item in due.items:
            cue = item_cue(item, t)
            assert cue and len(cue) <= CUE_LIMIT, (key, t, cue, len(cue))
            seen += 1
    assert seen > 500


def test_every_reason_cue_is_at_or_under_the_limit(store: Store) -> None:
    seen = 0
    for key, t, due, state in every_due(store):
        for cue in reason_cues(due, state):
            assert cue and len(cue) <= CUE_LIMIT, (key, t, cue, len(cue))
            seen += 1
    assert seen > 50


# ---- consistency ------------------------------------------------------------------------


def test_every_item_cue_is_consistent_with_its_sentence(store: Store) -> None:
    for key, t, due, _ in every_due(store):
        for item in due.items:
            assert problems(item_cue(item, t), item, t) == [], (key, t, item.message)


def test_not_known_items_say_not_known(store: Store) -> None:
    seen = 0
    for _, t, due, _ in every_due(store):
        for item in due.items:
            if item.state == "unknown":
                cue = item_cue(item, t)
                assert "not known" in cue, cue
                for word in ("left", "due in", "due by", "overdue", "past", "ok"):
                    assert word not in cue, cue
                seen += 1
    assert seen > 0


def test_reason_cues_stand_beside_the_reasons(store: Store) -> None:
    labels = (
        "AOG",
        "in maintenance",
        "unserviceable",
        "serviceable with deferred defects",
        "not known",
    )
    seen = dict.fromkeys(labels, 0)
    for key, t, due, state in every_due(store):
        cues = reason_cues(due, state)
        assert len(cues) == len(state.reasons), (key, t)
        label = state.status if isinstance(state.status, str) else "not known"
        if label in seen:
            seen[label] += 1
        for reason, cue in zip(state.reasons, cues, strict=True):
            if "awaiting parts" in reason:
                assert cue.startswith("AOG awaiting parts since 20"), cue
            elif "is in maintenance since" in reason:
                assert cue.startswith("in maintenance since 20"), cue
            elif "deferred defect" in reason:
                assert cue.startswith("deferred defect since 20"), cue
            elif "cannot be measured" in reason:
                assert "not known" in cue, cue
            else:
                # A due item's sentence: its cue is the item's cue.
                item = next(i for i in due.items if i.message == reason)
                assert cue == item_cue(item, t)
    assert all(n > 0 for n in seen.values()), seen


def test_a_not_known_board_has_a_cue_that_says_so(store: Store) -> None:
    for key, t, due, state in every_due(store):
        if not isinstance(state.status, str):
            cues = reason_cues(due, state)
            if not due.has_record:
                assert cues == (), (key, t)  # no reasons, as before; the status cue says it
            else:
                assert any("not known" in c for c in cues), (key, t, cues)


# ---- grounding --------------------------------------------------------------------------


def _record(item: DueItem, aircraft_key: str) -> Call:
    """The item as the assistant would have fetched it, without any cue."""
    data = {k: v for k, v in dataclasses.asdict(item).items() if k != "cue"}
    return Call("aircraft_due", {"key": aircraft_key}, f"/aircraft/{aircraft_key}/due", {}, data)


def test_every_item_cue_passes_the_assistant_grounding_check_against_its_item(store: Store) -> None:
    for key, t, due, _ in every_due(store):
        for item in due.items:
            cue = item_cue(item, t)
            grounding = ground(cue, (_record(item, key),))
            assert grounding.verified, (cue, grounding.unsupported)


def test_every_reason_cue_passes_the_grounding_check_against_the_due_list(store: Store) -> None:
    for key, _t, due, state in every_due(store):
        result = {
            "aircraft_key": key,
            "status": state.status
            if isinstance(state.status, str)
            else {"unknown": state.status.reason},
            "status_reasons": list(state.reasons),
            "items": [
                {k: v for k, v in dataclasses.asdict(i).items() if k != "cue"} for i in due.items
            ],
            "work_orders": [dataclasses.asdict(w) for w in due.work_orders],
        }
        call = Call(
            "aircraft_due",
            {"key": key},
            f"/aircraft/{key}/due",
            {},
            json.loads(json.dumps(result, default=str)),
        )
        for cue in reason_cues(due, state):
            grounding = ground(cue, (call,))
            assert grounding.verified, (cue, grounding.unsupported)


# ---- opt-in on the service ---------------------------------------------------------------


@pytest.mark.parametrize(
    "path", ["/aircraft", "/aircraft/SYN-04", "/aircraft/SYN-04/due", "/fleet/due"]
)
def test_responses_are_byte_identical_unless_cues_are_asked_for(
    client: TestClient, path: str
) -> None:
    plain = client.get(path, params={"as_of": "2026-10-01T00:00:00Z"})
    again = client.get(path, params={"as_of": "2026-10-01T00:00:00Z", "cues": "false"})
    cued = client.get(path, params={"as_of": "2026-10-01T00:00:00Z", "cues": "true"})
    assert plain.status_code == cued.status_code == 200
    assert plain.content == again.content
    assert b'"cue"' not in plain.content and b'"status_cues"' not in plain.content
    assert plain.content != cued.content


def test_cued_due_list_carries_a_cue_per_item_and_per_reason(client: TestClient) -> None:
    due = client.get(
        "/aircraft/SYN-04/due", params={"as_of": "2026-10-01T00:00:00Z", "cues": "true"}
    ).json()
    assert len(due["status_cues"]) == len(due["status_reasons"]) >= 1
    for item in due["items"]:
        assert item["cue"] and len(item["cue"]) <= CUE_LIMIT
        assert item["message"]  # the sentence stays
    plain = client.get("/aircraft/SYN-04/due", params={"as_of": "2026-10-01T00:00:00Z"}).json()
    stripped = {k: v for k, v in due.items() if k not in ("status_cue", "status_cues")}
    stripped["items"] = [{k: v for k, v in i.items() if k != "cue"} for i in due["items"]]
    assert plain == stripped


def test_cued_board_and_fleet_due(client: TestClient) -> None:
    rows = client.get("/aircraft", params={"as_of": "2026-10-01T00:00:00Z", "cues": "true"}).json()
    by_key = {r["key"]: r for r in rows}
    assert by_key["SYN-07"]["status_cues"][0].startswith("AOG awaiting parts since 2026-08-14")
    assert by_key["SYN-06"]["status_cue"] == "not known: no maintenance record"
    assert by_key["SYN-01"]["status_cue"] == "serviceable"
    items = client.get(
        "/fleet/due", params={"as_of": "2026-10-01T00:00:00Z", "cues": "true"}
    ).json()
    assert items and all(len(i["cue"]) <= CUE_LIMIT for i in items)


def test_the_evidence_pack_and_openapi_are_unchanged_by_the_option(client: TestClient) -> None:
    pack = client.get("/aircraft/SYN-04/evidence", params={"as_of": "2026-10-01T00:00:00Z"}).json()
    assert all("cue" not in i for i in pack["programme"]["items"])
    assert "status_cues" not in pack["programme"]
    schema = client.get("/openapi.json").json()
    assert "cues" in json.dumps(schema["paths"]["/aircraft/{key}/due"]["get"]["parameters"])


# ---- mutation: the checker refuses each broken property ----------------------------------


def _one(store: Store, state: str, component: bool | None = None) -> tuple[DueItem, datetime]:
    for _, t, due, _ in every_due(store):
        for item in due.items:
            if item.state == state and (
                component is None or (item.component_id is not None) == component
            ):
                return item, t
    raise LookupError(state)


def test_the_checker_refuses_a_cue_that_is_too_long(store: Store) -> None:
    item, t = _one(store, "ok")
    assert any(
        "longer than" in p
        for p in problems(item_cue(item, t) + " and then some more words", item, t)
    )


def test_the_checker_refuses_a_cue_naming_another_part(store: Store) -> None:
    item, t = _one(store, "ok", component=True)
    assert item.component_id
    other = item_cue(item, t).replace(item.component_id, "BAT-99Z")
    assert any("does not name" in p for p in problems(other, item, t))


def test_the_checker_refuses_a_cue_with_another_number(store: Store) -> None:
    item, t = _one(store, "due_soon")
    cue = item_cue(item, t)
    digits = next(tok for tok in cue.split() if tok[0].isdigit())
    assert any("number" in p for p in problems(cue.replace(digits, "7777", 1), item, t))


def test_the_checker_refuses_a_cue_with_another_unit(store: Store) -> None:
    item, t = _one(store, "ok")
    cue = item_cue(item, t)
    swapped = {"h": "cycles", "cycles": "h", "days": "h"}[item.unit]
    mutated = (
        cue.replace(f" {item.unit}", f" {swapped}")
        if f" {item.unit}" in cue
        else cue + f" {swapped}"
    )
    assert any("unit" in p for p in problems(mutated, item, t))


def test_the_checker_refuses_a_cue_stronger_or_weaker_than_its_state(store: Store) -> None:
    within, t = _one(store, "overdue_within_tolerance")
    cue = item_cue(within, t)
    assert any("state" in p for p in problems(cue.replace(" in tolerance", ""), within, t))
    overdue, t2 = _one(store, "overdue")
    weaker = item_cue(overdue, t2).replace("past", "left of").replace("overdue", "due in")
    assert any("state" in p for p in problems(weaker, overdue, t2))
    unknown, t3 = _one(store, "unknown")
    assert any("not known" in p for p in problems("100 h insp due in 5.0 h", unknown, t3))


def test_the_checker_refuses_a_cue_that_says_serviceable_for_a_not_known_item(store: Store) -> None:
    unknown, t = _one(store, "unknown")
    assert any("not known" in p for p in problems("100 h insp serviceable", unknown, t))
