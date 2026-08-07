"""Signal schema — standalone.

The platform's signal schema, implemented with nothing but the standard
library. This is a deliberate duplicate of `enrichment/signal_helpers.py`
from the prep kit: that module does `from wcc_impact import ...` at import
time, so it cannot be imported at all on a machine without the platform SDK
— which is every machine until the SDK is handed out on the day.

Keeping our own copy means `python3 run.py` works on a bare checkout. The
field names, limits and enums are identical, so a signal built here is
accepted by `publish_signal()` unchanged.

Reference: reference/platform_cheatsheet.md, "Signal Schema".
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

SEVERITIES = frozenset({"minor", "moderate", "severe", "extreme", "unknown"})
SOURCE_TYPES = frozenset({"official", "community", "media", "sensor"})

TITLE_MAX = 200
DESC_MAX = 2000
SOURCE_MAX = 200
IDEM_MAX = 200
SIGNAL_TYPE_MAX = 100
LINK_MAX = 2000


def utc_now() -> str:
    """Timezone-aware UTC, ISO 8601. Never naive — naive timestamps against a
    +12/+13 local clock silently corrupt ordering, which matters when the
    output is an audit trail of who knew what, when.
    """
    return datetime.now(timezone.utc).isoformat()


def idempotency_key(*parts: str) -> str:
    """Stable key from its parts, so a restart mid-poll does not duplicate a
    signal. Hashed rather than concatenated to stay inside the 200-char cap
    regardless of input length.
    """
    joined = "|".join(str(p) for p in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:32]


def make_signal(
    *,
    module_id: str,
    title: str,
    signal_type: str,
    source_type: str,
    source: str | None = None,
    description: str | None = None,
    lat: float | None = None,
    lng: float | None = None,
    place_name: str | None = None,
    severity: str = "unknown",
    confidence: float | None = None,
    link: str | None = None,
    observed_at: str | datetime | None = None,
    reported_at: str | datetime | None = None,
    raw: dict | None = None,
    idempotency_key_: str | None = None,
    media_urls: list[str] | None = None,
) -> dict:
    """Build a schema-valid signal dict.

    Raises ValueError on a missing required field or a bad enum. Truncates
    over-long free text rather than raising — rule 6 in the cheatsheet:
    "truncate, don't crash". A report that arrives during an emergency should
    never be rejected for being wordy.
    """
    if not module_id:
        raise ValueError("module_id is required")
    if not title:
        raise ValueError("title is required")
    if not signal_type:
        raise ValueError("signal_type is required")
    if source_type not in SOURCE_TYPES:
        raise ValueError(
            f"source_type must be one of {sorted(SOURCE_TYPES)}, got {source_type!r}")
    if severity not in SEVERITIES:
        raise ValueError(
            f"severity must be one of {sorted(SEVERITIES)}, got {severity!r}")

    if confidence is not None:
        confidence = max(0.0, min(1.0, float(confidence)))
    if lat is not None:
        lat = max(-90.0, min(90.0, float(lat)))
    if lng is not None:
        lng = max(-180.0, min(180.0, float(lng)))

    if isinstance(observed_at, datetime):
        observed_at = observed_at.astimezone(timezone.utc).isoformat()
    if isinstance(reported_at, datetime):
        reported_at = reported_at.astimezone(timezone.utc).isoformat()

    sig: dict = {
        "module_id": module_id,
        "title": title[:TITLE_MAX],
        "signal_type": signal_type[:SIGNAL_TYPE_MAX],
        "source_type": source_type,
        "severity": severity,
    }

    optional = {
        "source": source[:SOURCE_MAX] if source else None,
        "description": description[:DESC_MAX] if description else None,
        "lat": lat,
        "lng": lng,
        "place_name": place_name[:SOURCE_MAX] if place_name else None,
        "confidence": confidence,
        "link": link[:LINK_MAX] if link else None,
        "observed_at": observed_at,
        "reported_at": reported_at,
        "raw": raw,
        "idempotency_key": idempotency_key_[:IDEM_MAX] if idempotency_key_ else None,
        "media_urls": media_urls,
    }
    for key, value in optional.items():
        if value is not None:
            sig[key] = value

    return sig
