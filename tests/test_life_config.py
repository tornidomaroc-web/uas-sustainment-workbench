"""Every interval and limit in fleet.toml cites its public civil source next to it."""

import re
import tomllib
from importlib import resources

from uas_workbench.fleet import load_config

CITATION = re.compile(r"https?://\S+")


def raw_rules() -> list[dict[str, object]]:
    text = resources.files("uas_workbench.fleet").joinpath("fleet.toml").read_text("utf-8")
    life = tomllib.loads(text)["life"]
    return [*life["component_kinds"], *life["inspections"]]


def test_every_rule_carries_a_public_source_and_at_least_one_limit() -> None:
    rules = raw_rules()
    assert len(rules) >= 4
    for rule in rules:
        assert CITATION.search(str(rule["source"])), rule
        limits = {k for k in ("hours", "cycles", "calendar_months") if k in rule}
        assert limits, rule
        if "tolerance_hours" in rule:
            assert "hours" in rule, "a tolerance without an hours limit has no meaning"


def test_policy_loads_with_the_same_names_as_the_file() -> None:
    policy = load_config().life
    assert 0 < policy.due_soon_fraction < 1
    names = {str(r.get("kind", r.get("name"))) for r in raw_rules()}
    assert names == set(policy.component_kinds) | set(policy.inspections)
    for rule in [*policy.component_kinds.values(), *policy.inspections.values()]:
        assert rule.source and rule.limits()
        assert rule.tolerance_hours == 0 or rule.hours is not None
