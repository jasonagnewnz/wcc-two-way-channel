"""Spatial hazard context for any point in Wellington.

Wraps wcc_gis spatial queries into a single hazard_context(lat, lng) call
returning tsunami zone, liquefaction risk, flood hazard, fault proximity,
deprivation decile, nearby earthquake-prone buildings, nearest community
emergency hub, and nearest live gauge reading.

Usage:
    from enrichment.hazard_context import hazard_context, hazard_summary

    ctx = hazard_context(-41.2865, 174.7762)
    print(ctx["tsunami_zone"])       # e.g. "Orange" or None
    print(ctx["liquefaction_risk"])   # e.g. "Moderate damage is possible"
    print(hazard_summary(-41.2865, 174.7762))
"""

from __future__ import annotations

import math

import wcc_gis


# ---------------------------------------------------------------------------
# point-in-polygon lookups (wcc_gis at= queries)
# ---------------------------------------------------------------------------

def _first_attr(dataset: str, lat: float, lng: float, field: str) -> str | None:
    """Return the value of `field` from the first feature containing (lat, lng)."""
    try:
        rows = wcc_gis.features(dataset, at=(lat, lng), limit=1)
        return rows[0][field] if rows else None
    except wcc_gis.GisError:
        return None


def _tsunami_zone(lat: float, lng: float) -> str | None:
    """Tsunami evacuation zone colour at this point (Red/Orange/Yellow)."""
    return _first_attr("tsunami-evacuation-zones", lat, lng, "Zone_Class")


def _liquefaction_risk(lat: float, lng: float) -> str | None:
    """Liquefaction vulnerability description at this point."""
    return _first_attr("liquefaction-vulnerability", lat, lng, "Category")


# `flood-hazard-areas` is a whole SERVICE, not a layer. Querying it without a
# layer= raises GisError, which _first_attr swallows — so this returned None
# for every point in Wellington, silently, forever. Layer 3 is the 1% AEP
# extent (the standard 100-year flood) and the field is `Label`, not the
# guessed `Hazard_Class`. Verified against the live service 2026-08-08.
FLOOD_LAYER = 3


def _flood_hazard(lat: float, lng: float) -> str | None:
    """Flood hazard classification at this point (1% AEP extent)."""
    try:
        rows = wcc_gis.features("flood-hazard-areas", layer=FLOOD_LAYER,
                                at=(lat, lng), limit=1)
        if not rows:
            return None
        row = rows[0]
        return row.get("Label") or row.get("Title") or row.get("Description")
    except wcc_gis.GisError:
        return None


def _fault_zone(lat: float, lng: float, radius_m: int = 500) -> dict | None:
    """Nearest active fault within radius_m, if any."""
    try:
        rows = wcc_gis.features("active-faults", near=(lat, lng, radius_m), limit=1)
        if not rows:
            return None
        r = rows[0]
        return {
            "name": r.get("Name") or r.get("FaultName"),
            "distance_m": radius_m,  # upper bound — actual may be closer
            "slip_rate": r.get("Slip_Rate") or r.get("SlipRate"),
        }
    except wcc_gis.GisError:
        return None


def _deprivation_decile(lat: float, lng: float) -> int | None:
    """NZDep2023 deprivation decile (1 = least deprived, 10 = most)."""
    try:
        # NZDep2023 feature server — SA1-level polygons
        rows = wcc_gis.features("deprivation-2023", at=(lat, lng), limit=1)
        if not rows:
            return None
        # The field name varies; try common variants
        for field in ("NZDep2023", "Decile", "NZDep2023_Decile", "SA12023_V1"):
            val = rows[0].get(field)
            if val is not None:
                try:
                    return int(val)
                except (ValueError, TypeError):
                    continue
        return None
    except wcc_gis.GisError:
        return None


# ---------------------------------------------------------------------------
# radius queries (wcc_gis near= queries)
# ---------------------------------------------------------------------------

def _nearby_eq_prone_buildings(lat: float, lng: float, radius_m: int = 500) -> list[dict]:
    """Earthquake-prone buildings within radius_m."""
    try:
        rows = wcc_gis.features(
            "earthquake-prone-buildings",
            near=(lat, lng, radius_m),
            limit=10,
        )
        return [
            {
                "address": r.get("Address") or r.get("STREET_ADDRESS"),
                "rating": r.get("EQ_Rating") or r.get("Building_Status"),
                "lat": r.get("lat"),
                "lng": r.get("lng"),
            }
            for r in rows
        ]
    except wcc_gis.GisError:
        return []


