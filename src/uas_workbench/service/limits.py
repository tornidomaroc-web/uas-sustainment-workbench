"""The size limit on one log, the same for POST /ingest and `uasw ingest` (issue #89).

512 MiB unless `UASW_MAX_LOG_MIB` sets another whole number of MiB. The basis, measured on the
real logs read here: the largest are 80 MB of ArduPilot DataFlash (ALFA) and 246 MB (235 MiB)
of PX4 ULog (public Flight Review logs), so the limit takes the largest with twice its size
to spare. A log costs memory while it is read, on the command line as through the service:
pymavlink indexes a DataFlash log at about 1.5 bytes per byte (120 MB for the 80 MB log), the
ULog reader about 17 MB for the 246 MB log; a DataFlash log at the limit can take some 0.8 GB.
An operator with longer logs raises the limit; one with less memory lowers it.
"""

from __future__ import annotations

import os
import re

ENV = "UASW_MAX_LOG_MIB"
DEFAULT_MIB = 512
# What POST /ingest reads past the limit before it refuses a body: the form's other fields and
# its framing, which a log at the limit arrives with. A log over the limit inside this
# allowance is refused by its own size once the form is read.
FORM_ALLOWANCE = 1 << 20


def max_log_mib(value: str | None = None) -> int:
    """The limit in MiB: `value`, else `UASW_MAX_LOG_MIB`, else 512; empty is unset. Anything
    but a whole number above 0 is a ValueError naming it, so a limit is never guessed."""
    text = os.environ.get(ENV) if value is None else value
    if not text:
        return DEFAULT_MIB
    if not re.fullmatch(r"[0-9]+", text) or int(text) == 0:
        raise ValueError(f"{ENV} is {text!r}; it takes a whole number of MiB above 0")
    return int(text)


def too_large(mib: int) -> str:
    """The refusal of a log over the limit (413), on the command line and the service alike."""
    return (
        f"the log is larger than the {mib} MiB limit for one log, so it is not read; "
        f"{ENV} sets the limit"
    )
