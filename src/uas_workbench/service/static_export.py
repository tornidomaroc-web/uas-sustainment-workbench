"""Export the stored fleet, its flights and findings as a static site (JSON + one page)."""

from __future__ import annotations

from pathlib import Path

from .store import Store


def export_static(store: Store, out_dir: Path) -> Path:
    raise NotImplementedError
