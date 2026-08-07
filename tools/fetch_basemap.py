#!/usr/bin/env python3
"""Bake the map backdrop into web/data/basemap.json.

    python3 tools/fetch_basemap.py

Pulls two real WCC layers via wcc_gis and writes a trimmed GeoJSON the
browser can draw with no external map library and no network:

  - tsunami-evacuation-zones   the coloured zones people are told to leave
  - community-emergency-hubs   where communities gather and report from

Why bake rather than fetch live
-------------------------------
Three reasons, in order of how much they would hurt during a four-minute
demo. The venue wifi might not hold. Council servers throttle under
concurrent load and the README asks us to be considerate. And a raw pull of
the tsunami zones is 2.7 MB, which is a slow first paint on a phone.

Trimming: coordinates to 4 decimal places (~11 m, far finer than a city map
needs) and only the handful of properties the UI actually renders. That
turns 2.7 MB into something a phone loads instantly.

Re-run it whenever you want fresher geometry. The output is committed, so a
fresh clone works offline.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import wcc_gis  # noqa: E402

OUT = ROOT / "web" / "data" / "basemap.json"
PRECISION = 4


def round_coords(node):
    """Walk a GeoJSON coordinate tree, rounding every number."""
    if isinstance(node, (int, float)):
        return round(float(node), PRECISION)
    if isinstance(node, list):
        return [round_coords(item) for item in node]
    return node


# ~11 m at Wellington's latitude. Finer than a city-scale map can show, and
# still removes most of the vertices.
#
# 0.0003 (~33 m) was tried first and made the file smaller, but six of the
# nineteen tsunami zones collapsed below a valid ring and vanished from the
# map entirely. A backdrop that silently loses a third of the evacuation
# zones is worse than a backdrop that is 40 KB larger, so this is deliberately
# conservative. The script prints a warning if any feature is still dropped.
TOLERANCE = 0.0001


def _simplify_ring(points: list, tolerance: float) -> list:
    """Douglas-Peucker. Iterative rather than recursive: a coastline ring can
    be thousands of points deep and Python's recursion limit is 1000.
    """
    if len(points) < 3:
        return points

    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]

    while stack:
        start, end = stack.pop()
        if end <= start + 1:
            continue

        ax, ay = points[start][0], points[start][1]
        bx, by = points[end][0], points[end][1]
        dx, dy = bx - ax, by - ay
        span = dx * dx + dy * dy

        worst, worst_i = -1.0, start
        for i in range(start + 1, end):
            px, py = points[i][0], points[i][1]
            if span == 0:
                dist = (px - ax) ** 2 + (py - ay) ** 2
            else:
                # Perpendicular distance, squared, without the sqrt.
                cross = abs(dy * px - dx * py + bx * ay - by * ax)
                dist = (cross * cross) / span
            if dist > worst:
                worst, worst_i = dist, i

        if worst > tolerance * tolerance:
            keep[worst_i] = True
            stack.append((start, worst_i))
            stack.append((worst_i, end))

    return [p for p, k in zip(points, keep) if k]


def simplify_geometry(geom_type: str, coords, tolerance: float = TOLERANCE):
    """Simplify Polygon / MultiPolygon rings, dropping any that collapse.

    A ring needs four positions to be a valid closed polygon. Anything that
    simplifies below that was too small to see anyway.
    """
    if geom_type == "Polygon":
        rings = [_simplify_ring(r, tolerance) for r in coords]
        return [r for r in rings if len(r) >= 4]
    if geom_type == "MultiPolygon":
        polys = [simplify_geometry("Polygon", p, tolerance) for p in coords]
        return [p for p in polys if p]
    return coords


def zone_layer() -> list[dict]:
    print("  tsunami-evacuation-zones ...", end=" ", flush=True)
    started = time.time()
    collection = wcc_gis.geojson("tsunami-evacuation-zones", bbox=wcc_gis.WELLINGTON)
    features = []
    dropped = 0
    for feature in collection.get("features", []):
        props = feature.get("properties") or {}
        geom_type = feature["geometry"]["type"]
        coords = simplify_geometry(geom_type, feature["geometry"]["coordinates"])
        if not coords:
            dropped += 1
            continue
        features.append({
            "type": "Feature",
            "geometry": {
                "type": geom_type,
                "coordinates": round_coords(coords),
            },
            "properties": {
                "layer": "tsunami-zone",
                # Col_Code is the colour WCC publishes the zone as; Evac_Zone
                # is the instruction that goes with it.
                "colour": (props.get("Col_Code") or "").lower(),
                "zone": props.get("Evac_Zone"),
                "location": props.get("Location"),
            },
        })
    print(f"{len(features)} zones in {time.time() - started:.1f}s")
    if dropped:
        print(f"     ⚠  {dropped} zone(s) collapsed at TOLERANCE={TOLERANCE} "
              f"and are NOT on the map. Lower the tolerance.")
    return features


def hub_layer() -> list[dict]:
    print("  community-emergency-hubs ...", end=" ", flush=True)
    started = time.time()
    collection = wcc_gis.geojson("community-emergency-hubs", bbox=wcc_gis.WELLINGTON)
    features = []
    for feature in collection.get("features", []):
        props = feature.get("properties") or {}
        features.append({
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": round_coords(feature["geometry"]["coordinates"]),
            },
            "properties": {
                "layer": "hub",
                # Upper case: the live layer publishes NAME/ADDRESS/SUBURB.
                # Guessing at "Name" returns None for all 60 of them.
                "name": props.get("NAME"),
                "address": props.get("ADDRESS"),
                "suburb": props.get("SUBURB"),
            },
        })
    print(f"{len(features)} hubs in {time.time() - started:.1f}s")
    return features


def main() -> int:
    print("\nFetching WCC layers (live, from council servers):")
    features = zone_layer() + hub_layer()

    payload = {
        "type": "FeatureCollection",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "attribution": (
            "Tsunami evacuation zones and Community Emergency Hubs: "
            "Wellington City Council / Greater Wellington Regional Council. "
            "Hazard-planning layers, not live emergency information."
        ),
        "features": features,
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    size_kb = OUT.stat().st_size / 1024
    print(f"\n  wrote {OUT.relative_to(ROOT)}  ({size_kb:.0f} KB, {len(features)} features)\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
