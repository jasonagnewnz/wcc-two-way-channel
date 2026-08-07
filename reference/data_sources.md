# Data Sources by Track

Which datasets and feeds map to each of the 5 hackathon tracks.

---

## Track 1 — Warnings & Hazards

| Source | URL / Dataset | Type | Notes |
|---|---|---|---|
| Scenario weather feed | `https://impact-lab-wlg-2026-08-08.vercel.app/api/scenario/weather` | JSON | CAP-style warnings, supports `?t=` fast-forward |
| GWRC Hilltop telemetry | `https://hilltop.gw.govt.nz/Telemetry.hts` | XML/API | River gauges: Stage, Flow, Rainfall. Spaces must be `%20` not `+` |
| NZTA road delays | `https://www.journeys.nzta.govt.nz/assets/map-data-cache/delays.json` | JSON | Nationwide; filter to Wellington bbox |
| wcc_gis: tsunami-evacuation-zones | via `wcc_gis.features()` | Spatial | Zone_Class: Red/Orange/Yellow |
| wcc_gis: flood-hazard-areas | via `wcc_gis.features()` | Spatial | Hazard_Class field |
| wcc_gis: active-faults | via `wcc_gis.features()` | Spatial | Use `near=` query |

## Track 2 — Community Reports

| Source | URL / Dataset | Type | Notes |
|---|---|---|---|
| Scenario social feed | `https://impact-lab-wlg-2026-08-08.vercel.app/api/scenario/social` | JSON | Simulated social posts, supports `?t=` |
| Manual reporting form | Built in React UI | User input | Needs UI component (not in loader) |
| wcc_impact.geocode() | SDK function | API | Geocode Wellington place names to lat/lng |
| wcc_impact.ask_claude() | SDK function | API | Classify free text into signal fields |

## Track 3 — Impact Corroboration

| Source | URL / Dataset | Type | Notes |
|---|---|---|---|
| Scenario social feed | (same as Track 2) | JSON | Community eyewitness reports |
| RNZ RSS | `https://www.rnz.co.nz/rss/national.xml` | RSS/Atom | Filter to Wellington-relevant items |
| NEMA electricity outages | `https://services5.arcgis.com/cJn6oR1QqErYBL5d/arcgis/rest/services/electricity_outages_read_only/FeatureServer/0` | ArcGIS GeoJSON | Use `f=geojson&outSR=4326` |
| Spatial clustering | In-memory | Algorithm | Haversine distance, 2km radius |

## Track 4 — Signal Triage

| Source | URL / Dataset | Type | Notes |
|---|---|---|---|
| Shared signals table | `wcc_impact.fetch_signals()` | SDK | All teams' published signals |
| wcc_impact.ask_claude() | SDK function | API | Classify into action/verify/awareness |
| Title similarity + location proximity | In-memory | Algorithm | Jaccard on word sets + haversine |

## Track 5 — Movement & Transport

| Source | URL / Dataset | Type | Notes |
|---|---|---|---|
| NZTA cameras | `https://www.journeys.nzta.govt.nz/assets/map-data-cache/cameras.json` | JSON | Track dropout as infrastructure damage proxy |
| NZTA delays | (same as Track 1) | JSON | Rolling baseline comparison for anomaly detection |
| OpenSky aircraft | `https://opensky-network.org/api/states/all?lamin=-41.6&lomin=174.5&lamax=-41.0&lomax=175.3` | JSON | Wellington TMA bbox; near-zero = airport closure |

---

## Additional GIS Datasets (wcc_gis)

Available for enrichment across all tracks:

| Dataset ID | Use |
|---|---|
| `tsunami-evacuation-zones` | Zone classification at a point |
| `liquefaction-vulnerability` | Liquefaction risk category |
| `flood-hazard-areas` | Flood hazard classification |
| `active-faults` | Nearest fault (use `near=`) |
| `deprivation-2023` | NZDep2023 deprivation decile |
| `earthquake-prone-buildings` | EQ-prone buildings nearby (use `near=`) |
| `community-emergency-hubs` | Nearest emergency hub (use `near=`) |
| `wellington-city-facilities` | Council facilities |
| `school-zones` | School catchments |
| `council-controlled-land` | Public land parcels |

See also: `docs/additional-sources.md` in the GIS repo for 60+ additional feeds.
