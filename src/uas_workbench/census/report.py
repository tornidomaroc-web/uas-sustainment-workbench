"""Aggregate per-log census records into counts. Only these counts are ever committed."""

from __future__ import annotations

import re
import statistics
from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any

from .model import COUNTERS, FLIGHT_COUNTERS, FieldSpec, LogCensus


@dataclass(frozen=True)
class FieldCount:
    key: str
    counter: str
    filled_means: str
    extra: bool
    present: int
    filled: int
    filled_airborne: int


@dataclass(frozen=True)
class CounterCount:
    name: str
    derivable: int
    derivable_airborne: int


@dataclass(frozen=True)
class GroupSummary:
    source: str
    group: str
    logs: int
    unreadable: int
    airborne: int
    median_duration_s: float
    firmware: dict[str, int]
    fields: list[FieldCount]
    counters: list[CounterCount]


def _firmware_family(version: str) -> str:
    """'v1.14.3 (dev)' -> 'v1.14'; 'ArduPlane 3.9.2' -> 'ArduPlane 3.9'."""
    match = re.search(r"(\d+)\.(\d+)", version)
    if match is None:
        return version
    return f"{version[: match.start()]}{match[1]}.{match[2]}"


def summarise(
    source: str, group: str, results: Sequence[LogCensus], specs: Sequence[FieldSpec]
) -> GroupSummary:
    readable = [r for r in results if r.error is None]
    flown = [r for r in readable if r.airborne]
    fields = [
        FieldCount(
            key=spec.key,
            counter=spec.counter,
            filled_means=spec.filled_means,
            extra=spec.extra,
            present=sum(r.fields[spec.key].present for r in readable),
            filled=sum(r.fields[spec.key].filled for r in readable),
            filled_airborne=sum(r.fields[spec.key].filled for r in flown),
        )
        for spec in specs
    ]
    counters = [
        CounterCount(
            name=name,
            derivable=sum(r.counters[name] for r in readable),
            derivable_airborne=sum(r.counters[name] for r in flown),
        )
        for name in COUNTERS
    ]
    durations = [r.duration_s for r in readable]
    return GroupSummary(
        source=source,
        group=group,
        logs=len(results),
        unreadable=len(results) - len(readable),
        airborne=len(flown),
        median_duration_s=round(statistics.median(durations), 1) if durations else 0.0,
        firmware=dict(sorted(Counter(_firmware_family(r.firmware) for r in readable).items())),
        fields=fields,
        counters=counters,
    )


def to_json(summaries: Sequence[GroupSummary], meta: dict[str, Any]) -> dict[str, Any]:
    return {"meta": meta, "groups": [asdict(s) for s in summaries]}


def _pct(part: int, whole: int) -> str:
    """Whole percent, but never round a partial share to 0 % or 100 %."""
    if not whole:
        return "0/0"
    share = 100 * part / whole
    text = f"{share:.1f}" if 0 < share < 1 or 99 < share < 100 else f"{share:.0f}"
    return f"{part}/{whole} ({text} %)"


def index_markdown(index: dict[str, Any] | None) -> str:
    """Population-level rows from the Flight Review index (every eligible log, not a sample)."""
    if not index:
        return ""
    lines = [
        f"### px4 / whole index ({index['dbinfo_entries']} public logs; same filters as the "
        "sample)",
        "",
        "| Group | Eligible logs | Aircraft key (sys_uuid) filled | Distinct aircraft "
        "| Logs with ERROR-level messages |",
        "|---|---|---|---|---|",
    ]
    for group, st in index["index_stats"].items():
        lines.append(
            f"| {group} | {st['logs']} | {_pct(st['with_uuid'], st['logs'])} | {st['vehicles']} "
            f"| {_pct(st['with_logged_errors'], st['logs'])} |"
        )
    return "\n".join([*lines, "", ""])


def to_markdown(summaries: Sequence[GroupSummary]) -> str:
    lines: list[str] = []
    for s in summaries:
        n = s.logs - s.unreadable
        firmware = ", ".join(f"{k}: {v}" for k, v in s.firmware.items())
        lines += [
            f"### {s.source} / {s.group}",
            "",
            f"{s.logs} logs, {s.unreadable} unreadable, {s.airborne} airborne; "
            f"median length {s.median_duration_s:.0f} s. Firmware: {firmware}.",
            "",
            "| Field | Serves | 'Real value' means | Logs with field | Logs with real values "
            "| Real values, airborne logs |",
            "|---|---|---|---|---|---|",
        ]
        for f in s.fields:
            label = f"{f.key} *(extra)*" if f.extra else f.key
            lines.append(
                f"| {label} | {f.counter} | {f.filled_means} | {_pct(f.present, n)} "
                f"| {_pct(f.filled, n)} | {_pct(f.filled_airborne, s.airborne)} |"
            )
        lines += [
            "",
            "| Counter | Derivable, all logs | Derivable, airborne logs |",
            "|---|---|---|",
        ]
        for c in s.counters:
            flight = " (needs a flight)" if c.name in FLIGHT_COUNTERS else ""
            lines.append(
                f"| {c.name}{flight} | {_pct(c.derivable, n)} "
                f"| {_pct(c.derivable_airborne, s.airborne)} |"
            )
        lines.append("")
    return "\n".join(lines)
