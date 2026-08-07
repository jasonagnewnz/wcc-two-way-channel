"""wcc_gis — a tiny client for the Wellington emergency-management GIS catalogue.

Single file, standard library only. Copy it into your project and go.

    import wcc_gis

    wcc_gis.search("tsunami")                              # find a dataset
    wcc_gis.info("footpaths")                              # what is it, what fields
    wcc_gis.features("tsunami-evacuation-zones", limit=5)  # rows, already in WGS84
    wcc_gis.geojson("footpaths", bbox=WELLINGTON)          # GeoJSON for a map

Datasets are addressed by a short readable id: "footpaths",
"earthquake-prone-buildings", "tsunami-evacuation-zones". `wcc_gis.ids()` lists
them all; `wcc_gis.search()` finds them.

Everything that returns geometry returns **WGS84 lon/lat**. The councils publish
in NZTM2000 (EPSG:2193); asking for anything else silently gives coordinates in
metres that will not plot on a web map. This module always sends outSR=4326.

Results are dict-backed but also support attribute access, so both of these
work and neither breaks older code:

    row = wcc_gis.features("footpaths", limit=1)[0]
    row["lat"], row.lat

Live river level and rainfall come from a different system (Hilltop XML):

    wcc_gis.hilltop_sites()                                # gauges with lat/lng
    wcc_gis.hilltop_data("Hutt River at Taita Gorge", "Flow")

Two things this module will not do quietly, because both have burned people:
it never returns a partial result without saying so (see `truncated` and
`iter_features`), and a Hilltop reading always carries its own units, because
they differ per measurement and are not what you would guess.
"""

from __future__ import annotations

import difflib
import gzip
import json
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path
from typing import Any, Iterable, Iterator

__all__ = [
    "WELLINGTON", "HILLTOP", "GisError",
    "Record", "Dataset", "Field", "SubLayer", "LayerInfo", "Feature",
    "FeatureCollection", "Site", "Reading",
    "catalogue", "datasets", "get", "name", "ids", "search",
    "info", "sublayers", "fields", "count", "extent",
    "query", "geojson", "features", "iter_features", "all_features",
    "layer_url", "query_url", "image_url",
    "hilltop_sites", "hilltop_measurements", "hilltop_data", "hilltop_units",
]

HERE = Path(__file__).resolve().parent


def _find(*names):
    """Look beside this file, then in ./data, then in the working directory.

    Keeps the module copy-pasteable: drop wcc_gis.py and catalogue.json into a
    project together and it still works.
    """
    for base in (HERE, HERE / "data", Path.cwd(), Path.cwd() / "data"):
        for candidate in (base / n for n in names):
            if candidate.exists():
                return candidate
    return HERE / names[0]


CATALOGUE_PATH = _find("catalogue.json", "wcc-em-gis-datasets.json")
SUPPLEMENTARY_PATH = _find("sources-supplementary.json")

#: Bounding box for Wellington City, as (west, south, east, north) in WGS84.
WELLINGTON = (174.62, -41.36, 174.94, -41.14)

#: Greater Wellington's live telemetry server (river level, flow, rainfall).
HILLTOP = "https://hilltop.gw.govt.nz/Telemetry.hts"

TIMEOUT = 30
#: Some hosts (gis.niwa.co.nz) reject a default python-urllib user agent.
USER_AGENT = "wcc_gis/2.0 (Wellington emergency hackathon; +catalogue client)"


class GisError(RuntimeError):
    """Raised with a readable message when a request or lookup fails.

    Every failure in this module raises this and nothing else, so one `except`
    is enough.
    """


# --------------------------------------------------------------------------
# result models
# --------------------------------------------------------------------------

class Record(dict):
    """A dict you can also read with attributes.

    Everything this module returns is really a dict — hand it to json.dumps,
    geopandas or MapLibre unchanged — but `row.lat` reads better than
    `row["lat"]` and an editor can complete it.
    """

    __slots__ = ()

    def __getattr__(self, key: str):
        try:
            return self[key]
        except KeyError:
            raise AttributeError(
                f"{type(self).__name__} has no {key!r}. "
                f"Available: {', '.join(sorted(map(str, self)))[:180]}") from None

    def __setattr__(self, key: str, value) -> None:
        self[key] = value

    def __dir__(self):
        return list(super().__dir__()) + [str(k) for k in self]


class Dataset(Record):
    """One catalogue entry. `id`, `display_name`, `description`, provenance."""


