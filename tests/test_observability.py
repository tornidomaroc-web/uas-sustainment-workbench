"""The JSON log handler writes to whatever `sys.stderr` is when a record is emitted, not to
the stream it was when `configure_logging` was first called (issue #48).

`configure_logging` is idempotent, so the handler it adds lives for the process. pytest's
`capsys` replaces `sys.stderr` for every test; a handler bound to the first one it saw writes
every later test's log lines into an earlier test's buffer, which is why
`tests/test_unreadable_records.py` followed by `tests/test_ingest_cli.py` fails on a main that
passes in the default order.
"""

from __future__ import annotations

import io
import logging
import sys

import pytest

from uas_workbench.service.observability import configure_logging


def test_the_handler_follows_the_current_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    """The minimal reproduction from issue #48: two streams, two calls, two lines."""
    first, second = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stderr", first)
    configure_logging()
    logging.getLogger("uasw.cli").warning("one")
    monkeypatch.setattr(sys, "stderr", second)
    configure_logging()
    logging.getLogger("uasw.cli").warning("two")
    assert '"message": "one"' in first.getvalue() and '"message": "two"' not in first.getvalue()
    assert '"message": "two"' in second.getvalue()


def test_a_later_test_sees_its_own_log_lines(capsys: pytest.CaptureFixture[str]) -> None:
    """What the two-module order needs: this test's `capsys` holds this test's lines, however
    many tests called `configure_logging` before it under their own captured stderr."""
    configure_logging()
    logging.getLogger("uasw.cli").warning("seen here")
    assert '"message": "seen here"' in capsys.readouterr().err