def _haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in metres."""
    radius = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lng2 - lng1)
    a = (math.sin(dphi / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dlam / 2) ** 2)
    return 2 * radius * math.asin(math.sqrt(a))


def _nearest_emergency_hub(lat: float, lng: float, radius_m: int = 5000) -> dict | None:
    """Nearest community emergency hub within radius_m.

    A `near=` query returns features *within* the radius in whatever order
    the service feels like — it is a spatial filter, not a sort. Asking for
    `limit=1` therefore returns an arbitrary hub inside 5 km, not the closest
    one: a Newtown report was told its nearest hub was Aro Valley, roughly
    2 km further away than the correct answer.

    So: take the candidates, and do the sorting here. Telling someone the
    wrong place to walk to during an emergency is not a cosmetic bug.
    """
    try:
        rows = wcc_gis.features(
            "community-emergency-hubs",
            near=(lat, lng, radius_m),
            limit=60,
        )
        if not rows:
            return None
        located = [r for r in rows if r.get("lat") is not None and r.get("lng") is not None]
        if not located:
            return None
        r = min(located, key=lambda h: _haversine_m(lat, lng, h["lat"], h["lng"]))
        # The live layer publishes NAME/ADDRESS/SUBURB in upper case. The
        # original guesses (Name/HubName/FACILITY) all miss, so every hub came
        # back named `None` — verified against the real service 2026-08-08.
        # Upper case first, the old guesses kept as fallbacks in case the
        # layer's schema differs on the day.
        return {
            "name": r.get("NAME") or r.get("Name") or r.get("HubName") or r.get("FACILITY"),
            "address": r.get("ADDRESS") or r.get("Address") or r.get("LOCATION"),
            "suburb": r.get("SUBURB") or r.get("TOWN"),
            "lat": r.get("lat"),
            "lng": r.get("lng"),
        }
    except wcc_gis.GisError:
        return None


# ---------------------------------------------------------------------------
# live telemetry — nearest gauge reading
# ---------------------------------------------------------------------------

def _nearest_gauge_reading(lat: float, lng: float) -> dict | None:
    """Nearest Hilltop gauge with its most recent reading.

    Scans cached gauge locations for the closest by naive Euclidean distance
    (adequate within Wellington's extent). Returns the latest Stage reading.
    """
    try:
        sites = wcc_gis.hilltop_sites()
    except wcc_gis.GisError:
        return None

    if not sites:
        return None

    best = None
    best_dist = float("inf")
    for s in sites:
        if s.get("lat") is None or s.get("lng") is None:
            continue
        d = (s["lat"] - lat) ** 2 + (s["lng"] - lng) ** 2
        if d < best_dist:
            best_dist = d
            best = s

    if best is None:
        return None

    result = {
        "site": best["site"],
        "lat": best["lat"],
        "lng": best["lng"],
    }

    # Try to get the latest reading — Stage first, then Flow
    for measurement in ("Stage", "Flow", "Rainfall"):
        try:
            readings = wcc_gis.hilltop_data(best["site"], measurement, interval="PT1H")
            if readings:
                r = readings[-1]
                result["measurement"] = measurement
                result["value"] = r["value"]
                result["units"] = r["units"]
                result["time"] = r["time"]
                break
        except wcc_gis.GisError:
            continue

    return result


# ---------------------------------------------------------------------------
# composite function
# ---------------------------------------------------------------------------

def hazard_context(lat: float, lng: float) -> dict:
    """Full hazard context for a point. Returns a dict with all available layers.

    Every value is None when the layer doesn't cover the point or the query
    fails — callers should handle missing data gracefully.
    """
    fault = _fault_zone(lat, lng)
    eq_buildings = _nearby_eq_prone_buildings(lat, lng)
    hub = _nearest_emergency_hub(lat, lng)
    gauge = _nearest_gauge_reading(lat, lng)

    return {
        "lat": lat,
        "lng": lng,
        "tsunami_zone": _tsunami_zone(lat, lng),
        "liquefaction_risk": _liquefaction_risk(lat, lng),
        "flood_hazard": _flood_hazard(lat, lng),
        "fault_zone": fault,
        "deprivation_decile": _deprivation_decile(lat, lng),
        "eq_prone_buildings_nearby": eq_buildings,
        "eq_prone_building_count": len(eq_buildings),
        "nearest_emergency_hub": hub,
        "nearest_gauge": gauge,
    }


def hazard_summary(lat: float, lng: float) -> str:
    """One-line human-readable hazard summary for a location."""
    ctx = hazard_context(lat, lng)
    parts = []

    if ctx["tsunami_zone"]:
        parts.append(f"tsunami:{ctx['tsunami_zone']}")
    if ctx["liquefaction_risk"]:
        parts.append(f"liquefaction:{ctx['liquefaction_risk']}")
    if ctx["flood_hazard"]:
        parts.append(f"flood:{ctx['flood_hazard']}")
    if ctx["fault_zone"]:
        parts.append(f"fault:<{ctx['fault_zone']['distance_m']}m")
    if ctx["deprivation_decile"]:
        parts.append(f"deprivation:D{ctx['deprivation_decile']}")
    if ctx["eq_prone_building_count"]:
        parts.append(f"EQP-buildings:{ctx['eq_prone_building_count']}")
    if ctx["nearest_emergency_hub"]:
        parts.append(f"hub:{ctx['nearest_emergency_hub']['name']}")
    if ctx["nearest_gauge"] and ctx["nearest_gauge"].get("value") is not None:
        g = ctx["nearest_gauge"]
        parts.append(f"gauge:{g['site']}={g['value']}{g.get('units','')}")

    return " | ".join(parts) if parts else "no hazard data at this location"
