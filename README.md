# Community &harr; WCC — a two-way reporting channel

**Impact Lab Wellington · Team 6 · Problem 02**
Wellington City Council Emergency Management × Claude Code Community NZ

A working prototype of the thing Problem 02 asks for: a route for communities
to report local conditions to Wellington City Council, and — the part that
usually goes missing — a way to **see that the report was received and what is
happening to it**.

---

## Run it

```bash
git clone https://github.com/jasonagnewnz/wcc-two-way-channel
cd wcc-two-way-channel
python3 run.py --seed
```

Open <http://127.0.0.1:8080>.

That is the whole setup. **No `pip install`, no build step, no API key, no
database.** Python 3.9+ and the standard library. Every dependency is a chance
for one person's laptop to be the one where it does not work, and today there
is no time for that.

`--seed` puts six plausible Wellington reports on the map so there is something
to look at. Leave it off for an empty slate.

### See the loop in thirty seconds

1. **Report an issue** — pick a type, write a line, tap the map, send.
   You get a reference code like `WLG-K7M2Q`. No account, no login.
2. **WCC view** — your report is on the map. Tap **Being checked**.
3. **My reports** — the status has already changed. Nothing was refreshed and
   nobody was rung.

That is the entire product thesis in three clicks.

---

## Why this shape

**A third of council call-centre volume is people asking "what's happening
with the thing I reported".** Every one of those calls is a person who already
gave the council information and got nothing back. The model here is delivery
tracking, not a support ticket: one tap from staff becomes an update the
reporter sees immediately.

**The reference code is the whole auth model.** There are no accounts. During
an emergency, a login screen is a barrier between someone and the information
you need from them. Possession of the code is the claim; it is stored on the
reporter's device and can be typed in on any other.

**Nothing is ever edited.** A status change publishes a *new* signal that
chains to the original report. Current status is derived by replaying the
chain, which means the reporter's view and the council's view cannot disagree,
and after the event there is a complete, timestamped record of who knew what
and when. That is an audit trail an emergency-management body actually needs —
and it came free from a platform constraint (`publish_signal` exists,
`update_signal` does not).

**Inference is labelled as inference.** When a report lands we look up what
the WCC hazard layers say about that location — tsunami evacuation zone,
nearest Community Emergency Hub. That is shown as context and marked as
inferred, never as confirmed fact. Presenting something unverified as verified
is the failure mode these problem statements are most wary of.

---

## It composes

The brief asks for modules that feed one shared common operating picture,
and for outputs that compose — a feed, an endpoint, GeoJSON — over a
self-contained UI nothing else can read.

```
GET /api/geojson     every report as a GeoJSON FeatureCollection
GET /api/signals     the raw append-only log, with a ?since= cursor
```

`/api/geojson` is CORS-open and needs no key. Point any other team's map
straight at it:

```js
fetch('http://<this-machine>:8080/api/geojson').then(r => r.json())
```

Each feature carries `reference`, `issue_type`, `severity`, `status`,
`group_id` and `place_name`.

### All endpoints

| | |
|---|---|
| `GET /api/health` | liveness + signal count |
| `GET /api/meta` | issue types, statuses, map extent |
| `POST /api/reports` | submit a report → reference code |
| `GET /api/reports` | every report with status and group (the WCC view) |
| `GET /api/reports/<ref>` | one report + its full status timeline |
| `POST /api/reports/<ref>/status` | one-tap status update |
| `GET /api/geojson` | reports as GeoJSON |
| `GET /api/signals?since=` | the append-only log |
| `GET /api/basemap` | cached WCC hazard geometry |

---

## Dropping onto the platform

`loader.py` implements the platform contract — `main()`, `tick()`, `sample()`.

```bash
python3 loader.py           # run the poller
python3 loader.py sample    # print one representative signal
```

It binds to `wcc_impact` if that is importable and to the local append-only log
if it is not, through the same `ReportService`. **The same code path runs in
both cases; only the store differs.** So this repo is not blocked waiting for
the SDK, and nothing has to be rewritten when the SDK arrives — change
`MODULE_ID` and go.

It also resolves an open question the prep notes flagged: whether
`publish_signal()` returns the created signal, which the reference code was
assumed to depend on. `PlatformStore` mints the reference itself and carries it
in `raw.reference`, so it works either way. Still worth asking; no longer
blocking.

`tick()` acknowledges any `community-report` signal that has not been
acknowledged — including reports published by **another** module. If the
social-feed sketch turns a public post into a report signal, this closes the
loop on it too, without either module knowing the other exists. The log is the
entire integration surface.

---

## Layout

```
run.py                 start here
server.py              HTTP + JSON API (stdlib http.server)
loader.py              platform contract; wcc_impact or local
core/
  signals.py           the signal schema, standalone
  store.py             append-only JSONL log
  reports.py           the two-way loop: submit, acknowledge, group, GeoJSON
  hazard.py            hazard context — cached, backgrounded, never load-bearing
web/                   the app: one HTML, one CSS, one JS, no framework
  data/basemap.json    baked WCC geometry (regenerate with tools/fetch_basemap.py)
tests/                 python3 -m unittest discover tests
tools/fetch_basemap.py pull + simplify the WCC layers
wcc_gis.py             the WCC GIS SDK (unmodified, from the data repo)
catalogue.json         74 catalogued WCC datasets
reference/             platform cheatsheet, data sources, gotchas, design notes
loader-sketches/       the two prep-kit sketches, for reference
enrichment/            prep-kit helpers (two bugs fixed — see below)
```

