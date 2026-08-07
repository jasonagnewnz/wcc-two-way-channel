# wcc_gis Gotchas — Verified

The team-6 README calls out three traps in the raw ArcGIS services. Checked
against `wcc_gis.py` on 2026-08-07, live against the real WCC servers — all
three are already handled by the SDK. Use `wcc_gis`, don't hit ArcGIS REST
endpoints directly, and none of this is your problem.

## 1. NZTM2000, not lat/lng

Raw ArcGIS responses are in NZTM2000 (EPSG:2193) — plot them unconverted and
your pins land off the coast of Africa.

`wcc_gis.query()` hardcodes `outSR=4326` on every request (wcc_gis.py:630).
Verified: `wcc_gis.features("tsunami-evacuation-zones", limit=1)` returned
`lat=-41.14, lng=174.78` — real Wellington coordinates, not raw metres.

**Nothing to do.** Every `wcc_gis` call is already WGS84.

## 2. Rasters advertise a query capability, then refuse to answer

A quarter of the layers (25 of 74 in the catalogue) are raster-only —
`search(raster=True)` lists them. Query one for features and ArcGIS gives an
opaque error.

`wcc_gis.features()` / `query()` detect this *before* the network call and
raise a `GisError` telling you to use `image_url()` instead. Verified on
`slope-degrees`:

```
features('slope-degrees') -> GisError:
  slope-degrees layer 0 is a raster layer — a grid of values, not features,
  so there is nothing to list. Render it instead:
  wcc_gis.image_url('slope-degrees', layer=0)
```

**If a dataset is raster-only, call `wcc_gis.image_url(dataset)` for a PNG,
not `features()`/`query()`.**

## 3. One query is silently capped

`footpaths` has 8,130 features; a single request returns 2,000 and says
nothing unless you check for it.

`wcc_gis.query()` sets `.truncated` from the server's `exceededTransferLimit`
flag (handles both ArcGIS Server and ArcGIS Online response shapes).
`all_features()` / `iter_features()` page through via `resultOffset`
automatically. Verified: `all_features("footpaths", bbox=WELLINGTON,
max_features=9000)` returned all 8,130 rows in one call.

**Use `all_features()` (or `iter_features()` for a generator) on anything you
expect to be large — not `features()`, which only returns one page.**
