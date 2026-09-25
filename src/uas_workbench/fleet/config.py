"""Fleet policy loaded from fleet.toml (vocabulary, tolerances, life limits, synthetic seed)."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from datetime import date
from importlib import resources

from uas_workbench.life import LifePolicy, policy_from_toml


@dataclass(frozen=True)
class FleetConfig:
    states: tuple[str, ...]
    tolerance_s: float
    seed: int
    aircraft: int
    first_day: date
    life: LifePolicy


def load_config() -> FleetConfig:
    text = resources.files("uas_workbench.fleet").joinpath("fleet.toml").read_text("utf-8")
    raw = tomllib.loads(text)
    return FleetConfig(
        states=tuple(raw["vocabulary"]["states"]),
        tolerance_s=float(raw["reconcile"]["tolerance_s"]),
        seed=int(raw["synthetic"]["seed"]),
        aircraft=int(raw["synthetic"]["aircraft"]),
        first_day=date.fromisoformat(raw["synthetic"]["first_day"]),
        life=policy_from_toml(raw["life"]),
    )
