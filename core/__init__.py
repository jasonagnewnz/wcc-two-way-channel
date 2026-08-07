"""Core of the two-way channel: schema, append-only store, report loop."""

from .reports import (
    ISSUE_TYPES,
    MODULE_ID,
    RECEIVED,
    REPORT_TYPE,
    RESOLVED,
    RESPONDING,
    REVIEWING,
    STATUS_LABELS,
    STATUS_TYPE,
    STATUSES,
    ReportService,
)
from .signals import SEVERITIES, SOURCE_TYPES, make_signal, utc_now
from .store import SignalStore, new_reference

__all__ = [
    "ISSUE_TYPES", "MODULE_ID", "RECEIVED", "REPORT_TYPE", "RESOLVED",
    "RESPONDING", "REVIEWING", "STATUSES", "STATUS_LABELS", "STATUS_TYPE",
    "ReportService", "SEVERITIES", "SOURCE_TYPES", "SignalStore",
    "make_signal", "new_reference", "utc_now",
]