class Field(Record):
    """A layer attribute: `name`, `type`, `alias`."""


class SubLayer(Record):
    """A child layer: `id`, `name`, `type`.

    `type == "Group Layer"` means it holds no data of its own.
    """


class LayerInfo(Record):
    """Layer metadata: geometry, fields, limits, children.

    `type` is the thing to branch on — "Feature Layer" answers queries,
    "Raster Layer" only draws, "Group Layer" holds children.
    """


class Feature(Record):
    """One row: every attribute, plus `lat`/`lng`.

    `lat`/`lng` are the geometry's **first vertex**, not a centroid — enough to
    drop a pin, not a substitute for real geometry. Both are None when the
    server returned no geometry.
    """


class FeatureCollection(Record):
    """A GeoJSON FeatureCollection, plus `truncated`.

    `truncated` is True when the server had more rows than it returned. Use
    :func:`iter_features` to page through everything.

    Deliberately still a plain dict for iteration and length — overriding those
    to mean "the features" would quietly break `sorted(collection)`,
    `list(collection)` and `dict(**collection)`. Use `collection["features"]`.
    """


class Site(Record):
    """A telemetry gauge: `site`, `lat`, `lng`."""


class Reading(Record):
    """One observation: `time`, `value`, `units`, `measurement`, `divisor`.

    Units travel with the value on purpose. They differ per measurement —
    flow is m³/sec, stage is mm — and assuming them is a factor-of-1000 error.

    `value` is exactly what the server published, never rescaled. Hilltop
    declares a per-item `Divisor`, but the publisher does not apply it
    consistently: at Taita Gorge, Flow needs its divisor of 1000 while Area's
    declared 10000 is spurious — the raw number is already in square metres,
    confirmed against Hyd Radius x Wet Perimeter from the same rows. Silently
    dividing would turn 21 sq m into 0.0021. The declared value is on
    `divisor` so you can decide, and it is 1 for the single-item sources
    (Stage, Flow, Rainfall) that most work uses.
    """


# --------------------------------------------------------------------------
# catalogue
# --------------------------------------------------------------------------

_cache: dict[str, Any] = {}


def catalogue() -> dict:
    """The whole catalogue as a dict, loaded once."""
    if "catalogue" not in _cache:
        if not CATALOGUE_PATH.exists():
            raise GisError(
                f"catalogue.json not found (looked beside {HERE} and in ./data). "
                f"Keep it next to wcc_gis.py, or run "
                f"scripts/build_dataset_catalogue.py to regenerate it.")
        try:
            payload = json.loads(CATALOGUE_PATH.read_text())
        except json.JSONDecodeError as exc:
            raise GisError(f"{CATALOGUE_PATH} is not valid JSON: {exc}") from exc
        if not isinstance(payload, dict) or "datasets" not in payload:
            raise GisError(f"{CATALOGUE_PATH} has no 'datasets' key — is it the "
                           f"catalogue, or some other file?")
        payload["datasets"] = [Dataset(r) for r in payload["datasets"]]
        _cache["catalogue"] = payload
        try:
            supp = (json.loads(SUPPLEMENTARY_PATH.read_text())
                    if SUPPLEMENTARY_PATH.exists() else {})
        except json.JSONDecodeError as exc:
            raise GisError(f"{SUPPLEMENTARY_PATH} is not valid JSON: {exc}") from exc
        _cache["resolved"] = supp.get("resolved", {})
    return _cache["catalogue"]


def datasets() -> list[Dataset]:
    """Every dataset record."""
    return catalogue()["datasets"]


def get(dataset: str) -> Dataset:
    """One dataset by id, e.g. ``get("footpaths")``."""
    for record in datasets():
        if record["id"] == dataset:
            return record
    known = [r["id"] for r in datasets()]
    # Substring first (people type prefixes), then fuzzy (people make typos).
    near = [i for i in known if dataset.lower() in i] or \
        difflib.get_close_matches(dataset, known, n=5, cutoff=0.6)
    raise GisError(f"No dataset {dataset!r}."
                   + (f" Did you mean: {', '.join(near[:5])}?" if near else
                      " Try wcc_gis.search(...) or see docs/README.md."))


def name(dataset: str) -> str:
    """The readable display name, e.g. 'Tsunami Evacuation Zones (Wellington City)'."""
    record = get(dataset)
    return record.get("display_name") or record["name"]


