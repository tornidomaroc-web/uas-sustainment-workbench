"""The seeded synthetic fleet: reproducible, labelled, and rich enough to exercise reconcile()."""

import dataclasses
import hashlib
import json
import re
from datetime import UTC, datetime

from uas_workbench.fleet import Fleet, load_config
from uas_workbench.fleet.synthetic import generate
from uas_workbench.flight import Unknown, is_known, reconcile
from uas_workbench.flight.codec import JsonDict, record_to_json
from uas_workbench.ledger import Entry, entry_to_json
from uas_workbench.service.app import fleet_grounded
from uas_workbench.service.store import Store

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
        assert (
            EXCLUDED_CODES.search(f"{aircraft.key} {aircraft.label} {aircraft.attribution}") is None
        )
        records = fleet.flights[aircraft.key]
        assert len(records) >= 2  # an aircraft grounded after its second flight has two logs
        for r in records:
            assert r.synthetic and r.licence == "CC0"
            assert "synthetic" in r.attribution.lower()
            assert r.log_ref.endswith(".synthetic")
            assert EXCLUDED_CODES.search(str(record_to_json(r))) is None


def test_no_synthetic_aircraft_flies_while_its_records_show_it_grounded() -> None:
    """The generator's rule since 0.5.0, judged by the comparison the service reports with:
    not one log of the demo fleet starts while its aircraft's own records show it
    unserviceable, in maintenance or AOG. Every log is still accounted for."""
    store = Store(":memory:")
    store.add_fleet(generate(CONFIG))
    found = fleet_grounded(store, CONFIG, datetime(2026, 10, 1, tzinfo=UTC))
    assert found.findings == []
    # 30 synthetic logs: 26 judged; SYN-06's three (no record) and SYN-03's second upload not.
    assert (found.logs, found.judged, found.not_judged) == (30, 26, 4)


def test_the_logs_left_out_are_the_ones_after_grounding_and_the_draws_are_unchanged() -> None:
    """What 0.5.0 changed in the generation, and what it did not. All flights are drawn as
    before; the logs that would start while the aircraft is grounded are then left out: two of
    SYN-01's five, after its pack reached its limit, and four of SYN-05's six, after its work
    order opened. The digests are those of the 0.4.0 seed: the flights of the five other
    aircraft, the logs SYN-01 and SYN-05 keep, and every entry but two are byte for byte
    what they were."""
    fleet = generate(CONFIG)
    assert {k: len(v) for k, v in fleet.flights.items()} == {
        "SYN-01": 3, "SYN-02": 6, "SYN-03": 7, "SYN-04": 3, "SYN-05": 2, "SYN-06": 3, "SYN-07": 6,
    }  # fmt: skip
    others = {
        k: [record_to_json(r) for r in v]
        for k, v in fleet.flights.items()
        if k not in ("SYN-01", "SYN-05")
    }
    assert _digest(others) == "868ce1375b3adb06"
    assert _digest([record_to_json(r) for r in fleet.flights["SYN-01"]]) == "46321f97c1b3711c"
    assert _digest([record_to_json(r) for r in fleet.flights["SYN-05"]]) == "f145c40f89fef4f7"

    def changed(e: Entry) -> bool:
        return e.subject == "BAT-04A" and e.kind in ("component.register", "component.remove")

    assert len(fleet.entries) == 60
    assert _digest([entry_to_json(e) for e in fleet.entries if not changed(e)]) == (
        "601cd24dcd840e92"
    )
    registered, removed = (e for e in fleet.entries if changed(e))
    assert registered.details["cycles_before"] == 297  # 298 until 0.5.0
    assert removed.statement == (
        "[synthetic] battery pack BAT-04A removed from SYN-01, tagged unserviceable and segregated"
    )  # "... and placed in storage" until 0.5.0
    assert not any("placed in storage" in e.statement for e in fleet.entries)


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()[:16]


def test_fleet_contains_the_reconciliation_cases() -> None:
    fleet = generate(CONFIG)
    kinds: set[str] = set()
    findings_by_aircraft: dict[str, int] = {}
    duplicates = 0
    boot_gaps = 0
    unknown_fields = 0
    for aircraft in fleet.aircraft:
        result = reconcile(fleet.flights[aircraft.key], aircraft_key=aircraft.key)
        kinds |= {f.kind for f in result.findings}
        if result.findings:
            findings_by_aircraft[aircraft.key] = len(result.findings)
        duplicates += sum(1 for v in result.unchecked.values() if v.startswith("duplicate of"))
        boot_gaps += sum(
            1 for c in result.coverage if is_known(c.boots_between) and c.boots_between > 0
        )
        for r in fleet.flights[aircraft.key]:
            unknown_fields += sum(
                1 for f in dataclasses.fields(r) if isinstance(getattr(r, f.name), Unknown)
            )
    assert "unlogged_flight" in kinds
    assert "counter_mismatch" not in kinds  # the generator never contradicts its own counter
    assert findings_by_aircraft == {"SYN-02": 1, "SYN-04": 1}
    assert duplicates >= 1
    assert boot_gaps >= 1
    assert unknown_fields >= 1  # the fleet also shows what a log cannot say
