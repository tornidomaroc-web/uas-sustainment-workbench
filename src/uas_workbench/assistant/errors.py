"""The refusals that end a run of the assistant.

A run ends where a read or a model turn fails, so the model is never handed a refusal, a
failed read or a partial result to answer from, and no answer, grounding or recording can
carry it. Every refusal is a `RefusedCall`: a status, a sentence, and `str()` is the one line
every `uasw` command prints for a refusal, `refused (<status>): <sentence>`.
"""

from __future__ import annotations

from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit

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


def shown_url(url: str) -> str:
    """A URL as a refusal line shows it: scheme, host, port and path. Userinfo, the query and
    the fragment are dropped, since a credential or a token can stand in any of them, and the
    line goes to a terminal, a log or a ticket."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc.rpartition("@")[2], parts.path, "", ""))


def scrubbed(text: str, url: str) -> str:
    """`text` with the parts of `url` that `shown_url` drops replaced, for an error message
    from the standard library that repeats the URL it was given."""
    parts = urlsplit(url)
    for secret in (parts.netloc.rpartition("@")[0], parts.password, parts.query, parts.fragment):
        if secret:
            text = text.replace(secret, "<redacted>")
    return text


class RefusedCall(Exception):
    """The service answered a tool call with an error status (issue #46): the run ends here.

    The model is not asked again, so it never holds a refusal it could phrase as a value or
    build an answer on, and no answer, grounding or recording can carry the call. `detail` is
    the service's own sentence (a 503 names the stored record that cannot be read and points
    to `uasw verify`; a 404 names the aircraft the model asked for), whole; `str()` is the
    line every `uasw` command prints for a refusal, `refused (<status>): <detail>`, or with
    `label` in place of a status when no HTTP status was received."""

    def __init__(
        self, status: int, detail: str, tool: str = "", path: str = "", *, label: str = ""
    ) -> None:
        super().__init__(f"refused ({label or status}): {detail}")
        self.status = status
        self.detail = detail
        self.tool = tool
        self.path = path


class ServiceUnreachable(RefusedCall):
    """The service did not answer a tool call (issue #49): nothing listens at the URL, the
    host does not resolve, or the connection timed out, was reset or carried no HTTP reply.

    No HTTP status was received, so none is shown: the line is `refused (unreachable): ...`
    and `status` is 0. A 503 is what the service itself sends for a stored record it cannot
    read, a different fault with a different remedy, and an operator or a script reading the
    line must be able to tell the two apart. The URL is shown without userinfo, query or
    fragment, and the reason is scrubbed of them; no record was read."""

    def __init__(self, base_url: str, reason: str, tool: str = "", path: str = "") -> None:
        shown = shown_url(base_url)
        said = scrubbed(reason, base_url)
        detail = f"the service at {shown} did not answer: {said}"
        super().__init__(0, detail, tool, path, label="unreachable")
        self.base_url = shown
        self.reason = said


class BackendError(RefusedCall):
    """The model backend failed (issue #49): it answered an error status, did not answer,
    or does not hold the model asked for. Reported as a 502 with the sentence; no model turn
    was taken on it, so nothing is answered."""

    def __init__(self, detail: str) -> None:
        super().__init__(502, detail)
