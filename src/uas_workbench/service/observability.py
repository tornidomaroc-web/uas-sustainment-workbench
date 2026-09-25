"""Structured JSON logs and Prometheus metrics."""

from __future__ import annotations

import logging


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        raise NotImplementedError
