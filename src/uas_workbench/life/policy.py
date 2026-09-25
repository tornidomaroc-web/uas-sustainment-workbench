"""Life limits and inspection intervals as read from fleet.toml, each with its source."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .model import Basis


@dataclass(frozen=True)
class LifeRule:
    """Limits for one component kind or one inspection: whichever basis comes first applies."""

    name: str
    hours: float | None = None
    cycles: int | None = None
    calendar_months: int | None = None
    tolerance_hours: float = 0.0  # 14 CFR 91.409(b) style overflight allowance, hours only
    source: str = ""

    def limits(self) -> tuple[tuple[Basis, float], ...]:
        out: list[tuple[Basis, float]] = []
        if self.hours is not None:
            out.append(("hours", float(self.hours)))
        if self.cycles is not None:
            out.append(("cycles", float(self.cycles)))
        if self.calendar_months is not None:
            out.append(("calendar", float(self.calendar_months)))
        return tuple(out)


@dataclass(frozen=True)
class LifePolicy:
    due_soon_fraction: float  # "due soon" = less than this fraction of the interval remains
    component_kinds: dict[str, LifeRule]
    inspections: dict[str, LifeRule]


def _rule(raw: dict[str, Any], name_key: str) -> LifeRule:
    rule = LifeRule(
        name=str(raw[name_key]),
        hours=float(raw["hours"]) if "hours" in raw else None,
        cycles=int(raw["cycles"]) if "cycles" in raw else None,
        calendar_months=int(raw["calendar_months"]) if "calendar_months" in raw else None,
        tolerance_hours=float(raw.get("tolerance_hours", 0.0)),
        source=str(raw["source"]),
    )
    if not rule.limits():
        raise ValueError(f"life rule {rule.name!r} sets no hours, cycles or calendar limit")
    if rule.tolerance_hours and rule.hours is None:
        raise ValueError(f"life rule {rule.name!r} has a tolerance but no hours limit")
    if not rule.source:
        raise ValueError(f"life rule {rule.name!r} cites no source")
    return rule


def policy_from_toml(raw: dict[str, Any]) -> LifePolicy:
    """`raw` is the [life] table of fleet.toml."""
    kinds = [_rule(r, "kind") for r in raw.get("component_kinds", [])]
    inspections = [_rule(r, "name") for r in raw.get("inspections", [])]
    return LifePolicy(
        due_soon_fraction=float(raw["due_soon_fraction"]),
        component_kinds={r.name: r for r in kinds},
        inspections={r.name: r for r in inspections},
    )
