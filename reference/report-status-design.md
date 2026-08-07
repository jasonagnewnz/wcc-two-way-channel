# Report-status design — the "reporting back" loop

Working design for the part of Problem 02 with no existing sketch: letting a
reporter see that their report has been received and acted on, without
calling the council to ask ("a third of council call-centre calls are status
checks" — the actual problem this solves).

Model: **Uber Eats delivery tracking**, not a support ticket. One tap from
staff becomes a push update; the reporter never refreshes or calls back in.

## Load-bearing assumption: there's an existing host app we plug into

Everything below assumes the platform is already a built application — a
live map with a module registry — that our loader and React component slot
into, rather than something we build from scratch. Not confirmed by directly
seeing it (searched the whole `claudecommunity-nz` GitHub org, nothing public
called `plugin-sdk` or similar — it's gated to the day, like `wcc_impact`),
but the API shape all points the same way:

- It's named **`plugin-sdk`**, not `app-sdk` — a component that mounts into
  something, not a thing that creates the something.
- `register_module(id, name, icon, description)` only makes sense against an
  existing shell that lists modules by icon/name.
- `useModuleConfig()` implies a host that stores and injects per-module
  settings — plumbing we're not writing.
- The README describes **one** shared map fed by all ten teams, not ten
  separate apps.
- The golden-path `cd modules/your-team && python loader.py` implies our code
  lives inside a larger existing project structure.

If this assumption is wrong — if tomorrow's kit is actually a blank React
starter with no host — the "don't build your own dashboard" advice in this
doc inverts, and the reporter-facing view becomes something we do have to
build standalone. Confirm this first, before anything else, at 09:00.

## Why append-only, not update-in-place

`wcc_impact`'s exported surface is `publish_signal` (insert), `fetch_signals`
(read), `on_new_signals` (real-time read) — no `update_signal`. Combined with
"durable outbox is the default" in the platform cheatsheet, this reads as an
append-only log, not mutable rows. `track4_triage.py` already establishes the
pattern for this: it doesn't edit the signal it's triaging, it publishes a
*new* signal carrying `raw: {"original_signal_id": <id>, ...}`. Reuse that
convention rather than inventing a new one.

**Unverified — confirm on the day**: whether the real `wcc_impact` actually
has no update path. If it does, this whole design collapses to something
simpler (mutate a status field directly). Check this in the first 30 minutes
of build time before committing to signal-chaining.

## The three pieces

**1. Reference code, not a login.**
No auth exists anywhere in the platform docs. On submit, `publishSignal()`
returns the new signal's id — show it to the reporter as a reference code and
store it in `localStorage`. Possession of the code is the only "auth" there
is.

**2. Status updates are one-tap, not free text.**
A fixed, small vocabulary — `received` (fires automatically the instant the
report lands, no human needed) → `reviewing` → `responding` → `resolved`.
Deliberately clear of track4's `action`/`verify`/`awareness` + priority 1–5
vocabulary — same underlying classify-with-Claude trick, different scope
(this team's own reporters, not a citywide ops ranking), so it gets its own
words rather than reusing track4's. Each tap publishes a `report-status`
signal:

```python
publish_signal(
    module_id=MODULE_ID,
    signal_type="report-status",
    source_type="official",
    title=f"Status: {status}",
    raw={"original_signal_id": original_id, "status": status},
    ...
)
```

Minimal friction on the WCC/field side — same shape as a delivery driver
tapping "heading out", not typing a sentence. Could be a button on the shared
map itself (click the pin, tap a status) rather than a separate WCC app.

**3. Reporter view is push, not poll.**
The "check my report" view opens `on_new_signals(signal_type="report-status")`
(or `useSignals` if it's wired to the same realtime channel — see open
question below), filters client-side to `raw.original_signal_id === myCode`,
and renders the latest one. The screen updates itself the moment staff tap a
button. No manual refresh, no calling in.

## Deliberately out of scope

An Uber-Eats-style ETA implies a moving vehicle converging on a point — WCC
doesn't have that without pulling in `track5`'s movement tracking for field
crews, which is out of scope for a 4-minute demo. "Current state, updated
live" is the whole win; skip ETA.

## Open questions for 09:00 briefing

- Does `publish_signal` support update-in-place, or is it genuinely
  insert-only? (Determines whether signal-chaining is necessary at all.)
- Is `useSignals()` in `plugin-sdk` realtime-backed, or a polling hook under
  the hood? Design the "instant update" demo moment around whichever it
  actually is — `run_every`'s 5s minimum is the polling fallback if realtime
  isn't wired into the hook.
- Does `raw` support the kind of client-side filtering this design assumes
  (`fetch_signals`/`useSignals` returning full `raw` payloads, not just
  indexed fields)?
- Does `publish_signal()` return the signal it just created? The cheatsheet
  only says "→ publishes to Supabase," nothing about a return value —
  `submit_report()` in `report_status_loader.py` assumes it gets the new
  signal's `id` back to hand to the reporter as their reference code. If it
  returns nothing, the id has to come from somewhere else (e.g. the caller
  generates its own UUID up front and includes it in the published fields).
- Does WCC's call centre already offload after-hours/weekend load to
  another provider — e.g. Palmerston North City Council's shared
  after-hours hub (a real, documented service used by ~27–28 NZ councils,
  confirmed for Christchurch City Council, unconfirmed for WCC), or
  Greater Wellington Regional Council? This changes who the report-status
  loop actually relieves: if WCC's after-hours calls already go to a
  subcontracted provider that never sees our signals, the "reduces
  double-handling" pitch needs to be scoped to WCC's own daytime team, not
  after-hours coverage generally.
