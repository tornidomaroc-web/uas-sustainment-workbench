import random
from typing import Any

from uas_workbench.census import sample
from uas_workbench.census.sample import SampleSpec


def entry(log_id: str, uuid: str | None, **overrides: Any) -> dict[str, Any]:
    base = {
        "log_id": log_id,
        "vehicle_uuid": uuid,
        "mav_type": "Fixed Wing",
        "duration_s": 600,
        "log_date": "2025-05-01",
        "sys_hw": "PX4_FMU_V6X",
        "ver_sw_release": "v1.15.2 255",
        "download_url": f"https://example.invalid/{log_id}.ulg",
    }
    return base | overrides


ENTRIES = [
    entry("a1", "A"),
    entry("a2", "A"),
    entry("a3", "A"),  # one heavy uploader
    entry("b1", "B"),
    entry("n1", None),
    entry("n2", ""),  # no uuid: each counts as its own vehicle
    entry("sitl", "S", sys_hw="PX4_SITL"),
    entry("short", "C", duration_s=30),
    entry("old", "D", log_date="2019-06-01"),
    entry("quad", "E", mav_type="Quadrotor"),
    entry("vt", "F", mav_type="Tiltrotor VTOL"),
]


def test_one_log_per_vehicle_and_filters_applied() -> None:
    chosen = sample.select(ENTRIES, SampleSpec())
    fixed = {e["log_id"] for e in chosen["fixed_wing"]}
    assert len(chosen["fixed_wing"]) == 4  # A, B, and two uuid-less logs
    assert len(fixed & {"a1", "a2", "a3"}) == 1
    assert {"b1", "n1", "n2"} <= fixed
    assert not fixed & {"sitl", "short", "old", "quad", "vt"}
    assert [e["log_id"] for e in chosen["vtol"]] == ["vt"]


def test_selection_is_seeded_and_independent_of_input_order() -> None:
    shuffled = ENTRIES.copy()
    random.Random(1).shuffle(shuffled)
    assert sample.select(ENTRIES, SampleSpec()) == sample.select(shuffled, SampleSpec())
    assert sample.select(ENTRIES, SampleSpec(seed=1)) != sample.select(ENTRIES, SampleSpec(seed=2))


def test_index_stats_count_every_eligible_log() -> None:
    entries = [*ENTRIES, entry("z", "0000", num_logged_errors=2)]
    stats = sample.index_stats(entries, sample.GROUPS["fixed_wing"], SampleSpec())
    # a1-a3, b1, n1, n2, z are eligible; n1, n2 and the all-zero "0000" identify nothing
    assert stats == {"logs": 7, "with_uuid": 4, "vehicles": 2, "with_logged_errors": 1}


def test_manifest_never_carries_vehicle_ids() -> None:
    for group in sample.select(ENTRIES, SampleSpec()).values():
        for e in group:
            assert "vehicle_uuid" not in e
            assert set(e) == set(sample.MANIFEST_KEYS)
