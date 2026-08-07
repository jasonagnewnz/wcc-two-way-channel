# Why these two sketches

Pulled in from [impact-lab-prep](https://github.com/mikeartee/impact-lab-prep) as a starting point for
Problem 02 (two-way community ↔ WCC channel):

- **track2_community.py** — covers the "communities report an issue" half: polls a feed,
  classifies with Claude, geocodes, publishes a signal. Swap the scenario social feed for
  whatever intake we actually build (form submission, hub report, etc).
- **track4_triage.py** — covers the "WCC groups similar reports" half: category
  (action/verify/awareness), priority, dedup by title + location.

## What's missing — this is our actual build

Neither sketch closes the loop back to the *original reporter*. Problem 02 explicitly asks for
communities to "see that their information has been received" — that acknowledgment path
(status per report, visible to the person who filed it) isn't in the prep kit. That's the novel
part of tomorrow's build, not a copy-paste job.

Before running either file: replace `MODULE_ID = "team-CHANGEME"` with our assigned module ID
(provided on the day), per `reference/platform_cheatsheet.md`.
