# CLAUDE.md — Community ↔ WCC two-way channel

Context for Claude Code working in this repo.

## What this is

Team 6's build for **Problem 02** at Impact Lab Wellington (Saturday
8 August 2026, Wellington City Council): a two-way information channel
between communities and WCC. Communities report local conditions; WCC
acknowledges and shows what is happening. See `README.md` for the product
argument and `reference/report-status-design.md` for the original design
reasoning.

Build time is roughly six and a half hours. **A narrow thing that works beats
a broad thing that does not demo.** Judging is a four-minute demo at 16:30;
submissions close at 16:00.

## Hard constraints — do not break these

- **No dependencies.** Standard library only, in Python and in the browser.
  No `pip install`, no npm, no CDN script tags, no build step. This is not
  minimalism for its own sake: every dependency is a laptop that does not
  work and a demo that dies with the venue wifi. If you think you need a
  package, you almost certainly do not.
- **The signal log is append-only.** Never edit a stored signal. Status
  changes publish a *new* `report-status` signal chaining to the original via
  `raw.original_signal_id`. Current status is derived by replaying the chain.
  This mirrors the platform (`publish_signal` exists, `update_signal` does
  not) and gives a real audit trail.
- **Never present inference as fact.** Hazard context is looked up from WCC
  planning layers and is labelled as inferred everywhere it appears. Several
  of these problem statements are explicitly about making limitations
  visible.
- **Hazard-planning data, not live emergency information.** Nothing built
  here may be presented as an operational emergency source. In an emergency,
  call 111. The disclaimer stays on screen.
- **This repo is public and holds no personal information.** No participant
  names, contact details, or anything from the application process. The
  seeded demo reports are invented.
- **Escape everything rendered into HTML.** `web/app.js` renders
  community-submitted text. Every value interpolated into a template goes
  through `esc()`. That is the one genuinely hostile input surface here.
- **Enrichment must never block or fail a report.** Losing hazard context is
  an inconvenience; losing a report is the thing this exists to prevent.
- **Permissions are decided by the server, from the card on the request.**
  The client may say who it is; it may never say what it is allowed to do.
  `author_role` and `?viewer=official` were both once taken from the client
  and both were holes. If you add an endpoint, derive the role from
  `self.session()`.
- **Enforce at the boundary, not at the renderer.** `/api/signals` leaked
  private messages for a while because the filter lived in
  `ChatService.messages()` and that endpoint did not call it. Any new public
  read path over the log goes through `redact_for_public`.
- **Card secrets are never signals.** The signal log is served over HTTP.
  Codes live in `data/cards.jsonl`, hashed, served by nothing. Card *events*
  go in the log without the code.
- **Automation may never grant a role that can issue cards.** `auto_promote`
  asserts this at runtime. If you raise `AUTO_PROMOTE_MAX_ROLE`, you are
  removing the only thing that stops a heuristic manufacturing officials.

## Shape

```
run.py       entrypoint            python3 run.py --seed
server.py    stdlib HTTP + JSON API
loader.py    platform contract: main() / tick() / sample()
core/        signals.py · store.py · reports.py · hazard.py
web/         index.html · styles.css · app.js · data/basemap.json
tests/       python3 -m unittest discover tests
tools/       fetch_basemap.py
```

`ReportService` (core/reports.py) is the whole domain. It takes a store, so
it runs unchanged against the local JSONL log or against `wcc_impact` via
`PlatformStore` in `loader.py`. **Keep that seam.** It is what makes the repo
useful before the SDK exists and correct after it arrives.

## Working conventions

- **Run the tests.** `python3 -m unittest discover tests`. They are fast
  (~0.4s) and they cover the things that would mislead the council rather
  than chasing coverage. Do not weaken one to make a change pass.
- **Verify against the live services, don't assume field names.** Two bugs in
  the carried-over `enrichment/hazard_context.py` were exactly this: guessed
  property names (`Name` vs the real `NAME`) and an assumption that a `near=`
  query sorts by distance (it does not — it filters). Probe the real layer.
- **Be considerate with council servers.** Hazard lookups are cached per
  ~11 m; the basemap is baked to disk. Keep it that way.
- **wcc_gis.py and catalogue.json are vendored unmodified.** If they need a
  fix, fix it upstream and re-vendor; do not fork them quietly.
- **Keep the README's problem statement in sync** if scope shifts during the
  day. The repo is the submission.
- **Commit early and often.**

## Composability

The brief wants modules that feed one shared common operating picture, and
outputs that compose over closed-off UIs. `/api/geojson` and `/api/signals`
are that contract — CORS-open, no key. If you add data, ask whether another
team's map could read it.