def _is_queryable(record: dict) -> bool:
    """Can this dataset answer a feature query?

    Read from the catalogue's resolved layer facts, which come from asking each
    service what its layers actually are. The spreadsheet's own link type is
    not a reliable signal: it hides datasets whose working endpoint had to be
    researched, and counts raster and group layers as queryable when they are
    not.
    """
    if "feature_queryable" in record:
        return bool(record["feature_queryable"])
    # Older catalogue without the resolved facts — fall back to the weak signal.
    return record.get("link_type") == "arcgis_rest"


def _is_raster(record: dict) -> bool:
    return bool(record.get("raster_only"))


def search(text: str = "", *, scope: str | None = None, theme: str | None = None,
           queryable: bool | None = None, raster: bool | None = None) -> list[Dataset]:
    """Find datasets by free text, scope ('wcc'/'regional'/'national'), or theme.

    ``queryable=True`` restricts to layers that can answer a feature query;
    ``raster=True`` to the image-only ones.
    """
    needle = text.lower()
    out = []
    for record in datasets():
        haystack = " ".join(str(record.get(k) or "") for k in
                            ("display_name", "name", "id", "description",
                             "theme", "prepared_by")).lower()
        if needle and needle not in haystack:
            continue
        if scope and record["scope"] != scope:
            continue
        if theme and record.get("theme") != theme:
            continue
        if queryable is not None and _is_queryable(record) != queryable:
            continue
        if raster is not None and _is_raster(record) != raster:
            continue
        out.append(record)
    return out


def ids(text: str = "", **filters) -> list[str]:
    """Every dataset id, filtered exactly as in :func:`search`."""
    return [r["id"] for r in search(text, **filters)]


# --------------------------------------------------------------------------
# url resolution
# --------------------------------------------------------------------------

def _service_and_layer(dataset_id: str, layer: int | None) -> tuple[str, int | None]:
    record = get(dataset_id)
    catalogue()
    resolved = _cache["resolved"].get(dataset_id) or {}

    # A researched endpoint exists because the spreadsheet's link is wrong or
    # broken, so it takes precedence over the sheet's own URL.
    if resolved.get("endpoint") and "rest/services" in resolved["endpoint"]:
        url, default_layer = resolved["endpoint"], resolved.get("layer")
    elif record.get("link_type") == "arcgis_rest":
        url, default_layer = record["url"], record.get("layer_id")
    else:
        hint = resolved.get("note") or "It is a web page, not a queryable service."
        raise GisError(f"{dataset_id} is not queryable. {hint}")

    url = url.rstrip("/")
    if url.split("/")[-1].isdigit():
        if default_layer is None:
            default_layer = int(url.split("/")[-1])
        url = url.rsplit("/", 1)[0]

    # NB: no fallback to the catalogue's default_child here. That field exists
    # so the explorer can preselect something in a dropdown; picking 1 of 26
    # layers on the caller's behalf would be a guess, and guessing quietly is
    # what the "pick one with layer=" error exists to prevent. A service with
    # exactly one real layer is resolved by _sole_layer, which is not a guess.
    return url, (layer if layer is not None else default_layer)


def layer_url(dataset: str, layer: int | None = None) -> str:
    """The fully-resolved layer URL a query would go to.

    Public because reaching for a private helper to build a URL by hand is a
    sign the module is missing something.
    """
    root, chosen = _service_and_layer(dataset, layer)
    if chosen is None:
        chosen = _sole_layer(root)
    if chosen is None:
        raise GisError(
            f"{dataset} points at a whole service, not a single layer.\n"
            f"  Pick one with layer=:\n{_service_layers(root)}")
    return f"{root}/{chosen}"


#: Older name for :func:`layer_url`, kept so existing code keeps working.
_layer_url = layer_url


def query_url(dataset: str, layer: int | None = None, **params) -> str:
    """The exact URL :func:`query` would request. Handy for docs and debugging."""
    return f"{layer_url(dataset, layer)}/query?{urllib.parse.urlencode(params)}"


