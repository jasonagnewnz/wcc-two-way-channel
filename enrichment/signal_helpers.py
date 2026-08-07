"""Signal construction helpers: severity mapping, text classification, geocoding.

Usage:
    from enrichment.signal_helpers import (
        cap_to_severity,
        classify_text,
        geocode_text,
        make_signal,
    )

    # Map CAP severity terms to signal schema enum
    severity = cap_to_severity("Severe")  # -> "severe"

    # Classify free text into signal fields using ask_claude
    fields = classify_text("Large slip blocking Ngauranga Gorge")
    # -> {"signal_type": "landslide", "severity": "severe",
    #     "place_name": "Ngauranga Gorge"}

    # Build a validated signal dict
    sig = make_signal(
        module_id="team-example",
        title="Slip on Ngauranga Gorge",
        signal_type="landslide",
        source_type="community",
    )
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from wcc_impact import ask_claude, geocode, SEVERITIES, SOURCE_TYPES


# ---------------------------------------------------------------------------
# severity mapping
# ---------------------------------------------------------------------------

# CAP severity terms -> signal schema enum values
_CAP_MAP: dict[str, str] = {
    # Standard CAP
    "extreme": "extreme",
    "severe": "severe",
    "moderate": "moderate",
    "minor": "minor",
    "unknown": "unknown",
    # MetService / common wording
    "red": "extreme",
    "orange": "severe",
    "yellow": "moderate",
    "watch": "moderate",
    "warning": "severe",
    "advisory": "minor",
    "outlook": "minor",
    # NZTA / generic
    "high": "severe",
    "medium": "moderate",
    "low": "minor",
    "critical": "extreme",
    "significant": "severe",
}


def cap_to_severity(term: str | None) -> str:
    """Map a CAP/MetService/generic severity term to the signal schema enum.

    Case-insensitive. Returns "unknown" for unrecognised terms.
    """
    if not term:
        return "unknown"
    return _CAP_MAP.get(term.strip().lower(), "unknown")


# ---------------------------------------------------------------------------
# Claude-based text classification
# ---------------------------------------------------------------------------

_CLASSIFY_SYSTEM = """You extract structured emergency information from free text.
Return ONLY a JSON object with these fields (omit any you can't determine):
- signal_type: kebab-case category (e.g. flooding, road-closure, power-outage,
  landslide, earthquake, tsunami-warning, community-report, weather-warning)
- severity: one of minor, moderate, severe, extreme, unknown
- place_name: the Wellington place name mentioned, if any
- lat: approximate latitude if you can infer it from the place name (Wellington region)
- lng: approximate longitude
No explanation, just the JSON object."""


def classify_text(text: str) -> dict:
    """Use ask_claude to extract signal_type, severity, and place from free text.

    Returns a dict with available fields. Falls back to empty dict on failure.
    """
    try:
        response = ask_claude(
            f"Extract emergency signal fields from this text:\n\n{text[:1500]}",
            system=_CLASSIFY_SYSTEM,
            max_tokens=256,
        )
        # Strip markdown fences if present
        cleaned = response.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[-1].rsplit("```", 1)[0]
        result = json.loads(cleaned)
        if not isinstance(result, dict):
            return {}

        # Validate severity
        if result.get("severity") and result["severity"] not in SEVERITIES:
            result["severity"] = cap_to_severity(result["severity"])

        return result
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# geocoding helper
# ---------------------------------------------------------------------------

def geocode_text(place_name: str) -> tuple[float, float] | None:
    """Geocode a Wellington place name to (lat, lng).

    Wraps wcc_impact.geocode with common cleanup: strips "near", "at",
    "around" prefixes and trailing punctuation.
    """
    if not place_name:
        return None

    # Clean up common prefixes/suffixes
    cleaned = place_name.strip().rstrip(".,;:")
    for prefix in ("near ", "at ", "around ", "in ", "on ", "by "):
        if cleaned.lower().startswith(prefix):
            cleaned = cleaned[len(prefix):]

    result = geocode(cleaned)
    if result:
        return result

    # Fallback: try just the first two words (suburb name)
    words = cleaned.split()
    if len(words) > 2:
        return geocode(" ".join(words[:2]))

    return None


# ---------------------------------------------------------------------------
# signal builder
# ---------------------------------------------------------------------------

# Schema limits from signal.schema.json
_TITLE_MAX = 200
_DESC_MAX = 2000
_SOURCE_MAX = 200
_IDEM_MAX = 200
_SIGNAL_TYPE_MAX = 100


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
    idempotency_key: str | None = None,
    media_urls: list[str] | None = None,
) -> dict:
    """Build a signal dict, validated and truncated to schema limits.

    Raises ValueError if required fields are missing or source_type is invalid.
    Truncates title/description/source to their max lengths rather than failing.
    """
    if not title:
        raise ValueError("title is required")
    if not signal_type:
        raise ValueError("signal_type is required")
    if not module_id:
        raise ValueError("module_id is required")
    if source_type not in SOURCE_TYPES:
        raise ValueError(f"source_type must be one of {SOURCE_TYPES}, got {source_type!r}")

    # Validate severity
    if severity not in SEVERITIES:
        severity = cap_to_severity(severity)

    # Validate confidence
    if confidence is not None:
        confidence = max(0.0, min(1.0, float(confidence)))

    # Validate lat/lng
    if lat is not None:
        lat = max(-90.0, min(90.0, float(lat)))
    if lng is not None:
        lng = max(-180.0, min(180.0, float(lng)))

    # Convert datetimes to ISO strings
    if isinstance(observed_at, datetime):
        observed_at = observed_at.astimezone(timezone.utc).isoformat()
    if isinstance(reported_at, datetime):
        reported_at = reported_at.astimezone(timezone.utc).isoformat()

    sig: dict = {
        "module_id": module_id,
        "title": title[:_TITLE_MAX],
        "signal_type": signal_type[:_SIGNAL_TYPE_MAX],
        "source_type": source_type,
        "severity": severity,
    }

    if source:
        sig["source"] = source[:_SOURCE_MAX]
    if description:
        sig["description"] = description[:_DESC_MAX]
    if lat is not None:
        sig["lat"] = lat
    if lng is not None:
        sig["lng"] = lng
    if place_name:
        sig["place_name"] = place_name[:_SOURCE_MAX]
    if confidence is not None:
        sig["confidence"] = confidence
    if link:
        sig["link"] = link[:2000]
    if observed_at:
        sig["observed_at"] = observed_at
    if reported_at:
        sig["reported_at"] = reported_at
    if raw:
        sig["raw"] = raw
    if idempotency_key:
        sig["idempotency_key"] = idempotency_key[:_IDEM_MAX]
    if media_urls:
        sig["media_urls"] = media_urls

    return sig
