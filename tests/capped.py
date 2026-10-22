"""Run a snippet in a child process capped in time and in memory.

A crafted log that makes a reader lay out billions of elements must not take the test run's
own memory or time with it: the read runs in a process of its own, which the OS refuses
memory past the cap (a job object's process memory limit on Windows, the address space limit
elsewhere), and which is terminated once the time cap passes.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any

# Run first in the child: cap its memory, then hand over to the snippet.
_CAP = """
import os, sys
mib = int(os.environ["CAPPED_MIB"])
if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class Basic(ctypes.Structure):
        _fields_ = [
            ("a", ctypes.c_longlong), ("b", ctypes.c_longlong), ("flags", wintypes.DWORD),
            ("c", ctypes.c_size_t), ("d", ctypes.c_size_t), ("e", wintypes.DWORD),
            ("f", ctypes.c_size_t), ("g", wintypes.DWORD), ("h", wintypes.DWORD),
        ]

    class Extended(ctypes.Structure):
        _fields_ = [
            ("basic", Basic), ("io", ctypes.c_ulonglong * 6), ("process", ctypes.c_size_t),
            ("job", ctypes.c_size_t), ("peak_process", ctypes.c_size_t),
            ("peak_job", ctypes.c_size_t),
        ]

    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    job = wintypes.HANDLE(k32.CreateJobObjectW(None, None))
    limit = Extended()
    limit.basic.flags = 0x100  # JOB_OBJECT_LIMIT_PROCESS_MEMORY
    limit.process = mib << 20
    assert k32.SetInformationJobObject(job, 9, ctypes.byref(limit), ctypes.sizeof(limit))
    assert k32.AssignProcessToJobObject(job, wintypes.HANDLE(k32.GetCurrentProcess()))
else:
    import resource
    resource.setrlimit(resource.RLIMIT_AS, (mib << 20, mib << 20))
"""


@dataclass(frozen=True)
class Outcome:
    """What the snippet printed last (JSON), or why there is nothing: "timeout" when the time
    cap passed, "exit N" when the child ended without printing."""

    said: Any
    seconds: float
    stderr: str


def run_capped(snippet: str, *, mib: int, seconds: float, args: list[str]) -> Outcome:
    """Run `snippet` (which prints one JSON line last) with `args` as sys.argv[1:]."""
    env = dict(os.environ, CAPPED_MIB=str(mib), OPENBLAS_NUM_THREADS="1", PYTHONWARNINGS="ignore")
    started = time.monotonic()
    try:
        done = subprocess.run(
            [sys.executable, "-c", _CAP + snippet, *args],
            capture_output=True,
            text=True,
            timeout=seconds,
            env=env,
        )
    except subprocess.TimeoutExpired as late:
        return Outcome("timeout", time.monotonic() - started, str(late.stderr or "")[-2000:])
    taken = time.monotonic() - started
    lines = done.stdout.strip().splitlines()
    said = json.loads(lines[-1]) if lines and done.returncode == 0 else f"exit {done.returncode}"
    return Outcome(said, taken, done.stderr[-2000:])