def image_url(dataset: str, layer: int | None = None,
              bbox: tuple[float, float, float, float] = WELLINGTON,
              size: tuple[int, int] = (900, 600), transparent: bool = True) -> str:
    """A rendered-image URL for a raster layer.

    Raster layers hold a grid of values, not features, so this is the only way
    to see them. ``bbox`` is WGS84 (west, south, east, north).
    """
    root, chosen = _service_and_layer(dataset, layer)
    if chosen is None:
        chosen = _sole_layer(root)
    if chosen is None:
        raise GisError(f"{dataset} is a whole service — pass layer= to pick one:\n"
                       f"{_service_layers(root)}")
    west, south, east, north = bbox
    return f"{root}/export?" + urllib.parse.urlencode({
        "bbox": f"{west},{south},{east},{north}", "bboxSR": 4326, "imageSR": 4326,
        "size": f"{size[0]},{size[1]}", "layers": f"show:{chosen}",
        "format": "png32", "transparent": str(transparent).lower(), "f": "image"})


def _sole_layer(service_url: str) -> int | None:
    """The id of the only real layer in a service, or None if it is ambiguous."""
    key = ("sole", service_url)
    if key not in _cache:
        _cache[key] = _pick_sole(service_url)
    return _cache[key]


def _pick_sole(service_url: str) -> int | None:
    leaves = [lyr for lyr in _service_layer_list(service_url)
              if lyr.get("type") != "Group Layer"]
    return leaves[0].get("id") if len(leaves) == 1 else None


def _service_layer_list(service_url: str) -> list[dict]:
    key = ("layers", service_url)
    if key not in _cache:
        try:
            _cache[key] = _fetch_json(service_url, {"f": "json"}).get("layers") or []
        except GisError as exc:
            # Distinguish "cannot reach it" from "it is ambiguous" — reporting a
            # network outage as a layer-selection problem sends people hunting
            # for the wrong thing.
            _cache[key] = []
            _cache[("layers_error", service_url)] = str(exc)
    return _cache[key]


def _service_layers(service_url: str) -> str:
    """List a service's layers, for the 'which layer?' error message."""
    layers = _service_layer_list(service_url)
    if not layers:
        problem = _cache.get(("layers_error", service_url))
        if problem:
            return f"    could not reach the service to list its layers — {problem}"
        return f"    (service publishes no layers — browse {service_url}?f=json)"
    # Group layers hold no data; offering one as a choice leads straight to an
    # opaque ArcGIS 400, so list the real layers first and mark the groups.
    leaves = [l for l in layers if l.get("type") != "Group Layer"]
    groups = [l for l in layers if l.get("type") == "Group Layer"]
    lines = [f"    layer={l.get('id'):<4} {l.get('name')}" for l in leaves[:30]]
    if len(leaves) > 30:
        lines.append(f"    … and {len(leaves) - 30} more")
    lines += [f"    (layer={l.get('id')} {l.get('name')} is a group — no data of its own)"
              for l in groups[:3]]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def _fetch(url: str, params: dict | None = None) -> bytes:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            body = response.read()
            encoding = (response.headers.get("Content-Encoding") or "").lower()
    except Exception as exc:  # noqa: BLE001 - surfaced with context below
        raise GisError(f"Request failed: {url}\n  {exc}") from exc

    # Hilltop gzips some responses without being asked, including its error
    # bodies — leaving them compressed destroyed the actual message.
    try:
        if encoding == "gzip":
            body = gzip.decompress(body)
        elif encoding == "deflate":
            body = zlib.decompress(body, -zlib.MAX_WBITS)
    except (OSError, zlib.error) as exc:
        raise GisError(f"Could not decompress a {encoding} response from {url}: "
                       f"{exc}") from exc
    return body


def _fetch_json(url: str, params: dict | None = None) -> dict:
    body = _fetch(url, params)
    try:
        payload = json.loads(body)
    except UnicodeDecodeError as exc:
        raise GisError(f"{url} returned bytes that are not text "
                       f"({body[:40]!r}…): {exc}") from exc
    except json.JSONDecodeError as exc:
        raise GisError(f"Expected JSON from {url}, got {body[:120]!r}: {exc}") from exc
    if not isinstance(payload, dict):
        raise GisError(f"Expected a JSON object from {url}, got "
                       f"{type(payload).__name__}: {str(payload)[:120]}")
    # ArcGIS reports failures as HTTP 200 with an error in the body, and is not
    # consistent about whether that error is an object, a string or a list.
    error = payload.get("error")
    if error is not None:
        if isinstance(error, dict):
            raise GisError(f"ArcGIS error {error.get('code')}: "
                           f"{error.get('message')} ({url})")
        raise GisError(f"ArcGIS error: {str(error)[:200]} ({url})")
    return payload


