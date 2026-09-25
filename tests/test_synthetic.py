"""The seeded synthetic fleet: reproducible, labelled, and rich enough to exercise reconcile()."""

import dataclasses
import re

from uas_workbench.fleet import Fleet, load_config
from uas_workbench.fleet.synthetic import generate
from uas_workbench.flight import Unknown, is_known, reconcile
from uas_workbench.flight.codec import JsonDict, record_to_json

CONFIG = load_config()
# Readiness codes that are not civil vocabulary; the fleet must never use them.
EXCLUDED_CODES = re.compile(r"\b(FMC|PMC|NMC|sortie)\b", re.I)


def test_same_seed_same_fleet_different_seed_different_fleet() -> None:
    a = generate(CONFIG)
    b = generate(CONFIG)
    c = generate(dataclasses.replace(CONFIG, seed=CONFIG.seed + 1))
    assert a.aircraft == b.aircraft and _dump(a) == _dump(b)
    assert _dump(a) != _dump(c)


def _dump(fleet: Fleet) -> list[JsonDict]:
    return [record_to_json(r) for records in fleet.flights.values() for r in records]


def test_everything_synthetic_is_marked_and_civil() -> None:
    fleet = generate(CONFIG)
    assert len(fleet.aircraft) == CONFIG.aircraft
    for aircraft in fleet.aircraft:
        assert aircraft.synthetic
        assert "synthetic" in aircraft.attribution.lower()
        assert not is_known(aircraft.status) or aircraft.status in CONFIG.states
        assert (
            EXCLUDED_CODES.search(f"{aircraft.key} {aircraft.label} {aircraft.attribution}") is None
        )
        records = fleet.flights[aircraft.key]
        assert len(records) >= 3
        for r in records:
            assert r.synthetic and r.licence == "CC0"
            assert "synthetic" in r.attribution.lower()
            assert r.log_ref.endswith(".synthetic")
            assert EXCLUDED_CODES.search(str(record_to_json(r))) is None


def test_fleet_contains_the_reconciliation_cases() -> None:
    fleet = generate(CONFIG)
    kinds: set[str] = set()
    duplicates = 0
    boot_gaps = 0
    unknown_fields = 0
    for aircraft in fleet.aircraft:
        result = reconcile(fleet.flights[aircraft.key], aircraft_key=aircraft.key)
        kinds |= {f.kind for f in result.findings}
        duplicates += sum(1 for v in result.unchecked.values() if v.startswith("duplicate of"))
        boot_gaps += sum(
            1 for c in result.coverage if is_known(c.boots_between) and c.boots_between > 0
        )
        for r in fleet.flights[aircraft.key]:
            unknown_fields += sum(
                1 for f in dataclasses.fields(r) if isinstance(getattr(r, f.name), Unknown)
            )
    assert "unlogged_flight" in kinds
    assert duplicates >= 1
    assert boot_gaps >= 1
    assert unknown_fields >= 1  # the fleet also shows what a log cannot say
