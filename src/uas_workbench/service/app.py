"""FastAPI application over a Store."""

from __future__ import annotations

from fastapi import FastAPI

from .store import Store


def create_app(store: Store) -> FastAPI:
    raise NotImplementedError