# --------------------------------------------------------------------------
# layer metadata
# --------------------------------------------------------------------------

def info(dataset: str, layer: int | None = None) -> LayerInfo:
    """Layer metadata: geometry type, fields, record limit, child layers.

    ``type`` of "Group Layer" means you cannot query this directly — pick one
    of ``sublayers`` and pass it as ``layer=``. "Raster Layer" means there are
    no features at all; use :func:`image_url`.
    """
    url = layer_url(dataset, layer)
    payload = _fetch_json(url, {"f": "json"})
    resolved = url.rsplit("/", 1)[-1]
    return LayerInfo({
        "id": dataset,
        # The layer that actually answered, not the argument that was passed.
        "layer": int(resolved) if resolved.isdigit() else layer,
        "name": payload.get("name"),
        "type": payload.get("type"),
        "geometry_type": payload.get("geometryType"),
        "description": (payload.get("description") or "").strip() or None,
        "max_record_count": payload.get("maxRecordCount"),
        "url": url,
        "sublayers": [SubLayer({"id": s.get("id"), "name": s.get("name"),
                                "type": s.get("type")})
                      for s in payload.get("subLayers") or []],
        "fields": [Field({"name": f.get("name"), "type": f.get("type"),
                          "alias": f.get("alias")})
                   for f in payload.get("fields") or []],
    })


def sublayers(dataset: str) -> list[SubLayer]:
    """Child layers of a group layer or a whole service — always a list.

    Empty when the dataset is a single queryable layer.
    """
    record = get(dataset)
    root, _ = _service_and_layer(dataset, None)

    # Two different questions wear the same name. If the dataset addresses one
    # specific layer, its children are that layer's sublayers. If it addresses a
    # whole service, they are the service's layers. Picking a convenience child
    # (default_child) does not count as addressing a layer — asking the service
    # is what the caller meant.
    addresses_a_layer = (record.get("resolved_layer") is not None
                         or record.get("layer_id") is not None)
    if addresses_a_layer:
        try:
            return info(dataset)["sublayers"]
        except GisError:
            return []
    return [SubLayer({"id": l.get("id"), "name": l.get("name"),
                      "type": l.get("type")})
            for l in _service_layer_list(root)]


def fields(dataset: str, layer: int | None = None) -> list[str]:
    """Just the field names, for building a ``where`` clause or ``out_fields``."""
    return [f["name"] for f in info(dataset, layer)["fields"]]


def _check_queryable(dataset_id: str, layer: int | None) -> None:
    """Turn ArcGIS's opaque 'Invalid or missing input parameters' into advice.

    Runs for explicitly-passed layers too: a caller who picks a group layer id
    deserves the same explanation as one who picks nothing.
    """
    record = get(dataset_id)
    if layer is None and _is_raster(record) and record.get("resolved_layer") is not None:
        raise GisError(
            f"{dataset_id} layer {record['resolved_layer']} is a raster layer — a "
            f"grid of values, not features, so there is nothing to list. Render "
            f"it instead:\n  wcc_gis.image_url({dataset_id!r}, "
            f"layer={record['resolved_layer']})")

    meta = info(dataset_id, layer)
    if meta["type"] == "Raster Layer":
        where = f", layer={meta['layer']}" if meta["layer"] is not None else ""
        raise GisError(
            f"{dataset_id} layer {meta['layer']} is a raster layer — a grid of "
            f"values, not features, so there is nothing to list. Render it "
            f"instead:\n  wcc_gis.image_url({dataset_id!r}{where})")
    if meta["type"] == "Group Layer":
        children = [c for c in meta["sublayers"] if c["type"] != "Group Layer"]
        if not children:
            raise GisError(f"{dataset_id} layer {meta['layer']} is a group layer "
                           f"with no queryable children.")
        listed = ", ".join(f"{c['id']} ({c['name']})" for c in children)
        raise GisError(
            f"{dataset_id} layer {meta['layer']} ({meta['name']!r}) is a group "
            f"layer and holds no data of its own. Pick a child with layer=:\n"
            f"  {listed}\n"
            f"e.g. wcc_gis.features({dataset_id!r}, layer={children[0]['id']})")


# --------------------------------------------------------------------------
# querying
# --------------------------------------------------------------------------

