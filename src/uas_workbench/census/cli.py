"""uasw-census: select, fetch and census public flight logs.

uasw-census px4-select   # snapshot Flight Review's index, choose candidate logs
uasw-census px4-fetch    # size-check candidates, download via the official script
uasw-census alfa-fetch   # extract the ten ALFA DataFlash logs by HTTP range
uasw-census run          # probe every local log, write aggregate counts to results/
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import gzip
import hashlib
import json
import logging
import os
import sys
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

from . import ardupilot, px4, report, sample
from .fetch import fetch_alfa, fetch_px4, pick_within_cap
from .model import LogCensus, Source

log = logging.getLogger("uasw.census")

DBINFO_URL = "https://review.px4.io/dbinfo"
RAW = Path("data/raw")
LOCAL = Path("data/local")


def _load_dbinfo(path: Path) -> list[dict[str, Any]]:
    raw = path.read_bytes()
    if raw[:2] == b"\x1f\x8b":  # the CDN serves the index gzip-compressed
        raw = gzip.decompress(raw)
    entries: list[dict[str, Any]] = json.loads(raw.decode("utf-8"))
    return entries


def cmd_px4_select(args: argparse.Namespace) -> None:
    import urllib.request

    snapshot = args.dest / "dbinfo.json.gz"
    args.dest.mkdir(parents=True, exist_ok=True)
    if not snapshot.exists():
        log.info("downloading Flight Review index from %s", DBINFO_URL)
        with urllib.request.urlopen(DBINFO_URL, timeout=300) as response:
            snapshot.write_bytes(response.read())
    entries = _load_dbinfo(snapshot)
    spec = sample.SampleSpec(per_group=args.per_group, seed=args.seed)
    chosen = sample.select(entries, spec)
    manifest: dict[str, Any] = {
        "meta": {
            "dbinfo_sha256": hashlib.sha256(snapshot.read_bytes()).hexdigest(),
            "dbinfo_entries": len(entries),
            "spec": dataclasses.asdict(spec),
            "eligible_vehicles": {group: len(c) for group, c in chosen.items()},
            "index_stats": {
                group: sample.index_stats(entries, types, spec)
                for group, types in sample.GROUPS.items()
            },
        },
        # Keep a reserve of candidates so size or availability skips can be replaced.
        "candidates": {group: c[: args.per_group * 3] for group, c in chosen.items()},
    }
    out = args.dest / "candidates.json"
    out.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    log.info("wrote %s: %s", out, manifest["meta"]["eligible_vehicles"])


def cmd_px4_fetch(args: argparse.Namespace) -> None:
    manifest = json.loads((args.dest / "candidates.json").read_text(encoding="utf-8"))
    selection: dict[str, list[dict[str, Any]]] = {}
    for group, candidates in manifest["candidates"].items():
        picked, skipped = pick_within_cap(candidates, args.per_group, args.max_mb * 1_000_000)
        log.info(
            "%s: picked %d, skipped %d over %d MB or unavailable",
            group,
            len(picked),
            skipped,
            args.max_mb,
        )
        selection[group] = picked
    fetch_px4(selection, args.dest, args.cache)


def cmd_alfa_fetch(args: argparse.Namespace) -> None:
    fetch_alfa(args.dest)


def _safe_probe(
    probe: Callable[[Path, str], LogCensus], source: Source, path: Path, group: str
) -> LogCensus:
    try:
        return probe(path, group)
    except Exception as exc:  # a corrupt public log is a census result, not a crash
        return LogCensus(
            source=source,
            group=group,
            log_ref=path.stem,
            firmware="unknown",
            duration_s=0.0,
            airborne=False,
            error=f"{type(exc).__name__}: {exc}",
        )


def _px4_job(item: tuple[Path, str]) -> LogCensus:
    return _safe_probe(px4.probe_ulog, "px4", *item)


def _ardupilot_job(item: tuple[Path, str]) -> LogCensus:
    return _safe_probe(ardupilot.probe_dataflash, "ardupilot", *item)


def cmd_run(args: argparse.Namespace) -> None:
    px4_groups: dict[str, str] = {}
    selected = args.px4 / "selected.json"
    if selected.exists():
        for group, entries in json.loads(selected.read_text(encoding="utf-8")).items():
            px4_groups.update({e["log_id"]: group for e in entries})
    px4_items = [
        (p, px4_groups[p.stem]) for p in sorted(args.px4.glob("*.ulg")) if p.stem in px4_groups
    ]
    alfa_items = [(p, "alfa_fixed_wing") for p in sorted(args.alfa.glob("*.bin"))]
    log.info("census: %d PX4 logs, %d ALFA logs", len(px4_items), len(alfa_items))

    with ProcessPoolExecutor(max_workers=args.jobs) as pool:
        results = list(pool.map(_px4_job, px4_items)) + list(pool.map(_ardupilot_job, alfa_items))
    for r in results:
        if r.error:
            log.warning("unreadable %s/%s: %s", r.source, r.log_ref, r.error)

    LOCAL.mkdir(parents=True, exist_ok=True)
    with (LOCAL / "census_per_log.jsonl").open("w", encoding="utf-8") as out:
        for r in results:
            out.write(json.dumps(dataclasses.asdict(r)) + "\n")

    specs = {"px4": px4.FIELD_SPECS, "ardupilot": ardupilot.FIELD_SPECS}
    summaries = []
    for source, group in sorted({(r.source, r.group) for r in results}):
        members = [r for r in results if (r.source, r.group) == (source, group)]
        summaries.append(report.summarise(source, group, members, specs[source]))

    args.out.mkdir(parents=True, exist_ok=True)
    meta: dict[str, Any] = {"generated_utc": dt.datetime.now(dt.UTC).isoformat(timespec="seconds")}
    candidates = args.px4 / "candidates.json"
    if candidates.exists():
        index_meta = json.loads(candidates.read_text(encoding="utf-8"))["meta"]
        meta["px4_index"] = {k: index_meta[k] for k in ("dbinfo_entries", "spec", "index_stats")}
    (args.out / "census.json").write_text(
        json.dumps(report.to_json(summaries, meta), indent=2) + "\n", encoding="utf-8"
    )
    (args.out / "census.md").write_text(
        report.index_markdown(meta.get("px4_index")) + report.to_markdown(summaries),
        encoding="utf-8",
    )
    log.info("wrote %s", args.out)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="uasw-census",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("px4-select", help="snapshot the Flight Review index and pick candidates")
    p.add_argument("--dest", type=Path, default=RAW / "px4")
    p.add_argument("--per-group", type=int, default=20)
    p.add_argument("--seed", type=int, default=sample.SampleSpec.seed)
    p.set_defaults(func=cmd_px4_select)

    p = sub.add_parser("px4-fetch", help="download the chosen logs with the official script")
    p.add_argument("--dest", type=Path, default=RAW / "px4")
    p.add_argument("--cache", type=Path, default=Path(".cache"))
    p.add_argument("--per-group", type=int, default=20)
    p.add_argument("--max-mb", type=int, default=300)
    p.set_defaults(func=cmd_px4_fetch)

    p = sub.add_parser("alfa-fetch", help="extract the ALFA DataFlash logs by HTTP range")
    p.add_argument("--dest", type=Path, default=RAW / "alfa")
    p.set_defaults(func=cmd_alfa_fetch)

    p = sub.add_parser("run", help="census every local log and write aggregate counts")
    p.add_argument("--px4", type=Path, default=RAW / "px4")
    p.add_argument("--alfa", type=Path, default=RAW / "alfa")
    p.add_argument("--out", type=Path, default=Path("results"))
    p.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    p.set_defaults(func=cmd_run)
    return parser


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
