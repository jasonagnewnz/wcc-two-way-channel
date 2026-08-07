"""Hazard context for a report — inferred, cached, and never load-bearing.

When a report arrives we can say something useful about where it is: which
tsunami evacuation zone, the nearest Community Emergency Hub, the nearest
live river gauge. That is the difference between "a pin on a map" and "a pin
in the Red zone, 300 m from the Aro Valley hub".

Three rules, all deliberate:

1. **It never blocks the reporter.** The lookup takes 3-5 seconds against
   council servers. The reference code is returned instantly and the context
   is filled in behind it. A resident standing in rising water does not wait
   on an ArcGIS round-trip.
2. **It never fails the report.** Any exception is swallowed and the report
   keeps its empty context. Losing the enrichment is an inconvenience;
   losing the report is the thing we are here to prevent.
3. **It is labelled as inferred.** The interface renders it as context, not
   as fact. Presenting something unverified as confirmed is the failure mode
   the problem statements are most wary of.

Also: these are hazard-*planning* layers, not live emergency information.
"""

from __future__ import annotations

import threading

# Cache key is the point rounded to ~11 m. Reports from the same street
# corner reuse one lookup, which matters when a single flood generates
# dozens of reports from one block and every one of them would otherwise hit
# council servers. The README asks us to be considerate with request rates.
_PRECISION = 4

_cache: dict[tuple[float, float], dict] = {}
_cache_lock = threading.Lock()


def _key(lat: float, lng: float) -> tuple[float, float]:
    return (round(lat, _PRECISION), round(lng, _PRECISION))


def lookup(lat: float, lng: float) -> dict:
    """Blocking hazard lookup. Returns {} on any failure."""
    key = _key(lat, lng)
    with _cache_lock:
        if key in _cache:
            return _cache[key]

    try:
        from enrichment.hazard_context import hazard_context
        raw = hazard_context(lat, lng)
    except Exception:
        # Bare except is intentional: no network, a council server throttling
        # under load, an SDK import failure on a machine without wcc_gis --
        # every one of them degrades to "no context", none of them lose a
        # report.
        raw = {}

    hub = raw.get("nearest_emergency_hub") or {}
    context = {
        "tsunami_zone": raw.get("tsunami_zone"),
        "flood_hazard": raw.get("flood_hazard"),
        "liquefaction_risk": raw.get("liquefaction_risk"),
        "deprivation_decile": raw.get("deprivation_decile"),
        "nearest_hub": hub.get("name"),
        "nearest_hub_address": hub.get("address"),
        "eq_prone_building_count": raw.get("eq_prone_building_count"),
        # Stamped on every record so the UI can label it honestly and a
        # reader can tell inference from testimony.
        "inferred": True,
        "source": "WCC / GWRC / GNS hazard-planning layers via wcc_gis",
    }

    with _cache_lock:
        _cache[key] = context
    return context


def lookup_async(lat: float, lng: float, on_done) -> None:
    """Run `lookup` off the request thread and hand the result to `on_done`.

    Daemon thread: a pending lookup must never keep the process alive at the
    end of a demo.
    """
    def run() -> None:
        try:
            on_done(lookup(lat, lng))
        except Exception:
            pass

    threading.Thread(target=run, daemon=True).start()


def summary(context: dict) -> str:
    """One short human line, for the map popup and the reporter's screen."""
    if not context:
        return ""
    bits = []
    zone = context.get("tsunami_zone")
    if zone not in (None, ""):
        bits.append(f"Tsunami evacuation zone {zone}")
    if context.get("flood_hazard"):
        bits.append(f"Flood hazard: {context['flood_hazard']}")
    if context.get("nearest_hub"):
        bits.append(f"Nearest hub: {context['nearest_hub']}")
    return " · ".join(bits)