def _spatial(params: dict, bbox, at, near) -> None:
    """Attach at most one spatial filter."""
    given = [n for n, v in (("bbox", bbox), ("at", at), ("near", near)) if v]
    if len(given) > 1:
        raise GisError(f"Pass only one spatial filter — got {', '.join(given)}.")
    if bbox:
        west, south, east, north = bbox
        params.update({"geometry": f"{west},{south},{east},{north}",
                       "geometryType": "esriGeometryEnvelope"})
    elif at:
        lat, lng = at
        params.update({"geometry": f"{lng},{lat}",
                       "geometryType": "esriGeometryPoint"})
    elif near:
        lat, lng, metres = near
        params.update({"geometry": f"{lng},{lat}",
                       "geometryType": "esriGeometryPoint",
                       "distance": metres, "units": "esriSRUnit_Meter"})
    if given:
        params.setdefault("inSR", 4326)
        params.setdefault("spatialRel", "esriSpatialRelIntersects")


def query(dataset: str, *, where: str = "1=1",
          out_fields: str | Iterable[str] = "*",
          bbox: tuple[float, float, float, float] | None = None,
          at: tuple[float, float] | None = None,
          near: tuple[float, float, float] | None = None,
          limit: int | None = None, offset: int = 0, layer: int | None = None,
          order_by: str | Iterable[str] | None = None,
          geometry: bool = True, distinct: bool = False,
          extra: dict | None = None) -> FeatureCollection:
    """Query a layer. Always GeoJSON, always WGS84.

    Spatial filters, pick one:
      ``bbox=(west, south, east, north)`` — use ``WELLINGTON`` for the city
      ``at=(lat, lng)``                   — features containing this point
      ``near=(lat, lng, metres)``         — features within a radius

    ``limit`` caps rows, but the server also enforces its own maxRecordCount.
    The result's ``truncated`` says whether more exist; :func:`iter_features`
    pages through them.
    """
    _check_queryable(dataset, layer)
    if not isinstance(out_fields, str):
        out_fields = ",".join(out_fields)

    params: dict[str, Any] = {
        "where": where,
        "outFields": out_fields,
        "outSR": 4326,              # never drop this — the source is NZTM (2193)
        "returnGeometry": str(bool(geometry)).lower(),
        "f": "geojson",
    }
    _spatial(params, bbox, at, near)
    if limit:
        params["resultRecordCount"] = limit
    if offset:
        params["resultOffset"] = offset
    if order_by:
        params["orderByFields"] = (order_by if isinstance(order_by, str)
                                   else ",".join(order_by))
    if distinct:
        params["returnDistinctValues"] = "true"
    if extra:
        params.update(extra)

    payload = _fetch_json(layer_url(dataset, layer) + "/query", params)
    collection = FeatureCollection(payload)
    if "features" in payload:
        collection["features"] = [Feature(f) if isinstance(f, dict) else f
                                  for f in (payload["features"] or [])]
    # The flag sits at the top level on ArcGIS Server but only inside
    # "properties" on ArcGIS Online.
    collection["truncated"] = bool(
        payload.get("exceededTransferLimit")
        or (payload.get("properties") or {}).get("exceededTransferLimit"))
    return collection


def geojson(dataset: str, **kwargs) -> FeatureCollection:
    """A GeoJSON FeatureCollection, ready for MapLibre/Leaflet."""
    return query(dataset, **kwargs)


def features(dataset: str, **kwargs) -> list[Feature]:
    """Flat rows: every attribute plus ``lat``/``lng``.

    Convenient when you want one record per feature and somewhere to put the
    pin. For real geometry use :func:`geojson`.

    Only returns what one request gave you — check ``.truncated`` on
    :func:`geojson`, or use :func:`iter_features`, if the layer is large.
    """
    return _rows(query(dataset, **kwargs))


def _rows(collection) -> list[Feature]:
    rows = []
    for feature in collection.get("features") or []:
        row = Feature(feature.get("properties") or {})
        point = _first_point(feature.get("geometry"))
        # Only synthesise lat/lng when the layer does not already have them.
        row.setdefault("lng", point[0] if point else None)
        row.setdefault("lat", point[1] if point else None)
        rows.append(row)
    return rows


