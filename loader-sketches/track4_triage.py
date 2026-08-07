"""Track 4 — Cross-team signal triage.

Consumes OTHER teams' signals via fetch_signals(), uses ask_claude to
classify each into action/verify/awareness with priority 1-5, and detects
duplicates by title similarity + location proximity.

This is the "meta" track — it doesn't poll external feeds, it reads the
shared signals table and adds triage intelligence.

Platform contract: main(), sample(), tick().
"""

from __future__ import annotations

import json
import math
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from wcc_impact import ask_claude, fetch_signals, publish_signal, register_module, run_every

from enrichment.feed_poller import idempotency_key
from enrichment.signal_helpers import make_signal

# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------

MODULE_ID = "team-CHANGEME"

POLL_INTERVAL = 60  # seconds — triage doesn't need to be as fast
DEDUP_RADIUS_KM = 1.0
TITLE_SIMILARITY_THRESHOLD = 0.6

# Track what we've already triaged (by signal id)
_triaged: set[str] = set()

# ---------------------------------------------------------------------------
# triage helpers
# ---------------------------------------------------------------------------

_TRIAGE_SYSTEM = """You are an emergency operations triage officer for Wellington, NZ.
Given a signal, classify it:
- category: one of "action" (needs immediate response), "verify" (needs confirmation/investigation), "awareness" (informational, no action needed)
- priority: 1 (critical) to 5 (low)
- reason: one sentence explaining the classification

Return ONLY a JSON object with fields: category, priority, reason.
No markdown, no explanation outside the JSON."""


def _triage_signal(signal: dict) -> dict | None:
    """Use ask_claude to classify a signal into action/verify/awareness."""
    prompt = (
        f"Triage this emergency signal:\n"
        f"Title: {signal.get('title', '')}\n"
        f"Type: {signal.get('signal_type', '')}\n"
        f"Severity: {signal.get('severity', '')}\n"
        f"Source: {signal.get('source_type', '')} / {signal.get('source', '')}\n"
        f"Description: {(signal.get('description') or '')[:500]}\n"
        f"Location: {signal.get('place_name', 'unknown')}\n"
    )
    try:
        response = ask_claude(prompt, system=_TRIAGE_SYSTEM, max_tokens=200)
        cleaned = response.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[-1].rsplit("```", 1)[0]
        result = json.loads(cleaned)
        if isinstance(result, dict):
            return result
    except Exception:
        pass
    return None


def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(dlng / 2) ** 2
    )
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _title_similarity(a: str, b: str) -> float:
    """Jaccard similarity on word sets — fast and good enough for dedup."""
    wa = set(a.lower().split())
    wb = set(b.lower().split())
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def _find_duplicates(signal: dict, all_signals: list[dict]) -> list[dict]:
    """Find signals that look like duplicates of this one."""
    dupes = []
    for other in all_signals:
        if other.get("id") == signal.get("id"):
            continue
        # Title similarity
        sim = _title_similarity(
            signal.get("title", ""), other.get("title", "")
        )
        if sim < TITLE_SIMILARITY_THRESHOLD:
            continue
        # Location proximity (if both have coords)
        if (
            signal.get("lat") is not None
            and other.get("lat") is not None
        ):
            dist = _haversine_km(
                signal["lat"], signal["lng"],
                other["lat"], other["lng"],
            )
            if dist > DEDUP_RADIUS_KM:
                continue
        dupes.append(other)
    return dupes


# ---------------------------------------------------------------------------
# tick
# ---------------------------------------------------------------------------


def tick() -> None:
    """Fetch recent signals from all teams, triage new ones."""
    signals = fetch_signals(limit=50)
    if not signals:
        return

    for signal in signals:
        sig_id = signal.get("id") or signal.get("idempotency_key", "")
        if not sig_id:
            continue

        # Skip our own signals and already-triaged ones
        if signal.get("module_id") == MODULE_ID:
            continue
        if sig_id in _triaged:
            continue
        _triaged.add(sig_id)

        # Triage with Claude
        triage = _triage_signal(signal)
        if not triage:
            continue

        category = triage.get("category", "awareness")
        priority = triage.get("priority", 3)
        reason = triage.get("reason", "")

        # Check for duplicates
        dupes = _find_duplicates(signal, signals)
        dupe_note = ""
        if dupes:
            dupe_titles = [d.get("title", "?")[:60] for d in dupes[:3]]
            dupe_note = f"\n\nPossible duplicates ({len(dupes)}): {'; '.join(dupe_titles)}"

        description = (
            f"Triage: {category.upper()} (P{priority})\n"
            f"Reason: {reason}\n"
            f"Original: [{signal.get('signal_type')}] {signal.get('title')}\n"
            f"Source: {signal.get('source_type')} / {signal.get('source', 'unknown')}"
            f"{dupe_note}"
        )

        # Map triage priority to severity
        priority_severity = {1: "extreme", 2: "severe", 3: "moderate", 4: "minor", 5: "minor"}

        sig = make_signal(
            module_id=MODULE_ID,
            title=f"[{category.upper()}/P{priority}] {signal.get('title', 'Signal')}"[:200],
            signal_type="triage-assessment",
            source_type="official",
            source=f"Triage of {signal.get('module_id', 'unknown')}",
            description=description,
            severity=priority_severity.get(priority, "unknown"),
            lat=signal.get("lat"),
            lng=signal.get("lng"),
            place_name=signal.get("place_name"),
            idempotency_key=idempotency_key("triage", sig_id),
            raw={
                "original_signal_id": sig_id,
                "triage": triage,
                "duplicate_count": len(dupes),
            },
        )
        publish_signal(**sig)


# ---------------------------------------------------------------------------
# platform contract
# ---------------------------------------------------------------------------


def sample() -> dict:
    return make_signal(
        module_id=MODULE_ID,
        title="[ACTION/P1] Severe flooding reported on Hutt Road",
        signal_type="triage-assessment",
        source_type="official",
        source="Triage of team-warnings",
        description=(
            "Triage: ACTION (P1)\n"
            "Reason: Severe flooding on major transport route requires immediate response.\n"
            "Original: [flooding] Severe flooding reported on Hutt Road"
        ),
        severity="extreme",
        lat=-41.2270,
        lng=174.8712,
        place_name="Hutt Road",
        idempotency_key=idempotency_key("triage", "sample-1"),
    )


def main() -> None:
    register_module(
        id=MODULE_ID,
        name="Signal Triage",
        icon="🎯",
        description="Cross-team signal triage: action/verify/awareness with priority",
    )
    run_every(POLL_INTERVAL, tick)


if __name__ == "__main__":
    main()
