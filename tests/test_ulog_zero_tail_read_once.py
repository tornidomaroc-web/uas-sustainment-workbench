"""A ULog ending in a short zero run is read once (issue #129).

0.6.4 read such a log whole, to learn whether pyulog marks it corrupt for the zeros, and when
it did, read it again up to where they begin: twice the time of the log without them. The
count here is of the message headers pyulog parses, in a process of its own, so it does not
depend on the machine's speed: a second read parses every header again.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from builders.ulog import ULogBuilder

SAMPLES = 20_000

# Counts every message header pyulog parses, through whatever class reads it, then reads the
# file as the flight reader, the census and the excerpt cutter do.
DRIVER = """
import json, sys
from pathlib import Path
from pyulog import ULog
from uas_workbench.flight import ulog_declared

parsed = 0
initialize = ULog._MessageHeader.initialize

def counted(self, data):
    global parsed
    parsed += 1
    initialize(self, data)

ULog._MessageHeader.initialize = counted
try:
    ulog, end = ulog_declared.read_file(Path(sys.argv[1]), ["sample"])
    said = ["read", end, len(ulog.get_dataset("sample").data["timestamp"])]
except ValueError as refused:
    said = ["refused", str(refused)]
print(json.dumps({"said": said, "headers": parsed}))
"""


def long_log(last: float) -> bytes:
    """SAMPLES data messages, each 45 bytes; every value of the last one is `last`."""
    b = ULogBuilder().format("sample", ["uint64_t timestamp", "float[8] values"])
    b.info("sys_uuid", "synthetic-vehicle-0129")
    sample = b.subscribe("sample")
    for i in range(SAMPLES):
        value = last if i == SAMPLES - 1 else 1.1  # 1.1 as float32 has no zero byte
        b.data(sample, {"timestamp": 2_000_000 + i * 1000, "values": [value] * 8})
    return b.build()


def shapes() -> dict[str, tuple[bytes, int | None, int]]:
    """Each shape's bytes, where it is read to (None: whole) and the samples read."""
    log = long_log(1.1)
    own_zeros = long_log(0.0)  # its last 32 bytes are zero, its own
    cut = log[: len(log) - 45 + 3 + 2 + 8 + 5]  # inside the last sample's second value
    return {
        "4096 zeros after the log": (log + bytes(4096), len(log), SAMPLES),
        "512 zeros after a sample cut short": (cut + bytes(512), len(cut), SAMPLES - 1),
        "the last sample's own zeros": (own_zeros, None, SAMPLES),
        "70,000 zeros after the log": (log + bytes(70_000), len(log), SAMPLES),
    }


@pytest.mark.parametrize("shape", list(shapes()))
def test_a_log_ending_in_zeros_is_read_once(shape: str, tmp_path: Path) -> None:
    written, end, samples = shapes()[shape]
    path = tmp_path / "zeros.ulg"
    path.write_bytes(written)
    done = subprocess.run(
        [sys.executable, "-c", DRIVER, str(path)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr
    outcome = json.loads(done.stdout.strip().splitlines()[-1])
    assert outcome["said"] == ["read", end, samples]
    # One read parses each of the SAMPLES + 4 messages once; a second read, all of them again.
    assert outcome["headers"] < 1.1 * SAMPLES, outcome
