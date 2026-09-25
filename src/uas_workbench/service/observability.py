"""Structured JSON logs and Prometheus metrics for the service."""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime

from prometheus_client import Counter, Gauge, Histogram

# Attributes every LogRecord has; anything else on the record is an extra field to emit.
_STANDARD = set(logging.LogRecord("x", 0, "x", 0, "", None, None).__dict__) | {
    "message", "asctime", "taskName"
}  # fmt: skip


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        payload.update({k: v for k, v in record.__dict__.items() if k not in _STANDARD})
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: int = logging.INFO) -> logging.Logger:
    """JSON lines on stderr for everything under the 'uasw' logger; idempotent."""
    logger = logging.getLogger("uasw")
    if not any(isinstance(h.formatter, JsonFormatter) for h in logger.handlers):
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False
    return logger


HTTP_REQUESTS = Counter(
    "uasw_http_requests_total", "HTTP requests handled", ["method", "path", "status"]
)
HTTP_LATENCY = Histogram(
    "uasw_http_request_seconds", "HTTP request latency in seconds", ["method", "path"]
)
FLIGHTS_STORED = Gauge("uasw_flights_stored", "Flight records in the store")
FINDINGS = Gauge("uasw_findings", "Reconciliation findings across the fleet, by kind", ["kind"])
