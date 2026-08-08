# Demo access cards

**These codes are public and they work.** Sign in at
**https://impact-lab.bitn.cloud** — or on your own copy after
`python3 run.py --seed`.

Three ways in, in order of how little they slow you down:

1. **Tap one.** Hit *Sign in with a card* and the demo cards are listed as
   buttons. One tap signs you in.
2. **Follow a link.** `https://impact-lab.bitn.cloud/?card=WCC-JYCE-VWQE-U3PX-S2DY`
   signs in and removes the code from the address bar.
3. **Type it.** Case, spaces and dashes are all optional —
   `wccjycevwqeu3pxs2dy` works.

| Code | Role | Name | What it can do |
|---|---|---|---|
| `WCC-JYCE-VWQE-U3PX-S2DY` | Emergency coordinator | Alex Whitcombe | Everything, including issuing cards up to *official* |
| `WCC-BNWD-M9VS-QSBU-JHMH` | Official | Sam Tuilagi | Agency channels, the banner, report statuses, issue up to *hub lead* |
| `WCC-S58S-Z48T-ZRJG-KMEJ` | Official | Robin Kearsley | As above — a second one, so two people can run the agency wall at once |
| `WCC-NYUX-HEAH-SFXT-9XD9` | Emergency hub lead | Moana Reti | Report statuses, flagging, issue up to *moderator*. No agency channels |
| `WCC-U69C-USPV-J6TP-CRG6` | Community moderator | Tessa Iremonger | Flag and unflag. **Cannot** issue cards |
| `WCC-BG6B-T6MH-UMSN-R7B3` | Verified resident | Danny Okafor | Post with higher rate limits. No moderation |

**Every name above is invented.** They are not Wellington City Council staff,
not Impact Lab participants, and not anybody.

## Worth trying

- Sign in as **Tessa** (moderator) and as **Alex** (coordinator), then open
  *Cards & trust*. Tessa cannot reach it at all; Alex can, and the permission
  levels she may issue stop at *Official* — **no card can mint its own
  level or above**, so delegation always loses privilege.
- Sign in as **Moana** (hub lead) and look for the Agency wall tab. It is not
  there. A public request for an agency channel is a `403`, not a filtered
  list.
- Sign out entirely and try to publish the banner. `401`.
- As any moderator, flag a message on the public board. It leaves the feed and
  **a visible marker stays behind** — nothing is ever deleted.

## What this means on the public instance

Anyone who reads this file can act as a coordinator on
https://impact-lab.bitn.cloud: post in agency channels, publish the banner,
move report statuses, and issue further cards.

That is the intended trade for a demo — the whole point is that you can try
the official side rather than take a screenshot's word for it. It is not a
trade you would make for anything real.

Two switches, both one step:

```bash
python3 run.py --seed --no-demo-cards      # seed everything except these
python3 run.py --revoke-demo-cards         # cancel them on a running instance
python3 run.py --seed-demo-cards           # put them back
```

`--revoke-demo-cards` cancels the cards *and* kills any live session opened
with one.

## Your own cards

The first card on a fresh instance comes from the command line, because
permissions delegate downward and the root has nothing above it to authorise
it:

```bash
python3 run.py --issue-card coordinator "Your Name"
```

The code is printed once and never stored — only its SHA-256 hash is kept, in
`data/cards.jsonl`, which no endpoint serves. A lost card is reissued, never
recovered.
