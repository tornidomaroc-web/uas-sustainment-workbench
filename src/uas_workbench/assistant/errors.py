"""The refusals that end a run of the assistant.

A run ends where a read or a model turn fails, so the model is never handed a refusal, a
failed read or a partial result to answer from, and no answer, grounding or recording can
carry it. Every refusal is a `RefusedCall`: a status, a sentence, and `str()` is the one line
every `uasw` command prints for a refusal, `refused (<status>): <sentence>`.
"""

from __future__ import annotations

from http.client import HTTPException
from urllib.error import HTTPError, URLError

# What the standard library raises when an HTTP exchange does not complete (issue #49).
# urllib wraps a connection that is refused, a host that does not resolve and a timeout while
# connecting in `URLError`; a timeout while the reply is awaited is `TimeoutError`; a peer
# that closes or resets the connection is a `ConnectionError` (http.client's
# `RemoteDisconnected` is one); a reply that is not HTTP is an `HTTPException`
# (`BadStatusLine`, `IncompleteRead`). Nothing wider: an error in the code, or a reply that
# is HTTP but not the JSON expected, still raises. `HTTPError`, the status a server that did
# answer sent, is a `URLError` too and is handled before these wherever they are caught.
TRANSPORT_ERRORS = (URLError, TimeoutError, ConnectionError, HTTPException)


def transport_reason(exc: BaseException) -> str:
    """The reason to show for a transport error: the error urllib wrapped, else the error's
    own text, else its class name when it carries no text."""
    if isinstance(exc, URLError) and not isinstance(exc, HTTPError):
        return str(exc.reason)
    return str(exc) or type(exc).__name__


class RefusedCall(Exception):
    """The service answered a tool call with an error status (issue #46): the run ends here.

    The model is not asked again, so it never holds a refusal it could phrase as a value or
    build an answer on, and no answer, grounding or recording can carry the call. `detail` is
    the service's own sentence (a 503 names the stored record that cannot be read and points
    to `uasw verify`; a 404 names the aircraft the model asked for), whole; `str()` is the
    line every `uasw` command prints for a refusal."""

    def __init__(self, status: int, detail: str, tool: str = "", path: str = "") -> None:
        super().__init__(f"refused ({status}): {detail}")
        self.status = status
        self.detail = detail
        self.tool = tool
        self.path = path


class ServiceUnreachable(RefusedCall):
    """The service did not answer a tool call (issue #49): nothing listens at the URL, the
    host does not resolve, or the connection timed out, was reset or carried no HTTP reply.
    Reported as a 503 naming the URL and the reason; no record was read."""

    def __init__(self, base_url: str, reason: str, tool: str = "", path: str = "") -> None:
        super().__init__(503, f"the service at {base_url} did not answer: {reason}", tool, path)
        self.base_url = base_url
        self.reason = reason


class BackendError(RefusedCall):
    """The model backend failed (issue #49): it answered an error status, did not answer,
    or does not hold the model asked for. Reported as a 502 with the sentence; no model turn
    was taken on it, so nothing is answered."""

    def __init__(self, detail: str) -> None:
        super().__init__(502, detail)
