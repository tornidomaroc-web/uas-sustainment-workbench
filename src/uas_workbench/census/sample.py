"""Choose which public PX4 Flight Review logs the census reads.

The official download script sorts newest first and takes the top N, so a plain
"--mav-type 'Fixed Wing' --max-num 20" returns one uploader's afternoon (measured on
2026-09-25: 20 logs from 6 aircraft, all dated the same day). This module samples
*aircraft*, not logs: it shuffles distinct vehicles with a fixed seed and takes one random
log from each, so no single heavy uploader dominates the census.
"""

from __future__ import annotations

import random
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

Entry = Mapping[str, Any]

GROUPS: dict[str, frozenset[str]] = {
    "fixed_wing": frozenset({"Fixed Wing"}),
    "vtol": frozenset(
        {
            "VTOL Standard",
            "Tiltrotor VTOL",
            "Two-rotor VTOL (Tailsitter)",
            "Quad-rotor VTOL (Tailsitter)",
            "VTOL Tailsitter",
        }
    ),
}

# Fields copied into the local manifest. vehicle_uuid is deliberately left out.
MANIFEST_KEYS: tuple[str, ...] = (
    "log_id",
    "log_date",
    "mav_type",
    "duration_s",
    "ver_sw_release",
    "sys_hw",
    "download_url",
)


@dataclass(frozen=True)
class SampleSpec:
    per_group: int = 20
    min_duration_s: int = 120  # below this, most uploads are bench or arming tests
    min_date: str = "2023-01-01"  # current firmware era; 97 % of real fixed-wing/VTOL logs
    seed: int = 20260925


def is_simulated(entry: Entry) -> bool:
    return "SITL" in str(entry.get("sys_hw") or "").upper()


def eligible(entry: Entry, types: frozenset[str], spec: SampleSpec) -> bool:
    return (
        entry.get("mav_type") in types
        and not is_simulated(entry)
        and int(entry.get("duration_s") or 0) >= spec.min_duration_s
        and str(entry.get("log_date") or "") >= spec.min_date
    )


def candidates(
    entries: Iterable[Entry], types: frozenset[str], spec: SampleSpec, rng: random.Random
) -> list[dict[str, Any]]:
    """Every eligible vehicle once, in seeded random order, each with one random log.
    Returns the full ordered list; the fetcher takes from the front until its quota is met,
    skipping logs it cannot use (too large, gone)."""
    by_vehicle: dict[str, list[Entry]] = defaultdict(list)
    for entry in entries:
        if eligible(entry, types, spec):
            vehicle = str(entry.get("vehicle_uuid") or f"no-uuid:{entry['log_id']}")
            by_vehicle[vehicle].append(entry)
    vehicles = sorted(by_vehicle)  # input order must not change the result
    rng.shuffle(vehicles)
    chosen = []
    for vehicle in vehicles:
        logs = sorted(by_vehicle[vehicle], key=lambda e: str(e["log_id"]))
        entry = rng.choice(logs)
        chosen.append({key: entry.get(key) for key in MANIFEST_KEYS})
    return chosen


def index_stats(
    entries: Iterable[Entry], types: frozenset[str], spec: SampleSpec
) -> dict[str, int]:
    """Population-level counts over every eligible log in the index, not just the sample.
    Flight Review fills vehicle_uuid from the log's sys_uuid and counts ERROR-level messages
    at upload, so these two fields can be measured on tens of thousands of logs for free."""
    logs = [e for e in entries if eligible(e, types, spec)]
    uuids = [str(e.get("vehicle_uuid") or "") for e in logs]
    return {
        "logs": len(logs),
        "with_uuid": sum(1 for u in uuids if u.strip("0")),
        "vehicles": len({u for u in uuids if u.strip("0")}),
        "with_logged_errors": sum(1 for e in logs if int(e.get("num_logged_errors") or 0) > 0),
    }


def select(entries: Sequence[Entry], spec: SampleSpec) -> dict[str, list[dict[str, Any]]]:
    rng = random.Random(spec.seed)
    return {group: candidates(entries, types, spec, rng) for group, types in GROUPS.items()}
