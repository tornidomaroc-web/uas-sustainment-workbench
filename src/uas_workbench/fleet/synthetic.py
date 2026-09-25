"""Seeded synthetic fleet: reproducible aircraft and flights with known reconciliation cases."""

from __future__ import annotations

from .config import FleetConfig
from .model import Fleet


def generate(config: FleetConfig) -> Fleet:
    raise NotImplementedError