### Tests

```bash
python3 -m unittest discover tests -v      # 22 tests, ~0.4s
```

They cover the things that would break the demo or mislead the council:
acknowledgement fires without a human, status is derived rather than stored,
the log survives a restart (including a torn final line), grouping does what
the interface claims, and GeoJSON comes out lng/lat the right way round.

---

## The map

Plain SVG. Real WCC GeoJSON projected into a viewBox, about eighty lines in
`web/app.js`. No MapLibre, no Leaflet, no CDN.

That is deliberate. A `<script src="https://cdn...">` is a single point of
failure that fails exactly when the venue wifi does, which is during the demo.
`tools/fetch_basemap.py` bakes the geometry to `web/data/basemap.json`, so a
fresh clone renders **fully offline**.

Layers: the 19 tsunami evacuation zones, and all 60 Community Emergency Hubs.

---

## Two bugs fixed in the prep kit

Both in `enrichment/hazard_context.py`, both verified live against the WCC
services on 2026-08-08. Worth knowing about if you use that module elsewhere.

**Hubs came back with `name: None`.** The code read `Name` / `HubName` /
`FACILITY`; the live layer publishes `NAME` / `ADDRESS` / `SUBURB` in upper
case. All 60 hubs were anonymous.

**"Nearest" hub was not the nearest.** A `near=` query is a spatial *filter*,
not a sort — it returns what is inside the radius in arbitrary order, so
`limit=1` returned an arbitrary hub within 5 km. A Newtown report was told its
nearest hub was Aro Valley, about 2 km further than the right answer. Now all
candidates are fetched and sorted by haversine here. Telling someone the wrong
place to walk to during an emergency is not a cosmetic bug.

There is a third, unrelated trap in `enrichment/signal_helpers.py`: it does
`from wcc_impact import ...` at module scope, so it cannot be imported at all
without the SDK. `core/signals.py` is a standalone equivalent with the same
field names and limits.

---

## Where to pick up

Roughly in order of demo value.

- **Photos.** The form takes a photo *URL*; there is no upload. The platform
  has `upload_file()`; a local fallback needs a multipart handler in
  `server.py`.
- **Grouping is arithmetic** — same type, 250 m, 6 hours (`core/reports.py`,
  `ReportService.group`). Deliberately explainable to a duty officer, and it
  works with no API key. `ask_claude()` could group by *meaning* instead;
  keep the arithmetic path as the fallback.
- **Free-text triage.** `loader-sketches/track4_triage.py` classifies into
  action / verify / awareness with a priority. Wiring it in would give the
  WCC queue a sort order.
- **Notify the reporter.** Right now they have to have the page open. Web
  Push, or an SMS gateway, closes it properly.
- **The reporter cannot reply.** Genuinely two-way would let them answer
  "is it still blocked?" — a second signal type chaining to the same report.
- **Nothing is authenticated.** Anyone who can reach the server can set any
  status. Fine for a demo room, not for anything further. See below.

---

## Honest limitations

- **No authentication anywhere.** Any caller can move any report to any
  status. `run.py` binds to `127.0.0.1` for that reason; `--host 0.0.0.0`
  exposes it to the room and warns you when it does.
- **Not a live council service.** Nothing here is monitored, and no report
  filed into it reaches a real duty officer.
- **Hazard-planning layers, not live emergency information.** In an emergency,
  call 111.
- **The data is not ours.** Each dataset belongs to its publisher — WCC,
  Greater Wellington, GNS Science, NIWA, Wellington Water, MBIE, NZTA,
  MetService. Licences vary per dataset; check before publishing anything
  derived, and credit the publisher.
- **Be considerate with request rates.** These are council servers, and at
  least one throttles under concurrent load. Hazard lookups are cached per
  ~11 m and the basemap is baked, specifically to keep the request count down.
- **This repo is public and holds no personal information.** No participant
  names, no contact details, nothing from the application process. Please keep
  it that way — and note the seeded demo reports are invented, not real.

---

## Credit

Built on the Team 6 prep kit
([claudecommunity-nz/impact.lab.wlg.team-6.2026-08-08](https://github.com/claudecommunity-nz/impact.lab.wlg.team-6.2026-08-08)),
which carries the problem statement, the platform cheatsheet and the two
loader sketches from
[mikeartee/impact-lab-prep](https://github.com/mikeartee/impact-lab-prep).

`wcc_gis.py` and `catalogue.json` are unmodified from
[claudecommunity-nz/wcc-emergency-gis-data](https://github.com/claudecommunity-nz/wcc-emergency-gis-data)
— 74 catalogued datasets, [browsable here](https://claudecommunity-nz.github.io/wcc-emergency-gis-data/).

Code is MIT. The data is not covered by it.
