"""Polite, verifiable downloads of the two public sources. Everything lands in ignored folders.

ALFA (CMU): dataflash.zip is 541 MB, but only ten .bin members are DataFlash logs. HTTP
range requests read the zip's central directory and those ten members (about 144 MB), and
zipfile checks each member's CRC-32 as it extracts.

PX4 Flight Review: the logs are fetched by the project's own official script,
app/download_logs.py, pinned to a commit and checked by SHA-256 before it runs. Its
--log-id filter receives the sample chosen in sample.py, and its built-in 6 s delay applies.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import subprocess
import sys
import time
import urllib.request
import zipfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

USER_AGENT = "uas-sustainment-workbench-census/0.0.1 (+research; polite)"

ALFA_DATAFLASH_URL = "https://ndownloader.figshare.com/files/24096047"
ALFA_DATAFLASH_SIZE = 541_342_491

FLIGHT_REVIEW_COMMIT = "ea5c16a3bb939f49e50296f953efac40c2456489"
FLIGHT_REVIEW_FILES: dict[str, str] = {
    "app/download_logs.py": "3e0d326ed9892046d094413edbf8ca9ca8ded00e7296a3c0053ea69d47fc819d",
    "app/plot_app/config_tables.py": (
        "18915629ec6a82dbcc51ea7558cb92ee503aa8800e1b609f3ac545e83f0bec69"
    ),
}


def _get(
    url: str, headers: dict[str, str] | None = None, method: str = "GET", timeout: float = 120
) -> tuple[bytes, dict[str, str]]:
    request = urllib.request.Request(
        url, method=method, headers={"User-Agent": USER_AGENT, **(headers or {})}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read(), dict(response.headers)


class HttpRangeFile(io.RawIOBase):
    """A read-only, seekable view of a remote file, one HTTP range request per read.
    The figshare link redirects to a signed URL that expires in 10 s, so every read
    resolves the redirect again instead of caching it."""

    def __init__(self, url: str, size: int) -> None:
        self.url, self.size, self.pos, self.requests = url, size, 0, 0

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        base = {io.SEEK_SET: 0, io.SEEK_CUR: self.pos, io.SEEK_END: self.size}[whence]
        self.pos = base + offset
        return self.pos

    def readinto(self, buffer: Any) -> int:
        view = memoryview(buffer).cast("B")
        if self.pos >= self.size or len(view) == 0:
            return 0
        end = min(self.pos + len(view), self.size) - 1
        data, _ = _get(self.url, headers={"Range": f"bytes={self.pos}-{end}"})
        self.requests += 1
        view[: len(data)] = data
        self.pos += len(data)
        return len(data)


def fetch_alfa(
    dest: Path, url: str = ALFA_DATAFLASH_URL, size: int = ALFA_DATAFLASH_SIZE
) -> list[Path]:
    dest.mkdir(parents=True, exist_ok=True)
    remote = HttpRangeFile(url, size)
    archive = zipfile.ZipFile(io.BufferedReader(remote, buffer_size=1 << 20))
    members = [m for m in archive.infolist() if m.filename.lower().endswith(".bin")]
    log.info("alfa: %d .bin members in dataflash.zip", len(members))
    written = []
    for member in members:
        target = dest / Path(member.filename).name.replace(" ", "_")
        if target.exists() and target.stat().st_size == member.file_size:
            log.info("alfa: have %s", target.name)
            written.append(target)
            continue
        with archive.open(member) as src, target.open("wb") as out:  # CRC checked on read
            while chunk := src.read(1 << 20):
                out.write(chunk)
        log.info("alfa: wrote %s (%d bytes)", target.name, member.file_size)
        written.append(target)
    log.info("alfa: done, %d range requests", remote.requests)
    return written


def official_script(cache: Path) -> Path:
    """Fetch download_logs.py (and the one module it imports) at the pinned commit and
    refuse to run it if either file's hash differs from the reviewed one."""
    base = f"https://raw.githubusercontent.com/PX4/flight_review/{FLIGHT_REVIEW_COMMIT}/"
    for relpath, expected in FLIGHT_REVIEW_FILES.items():
        target = cache / "flight_review" / relpath
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(_get(base + relpath)[0])
        actual = hashlib.sha256(target.read_bytes()).hexdigest()
        if actual != expected:
            raise RuntimeError(f"{relpath}: sha256 {actual} does not match pinned {expected}")
    return (cache / "flight_review" / "app" / "download_logs.py").resolve()


def remote_size(url: str) -> int | None:
    try:
        _, headers = _get(url, method="HEAD", timeout=30)
    except OSError as exc:
        log.warning("px4: HEAD failed for %s: %s", url, exc)
        return None
    length = headers.get("Content-Length")
    return int(length) if length else None


def pick_within_cap(
    candidates: Sequence[dict[str, Any]], per_group: int, max_bytes: int, pause_s: float = 1.0
) -> tuple[list[dict[str, Any]], int]:
    """Walk the seeded candidate order and keep the first logs whose size is within the cap.
    Returns (picked, number skipped for size or unavailability)."""
    picked: list[dict[str, Any]] = []
    skipped = 0
    for entry in candidates:
        if len(picked) == per_group:
            break
        size = remote_size(str(entry["download_url"]))
        time.sleep(pause_s)
        if size is None or size > max_bytes:
            skipped += 1
            continue
        picked.append({**entry, "size_bytes": size})
    return picked, skipped


def fetch_px4(selection: dict[str, list[dict[str, Any]]], dest: Path, cache: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "selected.json").write_text(json.dumps(selection, indent=2), encoding="utf-8")
    ids = [entry["log_id"] for group in selection.values() for entry in group]
    script = official_script(cache)
    command = [
        sys.executable,
        str(script),
        "--log-id",
        *ids,
        "--max-num",
        str(len(ids)),
        "--download-folder",
        str(dest.resolve()),
        "--delay",
        "6",
    ]
    log.info("px4: running official download_logs.py for %d logs", len(ids))
    subprocess.run(command, cwd=script.parent, check=True)