def iter_features(dataset: str, *, page_size: int = 1000,
                  max_features: int = 20000, **kwargs) -> Iterator[Feature]:
    """Page through a layer, yielding every feature.

    A single query is capped by the server (often 2000 rows), so this is the
    only correct way to read a large layer. Verified against every service
    family in the catalogue: all honour ``resultOffset``.

    ``max_features`` is a deliberate guard, not a formality — `tree-cover` is
    over 160,000 features, which is 80+ requests at council servers. Raise it
    consciously.
    """
    if "limit" in kwargs or "offset" in kwargs:
        raise GisError("iter_features pages for you — drop limit/offset and use "
                       "page_size / max_features instead.")
    seen = 0
    offset = 0
    while seen < max_features:
        batch = query(dataset, limit=min(page_size, max_features - seen),
                      offset=offset, **kwargs)
        rows = _rows(batch)
        if not rows:
            return
        for row in rows:
            yield row
        seen += len(rows)
        # Advance by what we actually got: the server silently caps page size,
        # and not every layer even publishes an object-id field to key on.
        offset += len(rows)
        if not batch["truncated"]:
            return
    raise GisError(
        f"{dataset} has more than max_features={max_features} rows. Raise it if "
        f"you really want them all — that is {max_features // page_size}+ "
        f"requests to a council server.")


def all_features(dataset: str, **kwargs) -> list[Feature]:
    """Every feature, as a list. See :func:`iter_features` for the caveats."""
    return list(iter_features(dataset, **kwargs))


def count(dataset: str, where: str = "1=1", layer: int | None = None, **kwargs) -> int:
    """How many features match — cheap, ask this before pulling everything."""
    _check_queryable(dataset, layer)
    params: dict[str, Any] = {"where": where, "returnCountOnly": "true", "f": "json"}
    _spatial(params, kwargs.pop("bbox", None), kwargs.pop("at", None),
             kwargs.pop("near", None))
    payload = _fetch_json(layer_url(dataset, layer) + "/query", params)
    if "count" not in payload:
        raise GisError(f"{dataset} did not return a count — the service answered "
                       f"with {sorted(payload)[:6]}. Reporting 0 here would be a "
                       f"guess.")
    return payload["count"]


def extent(dataset: str, layer: int | None = None) -> dict:
    """The layer's bounding box in WGS84: west, south, east, north.

    The two service families disagree about where the box lives — ArcGIS Server
    puts it under ``extent.bbox``, ArcGIS Online at the top level as ``bbox`` —
    so this hides the difference.
    """
    payload = query(dataset, layer=layer, geometry=False,
                    extra={"returnExtentOnly": "true"})
    box = payload.get("bbox") or (payload.get("extent") or {}).get("bbox")
    if isinstance(box, (list, tuple)) and len(box) >= 4:
        west, south, east, north = box[:4]
        return {"west": west, "south": south, "east": east, "north": north}
    corners = payload.get("extent") or {}
    if isinstance(corners, dict) and "xmin" in corners:
        return {"west": corners["xmin"], "south": corners["ymin"],
                "east": corners["xmax"], "north": corners["ymax"]}
    raise GisError(f"{dataset} did not return an extent ({sorted(payload)[:6]}).")


def _first_point(geometry: dict | None) -> tuple[float, float] | None:
    """First vertex as (lon, lat) — GeoJSON order, unwrapped to any depth."""
    if not isinstance(geometry, dict):
        return None
    coords = geometry.get("coordinates")
    while isinstance(coords, list) and coords and isinstance(coords[0], list):
        coords = coords[0]
    if isinstance(coords, list) and len(coords) >= 2:
        try:
            return float(coords[0]), float(coords[1])
        except (TypeError, ValueError):
            return None
    return None


# --------------------------------------------------------------------------
# Hilltop — live river level, flow and rainfall
# --------------------------------------------------------------------------

def _hilltop(**params) -> ET.Element:
    # Hilltop needs %20 for spaces; a '+' is not decoded and returns "No data".
    query_string = "&".join(
        f"{k}={urllib.parse.quote(str(v), safe='')}" for k, v in params.items())
    body = _fetch(f"{HILLTOP}?{query_string}")
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise GisError(
            f"Hilltop did not return XML for {params.get('Site', '?')!r} "
            f"({params.get('Measurement', '')}): {body[:80]!r}") from exc
    # An <Error> can be the root itself or a child, depending on the request.
    if root.tag == "Error":
        raise GisError(f"Hilltop: {(root.text or 'unknown error').strip()}")
    error = root.findtext("Error") or root.findtext(".//Error")
    if error:
        raise GisError(f"Hilltop: {error.strip()}")
    if root.tag.lower() == "html" or root.find(".//body") is not None:
        raise GisError(f"Hilltop returned a web page, not data, for "
                       f"{params.get('Site', '?')!r} — the service may be down.")
    return root


