"""Track 2 — Community reports from scenario social feed.

Polls the scenario social feed, classifies each post with ask_claude,
geocodes any place name, and publishes community-report signals.

NOTE: The UI side of this track needs a manual reporting form (React).
This loader handles the automated social-feed pipeline only.

Platform contract: main(), sample(), tick().
"""

from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from wcc_impact import publish_signal, register_module, run_every

from enrichment.feed_poller import FeedPoller, idempotency_key
from enrichment.signal_helpers import classify_text, geocode_text, make_signal
from enrichment.hazard_context import hazard_summary

# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------

MODULE_ID = "team-CHANGEME"

SCENARIO_SOCIAL_URL = (
    "https://impact-lab-wlg-2026-08-08.vercel.app/api/scenario/social"
)

POLL_INTERVAL = 15  # seconds — social moves fast

poller = FeedPoller()

# ---------------------------------------------------------------------------
# tick
# ---------------------------------------------------------------------------


def tick() -> None:
    """Poll social feed, classify, geocode, publish."""
    items = poller.poll_json(
        SCENARIO_SOCIAL_URL,
        id_field="id",
        items_field="items",
        source_name="scenario-social",
    )

    for item in items:
        text = item.get("text") or item.get("body") or item.get("content", "")
        if not text:
            continue

        # Classify with Claude
        fields = classify_text(text)
        signal_type = fields.get("signal_type", "community-report")
        severity = fields.get("severity", "unknown")

        # Try to get location
        lat = item.get("lat") or fields.get("lat")
        lng = item.get("lng") or fields.get("lng")
        place_name = item.get("place") or fields.get("place_name")

        # Geocode if we have a place but no coords
        if place_name and (lat is None or lng is None):
            coords = geocode_text(place_name)
            if coords:
                lat, lng = coords

        # Enrich with hazard context if we have a location
        description = text
        if lat is not None and lng is not None:
            try:
                ctx = hazard_summary(lat, lng)
                if ctx and ctx != "no hazard data at this location":
                    description = f"{text}\n\nHazard context: {ctx}"
            except Exception:
                pass

        title = text[:200]
        author = item.get("author") or item.get("user") or item.get("handle", "")
        if author:
            title = f"@{author}: {text}"[:200]

        sig = make_signal(
            module_id=MODULE_ID,
            title=title,
            signal_type=signal_type,
            source_type="community",
            source="Social feed (scenario)",
            description=description,
            severity=severity,
            lat=lat,
            lng=lng,
            place_name=place_name,
            observed_at=item.get("time") or item.get("created_at"),
            idempotency_key=idempotency_key("social", item["id"]),
            raw=item,
        )
        publish_signal(**sig)


# ---------------------------------------------------------------------------
# platform contract
# ---------------------------------------------------------------------------


def sample() -> dict:
    return make_signal(
        module_id=MODULE_ID,
        title="@local_resident: Water coming over the road on Hutt Rd near Petone",
        signal_type="flooding",
        source_type="community",
        source="Social feed (scenario)",
        description="Water coming over the road on Hutt Rd near Petone",
        severity="moderate",
        lat=-41.2270,
        lng=174.8712,
        place_name="Petone",
        idempotency_key=idempotency_key("social", "sample-1"),
    )


def main() -> None:
    register_module(
        id=MODULE_ID,
        name="Community Reports",
        icon="📢",
        description="Social feed classification and community reports",
    )
    run_every(POLL_INTERVAL, tick)


if __name__ == "__main__":
    main()
