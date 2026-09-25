"""The real, licensed log excerpts committed under tests/fixtures, as showcase aircraft."""

from __future__ import annotations

from pathlib import Path

from .model import Fleet

ALFA_KEY = "alfa-fixed-wing"
PX4_KEY = "px4-validation-quadrotor"


def showcase(fixtures_dir: Path) -> Fleet:
    raise NotImplementedError