def hilltop_sites(with_coordinates: bool = True) -> list[Site]:
    """Every telemetry gauge. Coordinates are already WGS84."""
    root = _hilltop(service="Hilltop", request="SiteList", location="LatLong")
    sites = []
    for site in root.findall("Site"):
        lat, lng = site.findtext("Latitude"), site.findtext("Longitude")
        try:
            lat, lng = (float(lat) if lat else None), (float(lng) if lng else None)
        except ValueError:
            lat = lng = None
        if with_coordinates and (lat is None or lng is None):
            continue
        sites.append(Site({"site": site.get("Name"), "lat": lat, "lng": lng}))
    return sites


def hilltop_measurements(site: str) -> list[str]:
    """What this gauge records, e.g. 'Stage', 'Flow', 'Rainfall'."""
    root = _hilltop(service="Hilltop", request="MeasurementList", Site=site)
    seen, out = set(), []
    for node in root.iter("Measurement"):
        label = node.get("Name")
        if label and label not in seen:
            seen.add(label)
            out.append(label)
    return out


def _item_for(root: ET.Element, measurement: str) -> tuple[int, str | None, float]:
    """Which I-number holds this measurement, plus its units and declared divisor.

    Hilltop returns EVERY item of a DataSource whatever you asked for. Taita
    Gorge's "Gauging Results" has fifteen, so reading <I1> blindly gives Stage
    when you asked for Area.
    """
    items = list(root.iter("ItemInfo"))
    if not items:
        return 1, None, 1.0
    wanted = measurement.strip().lower()
    for item in items:
        if (item.findtext("ItemName") or "").strip().lower() == wanted:
            number = int(item.get("ItemNumber") or 1)
            divisor = item.findtext("Divisor")
            try:
                divisor = float(divisor) if divisor else 1.0
            except ValueError:
                divisor = 1.0
            return number, item.findtext("Units"), (divisor or 1.0)
    available = [(i.findtext("ItemName") or "?") for i in items]
    raise GisError(f"Hilltop returned no item called {measurement!r}. "
                   f"This DataSource holds: {', '.join(available[:12])}")


def hilltop_units(site: str, measurement: str = "Stage") -> str | None:
    """The units this gauge reports for a measurement, e.g. 'm³/sec' or 'mm'.

    Resolved by matching the measurement name, not by position — a multi-item
    DataSource lists several <Units> and the first is rarely the one you want.
    """
    root = _hilltop(service="Hilltop", request="GetData", Site=site,
                    Measurement=measurement, TimeInterval="PT1H")
    return _item_for(root, measurement)[1]


def hilltop_data(site: str, measurement: str = "Stage",
                 interval: str = "PT6H") -> list[Reading]:
    """Observations for a gauge, oldest first.

    ``interval`` is ISO 8601: PT6H, P1D, P7D. **It means "the last N records",
    not "the last N hours"** — a decommissioned gauge will happily answer with
    a reading from years ago. Check ``reading.time`` before calling it live.

    Each :class:`Reading` carries its own ``units``; they differ per
    measurement (flow m³/sec, stage mm) and assuming them is a 1000× error.
    Values are published as-is and never rescaled — see :class:`Reading`.
    """
    root = _hilltop(service="Hilltop", request="GetData", Site=site,
                    Measurement=measurement, TimeInterval=interval)
    number, units, divisor = _item_for(root, measurement)
    tag = f"I{number}"
    readings = []
    for entry in root.iter("E"):
        time, raw = entry.findtext("T"), entry.findtext(tag)
        if time is None or raw is None:
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = raw
        # Published as-is. See Reading's docstring for why the declared divisor
        # is reported rather than applied.
        readings.append(Reading({"time": time, "value": value, "units": units,
                                 "measurement": measurement, "divisor": divisor}))
    return readings


if __name__ == "__main__":
    cat = catalogue()
    queryable = search(queryable=True)
    print(f"{cat['counts']['total']} datasets · {len(queryable)} feature-queryable "
          f"· {len(search(raster=True))} raster")
    print(f"live gauges: {len(hilltop_sites())}")
    print("\ntry:  python3 -c \"import wcc_gis; "
          "print(wcc_gis.features('tsunami-evacuation-zones', limit=3))\"")
