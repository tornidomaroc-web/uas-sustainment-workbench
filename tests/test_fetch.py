import io
import re
import zipfile
from collections.abc import Callable
from pathlib import Path

import pytest

from uas_workbench.census import fetch


def fake_server(blob: bytes, calls: list[str]) -> Callable[..., tuple[bytes, dict[str, str]]]:
    def _get(
        url: str, headers: dict[str, str] | None = None, method: str = "GET", timeout: float = 120
    ) -> tuple[bytes, dict[str, str]]:
        calls.append((headers or {}).get("Range", "full"))
        match = re.fullmatch(r"bytes=(\d+)-(\d+)", (headers or {}).get("Range", ""))
        assert match, "every read must be a range request"
        start, end = int(match[1]), int(match[2])
        return blob[start : end + 1], {}

    return _get


def test_alfa_extracts_only_dataflash_members_by_range(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("dataflash/2018-07-18/2018-07-18 12-38-10.bin", b"\xa3\x95" * 5000)
        z.writestr("dataflash/2018-07-18/2018-07-18 12-38-10.log", b"text " * 50_000)
        z.writestr("dataflash/2018-07-18/2018-07-18 12-38-10.bin.gpx", b"<gpx/>")
    blob = buffer.getvalue()
    calls: list[str] = []
    monkeypatch.setattr(fetch, "_get", fake_server(blob, calls))

    written = fetch.fetch_alfa(tmp_path, url="https://example.invalid/z", size=len(blob))

    assert [p.name for p in written] == ["2018-07-18_12-38-10.bin"]
    assert written[0].read_bytes() == b"\xa3\x95" * 5000
    assert all(c != "full" for c in calls)


def test_official_script_refuses_a_changed_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fetch, "_get", lambda *a, **k: (b"print('tampered')", {}))
    with pytest.raises(RuntimeError, match="does not match pinned"):
        fetch.official_script(tmp_path)


def test_pick_within_cap_skips_large_and_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    sizes = {"a": 10, "b": None, "c": 999, "d": 20, "e": 30}
    monkeypatch.setattr(fetch, "remote_size", lambda url: sizes[url])
    candidates = [{"log_id": k, "download_url": k} for k in sizes]

    picked, skipped = fetch.pick_within_cap(candidates, per_group=2, max_bytes=100, pause_s=0)

    assert [p["log_id"] for p in picked] == ["a", "d"]
    assert [p["size_bytes"] for p in picked] == [10, 20]
    assert skipped == 2
