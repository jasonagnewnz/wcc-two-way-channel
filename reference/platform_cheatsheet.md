# Platform Cheatsheet — Impact Lab WLG 2026-08-08

Quick reference for hackathon day. Keep this open.

## Signal Schema (required fields)

| Field | Type | Constraint |
|---|---|---|
| `title` | string | **required**, max 200 chars |
| `signal_type` | string | **required**, kebab-case, max 100 chars |
| `source_type` | enum | **required**: `official` \| `community` \| `media` \| `sensor` |
| `module_id` | string | **required**, your team's module ID |
| `severity` | enum | `minor` \| `moderate` \| `severe` \| `extreme` \| `unknown` |
| `description` | string | max 2000 chars |
| `lat` / `lng` | float | WGS84, optional |
| `place_name` | string | max 200 chars |
| `confidence` | float | 0.0–1.0 |
| `link` | string | max 2000 chars |
| `source` | string | max 200 chars |
| `observed_at` | ISO 8601 | when the event happened |
| `reported_at` | ISO 8601 | when it was reported |
| `idempotency_key` | string | max 200 chars, prevents duplicate inserts |
| `media_urls` | string[] | image/video URLs |
| `raw` | jsonb | original source data |

## wcc_impact SDK — Key Exports

```python
from wcc_impact import (
    publish_signal,      # (**signal_fields) → publishes to Supabase
    register_module,     # (id, name, icon, description) → registers your module
    run_every,           # (seconds, callback) → polling loop (min 5s)
    ask_claude,          # (prompt, system=, max_tokens=) → Claude response string
    analyze_image,       # (url_or_path, prompt=) → Claude vision response
    fetch_signals,       # (limit=, signal_type=, module_id=) → list[dict]
    geocode,             # (place_name) → (lat, lng) or None
    heartbeat,           # () → updates module last_seen
    upload_file,         # (path, name=) → URL
    on_new_signals,      # (callback, signal_type=) → real-time listener
    SEVERITIES,          # frozenset of valid severity values
    SOURCE_TYPES,        # frozenset of valid source_type values
)
```

## wcc_gis SDK — Key Exports

```python
import wcc_gis

# Spatial queries
wcc_gis.features(dataset, at=(lat,lng), limit=)      # point-in-polygon
wcc_gis.features(dataset, near=(lat,lng,metres), limit=)  # radius search
wcc_gis.features(dataset, bbox=(w,s,e,n), limit=)    # bounding box
wcc_gis.count(dataset, at=|near=|bbox=)               # count only
wcc_gis.geojson(dataset, at=|near=|bbox=)             # raw GeoJSON

# Hilltop telemetry (live river gauges)
wcc_gis.hilltop_sites()                                # all gauge sites
wcc_gis.hilltop_data(site, measurement, interval=)    # readings
wcc_gis.hilltop_measurements(site)                     # available measurements

# IMPORTANT: Hilltop spaces must be %20, never +
```

## plugin-sdk (React UI)

```typescript
import { useSignals, useModuleConfig, publishSignal } from 'plugin-sdk';

// In your React component:
const signals = useSignals({ signal_type: 'weather-warning' });
const [config, setConfig] = useModuleConfig();
```

## Loader Contract

Every loader must expose:

```python
def main():
    """Entrypoint: register then poll."""
    register_module(id=MODULE_ID, name=..., icon=..., description=...)
    run_every(INTERVAL, tick)   # min 5 seconds

def sample() -> dict:
    """One representative signal dict — no database insert."""
    return make_signal(...)

def tick():
    """One polling cycle: fetch → classify → publish."""
    ...
```

## Golden-Path Commands

```bash
# Start your module
cd modules/your-team && python loader.py

# Test sample output
python -c "from loader import sample; import json; print(json.dumps(sample(), indent=2))"

# Check signals in Supabase
python -c "from wcc_impact import fetch_signals; print(fetch_signals(limit=5))"

# Validate signal schema
python -c "from enrichment.signal_helpers import make_signal; print(make_signal(module_id='test', title='Test', signal_type='test', source_type='official'))"
```

## Scenario Feed URLs

| Feed | URL | Format |
|---|---|---|
| Weather | `https://impact-lab-wlg-2026-08-08.vercel.app/api/scenario/weather` | JSON, `{items: [...]}` |
| Social | `https://impact-lab-wlg-2026-08-08.vercel.app/api/scenario/social` | JSON, `{items: [...]}` |

Both support `?t=<seconds>` for fast-forward during testing.

## Key Rules

1. **kebab-case** for signal_type (e.g., `weather-warning`, not `weatherWarning`)
2. **Idempotency keys** prevent duplicate signals across restarts
3. **run_every minimum** is 5 seconds (SDK enforced)
4. **Durable outbox** is the default — signals survive restarts
5. **Don't store secrets in code** — use env vars via the platform
6. **Title max 200 chars** — truncate, don't crash
7. **Severity "unknown"** is valid — better than guessing
