"""Deduplicating feed poller for scenario feeds, ArcGIS endpoints, and RSS.

Tracks seen item IDs across polls so the same upstream item is never processed
twice within a loader's lifetime. Pair with idempotency_key() for database-level
dedup across restarts.

Usage:
    from enrichment.feed_poller import FeedPoller, idempotency_key

    poller = FeedPoller()

    # Scenario JSON feed (weather warnings, social posts)
    new_items = poller.poll_json(
        "https://impact-lab-wlg-2026-08-08.vercel.app/api/scenario/weather",
        id_field="id",
    )

    # ArcGIS FeatureServer GeoJSON
    new_features = poller.poll_geojson(
        "https://services5.arcgis.com/.../FeatureServer/0",
        id_field="OBJECTID",
    )

    # RSS feed (e.g. RNZ)
    new_entries = poller.poll_rss(
        "https://www.rnz.co.nz/rss/national.xml",
    )

    # Stable idempotency key for the signals table
    key = idempotency_key("weather-feed", item["id"])
"""

from __future__ import annotations

import hashlib
import json
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any


USER_AGENT = "impact-lab-loader/1.0 (Wellington hackathon)"
TIMEOUT = 20


def idempotency_key(source: str, item_id: str | int) -> str:
    """Stable idempotency key for publish_signal().

    Format: source::item_id, truncated to 200 chars (schema limit).
    """
    key = f"{source}::{item_id}"
    if len(key) > 200:
        # Hash the item_id to stay within limits
        h = hashlib.sha256(str(item_id).encode()).hexdigest()[:24]
        key = f"{source}::{h}"
    return key[:200]


def _fetch(url: str, params: dict | None = None) -> bytes:
    """Fetch URL with timeout and user-agent."""
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read()


def _fetch_json(url: str, params: dict | None = None) -> Any:
    body = _fetch(url, params)
    return json.loads(body)


class FeedPoller:
    """Tracks seen item IDs across polls for deduplication.

    Each source gets its own seen-set, so IDs from different feeds never
    collide. Items seen in a previous poll() call are excluded from the
    return value of the next one.
    """

    def __init__(self) -> None:
        self._seen: dict[str, set[str]] = {}

    def _track(self, source: str, item_id: str | int) -> bool:
        """Record an item. Returns True if it's new, False if already seen."""
        key = str(item_id)
        if source not in self._seen:
            self._seen[source] = set()
        if key in self._seen[source]:
            return False
        self._seen[source].add(key)
        return True

    def seen_count(self, source: str) -> int:
        """How many items we've seen from this source."""
        return len(self._seen.get(source, set()))

    def reset(self, source: str | None = None) -> None:
        """Clear seen-set for a source, or all sources."""
        if source:
            self._seen.pop(source, None)
        else:
            self._seen.clear()

    # ----- JSON feeds (scenario engine) -----

    def poll_json(
        self,
        url: str,
        *,
        id_field: str = "id",
        items_field: str = "items",
        source_name: str | None = None,
        params: dict | None = None,
    ) -> list[dict]:
        """Poll a JSON feed and return only new items.

        Expects the response to be either:
        - A dict with an `items_field` key containing a list
        - A bare list

        Each item must have an `id_field` for deduplication.
        """
        source = source_name or url
        data = _fetch_json(url, params)

        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            items = data.get(items_field, [])
            if not isinstance(items, list):
                items = []
        else:
            return []

        new = []
        for item in items:
            item_id = item.get(id_field)
            if item_id is None:
                continue
            if self._track(source, item_id):
                new.append(item)
        return new

    # ----- ArcGIS GeoJSON feeds -----

    def poll_geojson(
        self,
        url: str,
        *,
        id_field: str = "OBJECTID",
        source_name: str | None = None,
        where: str = "1=1",
        limit: int = 100,
    ) -> list[dict]:
        """Poll an ArcGIS FeatureServer endpoint for new features.

        Requests GeoJSON format with WGS84 coordinates. Returns flat dicts
        with properties + lat/lng extracted from geometry.
        """
        source = source_name or url
        query_url = f"{url.rstrip('/')}/query"
        params = {
            "where": where,
            "outFields": "*",
            "outSR": 4326,
            "f": "geojson",
            "resultRecordCount": limit,
        }

        data = _fetch_json(query_url, params)
        features = data.get("features", [])

        new = []
        for f in features:
            props = f.get("properties", {})
            item_id = props.get(id_field)
            if item_id is None:
                continue
            if not self._track(source, item_id):
                continue

            # Extract lat/lng from geometry
            geom = f.get("geometry")
            lat, lng = None, None
            if geom and geom.get("type") == "Point":
                coords = geom.get("coordinates", [])
                if len(coords) >= 2:
                    lng, lat = coords[0], coords[1]

            row = dict(props)
            row["lat"] = lat
            row["lng"] = lng
            new.append(row)

        return new

    # ----- RSS feeds -----

    def poll_rss(
        self,
        url: str,
        *,
        source_name: str | None = None,
        limit: int = 20,
    ) -> list[dict]:
        """Poll an RSS/Atom feed and return new entries.

        Returns dicts with: title, link, description, published, guid.
        Uses guid (or link as fallback) for deduplication.
        """
        source = source_name or url
        body = _fetch(url)
        root = ET.fromstring(body)

        # Handle both RSS 2.0 and Atom namespaces
        atom_ns = "{http://www.w3.org/2005/Atom}"
        entries: list[dict] = []

        # Try RSS 2.0 first
        for item in root.findall(".//item"):
            guid = (
                item.findtext("guid")
                or item.findtext("link")
                or item.findtext("title")
            )
            if guid is None:
                continue
            entries.append({
                "guid": guid,
                "title": item.findtext("title") or "",
                "link": item.findtext("link") or "",
                "description": item.findtext("description") or "",
                "published": item.findtext("pubDate") or "",
            })

        # Try Atom if no RSS items found
        if not entries:
            for entry in root.findall(f".//{atom_ns}entry"):
                entry_id = (
                    entry.findtext(f"{atom_ns}id")
                    or (entry.find(f"{atom_ns}link") or {}).get("href", "")
                    or entry.findtext(f"{atom_ns}title")
                )
                if not entry_id:
                    continue
                link_el = entry.find(f"{atom_ns}link")
                entries.append({
                    "guid": entry_id,
                    "title": entry.findtext(f"{atom_ns}title") or "",
                    "link": link_el.get("href", "") if link_el is not None else "",
                    "description": (
                        entry.findtext(f"{atom_ns}summary")
                        or entry.findtext(f"{atom_ns}content")
                        or ""
                    ),
                    "published": entry.findtext(f"{atom_ns}updated") or "",
                })

        # Deduplicate and limit
        new = []
        for e in entries[:limit]:
            if self._track(source, e["guid"]):
                new.append(e)
        return new
