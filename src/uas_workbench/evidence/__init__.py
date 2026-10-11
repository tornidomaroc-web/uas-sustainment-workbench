"""A draft evidence pack for OSO #03, and a draft records summary against UAS.SPEC.050(1)(g),
per aircraft and for the fleet: what the records hold, labelled, never a finding."""

from .fleet_records import FLEET_RECORDS_SECTIONS, build_fleet_records
from .fleet_records_html import render_fleet_records_html
from .html import render_html
from .pack import SECTIONS, build_pack, ledger_hash
from .records import (
    RECORDS_ITEMS,
    RECORDS_SECTIONS,
    RECORDS_SOURCES,
    RECORDS_STATEMENT,
    RECORDS_STATUSES,
    RECORDS_TITLE,
    RETENTION_YEARS,
    SPAN_STATUSES,
    build_records,
    years_before,
)
from .records_html import render_records_html
from .sources import OSO_ITEMS, SOURCES, STATEMENT, VERSION_NOTE

__all__ = [
    "FLEET_RECORDS_SECTIONS",
    "OSO_ITEMS",
    "RECORDS_ITEMS",
    "RECORDS_SECTIONS",
    "RECORDS_SOURCES",
    "RECORDS_STATEMENT",
    "RECORDS_STATUSES",
    "RECORDS_TITLE",
    "RETENTION_YEARS",
    "SECTIONS",
    "SOURCES",
    "SPAN_STATUSES",
    "STATEMENT",
    "VERSION_NOTE",
    "build_fleet_records",
    "build_pack",
    "build_records",
    "ledger_hash",
    "render_fleet_records_html",
    "render_html",
    "render_records_html",
    "years_before",
]
