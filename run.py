#!/usr/bin/env python3
"""Start the two-way channel.

    python3 run.py              # http://127.0.0.1:8080
    python3 run.py --seed       # ...with a few demo reports already in
    python3 run.py --port 9000
    python3 run.py --host 0.0.0.0    # reachable from the room (see below)

Standard library only. No install step.

--host 0.0.0.0 binds to every interface so someone else on the venue wifi can
open it on their phone, which makes for a much better demo than a laptop
screen. It is off by default: this prototype has no authentication, so
exposing it is a decision you make on purpose, not one that happens to you.
"""

from __future__ import annotations

import argparse
import sys

from core.reports import ReportService
from core.store import SignalStore
from server import serve

# A handful of plausible Wellington reports so a fresh clone has something on
# the map. Chosen to demonstrate grouping: the first three are the same issue
# type within 250 m of each other, so they collapse into one cluster the way
# a real flood would generate several reports of one event.
DEMO_REPORTS = [
    dict(title="Water over the road on Hutt Road",
         description="Southbound lane is under water near the Ngauranga end. Cars turning around.",
         issue_type="flooding", lat=-41.2432, lng=174.8100,
         place_name="Ngauranga", severity="moderate", reporter_kind="resident"),
    dict(title="Hutt Road flooding getting deeper",
         description="Was ankle deep twenty minutes ago, now over the kerb.",
         issue_type="flooding", lat=-41.2434, lng=174.8112,
         place_name="Ngauranga", severity="severe", reporter_kind="resident"),
    dict(title="Cars stuck in water, Hutt Rd",
         description="Two vehicles stopped in the flooded section. Occupants are out and safe.",
         issue_type="flooding", lat=-41.2430, lng=174.8095,
         place_name="Ngauranga", severity="severe", reporter_kind="hub"),
    dict(title="Slip across the footpath on Wadestown Road",
         description="Mud and small rocks across the whole footpath, pedestrians on the road.",
         issue_type="slip-or-landslide", lat=-41.2660, lng=174.7710,
         place_name="Wadestown", severity="moderate", reporter_kind="resident"),
    dict(title="Power out along Aro Street",
         description="Whole block dark since about twenty past. Hub has opened.",
         issue_type="power-or-water", lat=-41.2950, lng=174.7690,
         place_name="Aro Valley", severity="moderate", reporter_kind="hub"),
    dict(title="Elderly resident needs help getting out",
         description="Ground floor flat taking water, resident uses a walker and cannot manage the step.",
         issue_type="people-need-help", lat=-41.3110, lng=174.7810,
         place_name="Island Bay", severity="severe", reporter_kind="community-group"),
]


def seed(store_path: str | None) -> None:
    store = SignalStore(store_path) if store_path else SignalStore()
    service = ReportService(store)
    existing = len(service.reports())
    if existing:
        print(f"  Store already has {existing} reports — not seeding again.")
        print("  Delete data/signals.jsonl first if you want a clean slate.\n")
        return

    for entry in DEMO_REPORTS:
        report = service.submit_report(**entry)
        print(f"  seeded {report['id']}  {report['title'][:52]}")

    # Move one along so the demo opens with a report already in progress,
    # rather than every pin sitting at "received".
    first = service.reports()[0]["id"]
    service.set_status(first, "reviewing", note="Duty officer checking against the flood layer.")
    service.set_status(first, "responding", note="Contractor dispatched to close the lane.")
    print(f"\n  {first} advanced to 'responding' so the timeline has something in it.")

    seed_chat(store)


def seed_chat(store: SignalStore) -> None:
    """Fill the message board. Every message is invented — see demo_data.py."""
    import demo_data
    from core.chat import ChatService

    chat = ChatService(store)

    for channel, author, agency, body, visibility in demo_data.AGENCY_MESSAGES:
        chat.post(channel_id=channel, body=body, author_name=author,
                  author_id=f"demo-{channel}", author_role="official",
                  agency=agency, visibility=visibility)

    last_newtown = None
    for channel, author, role, body, visibility in demo_data.PUBLIC_MESSAGES:
        message = chat.post(channel_id=channel, body=body, author_name=author,
                            author_id=f"demo-{author.lower().replace(' ', '-')}",
                            author_role=role,
                            agency=("Wellington City Council" if role == "official" else None),
                            visibility=visibility)
        if channel == "newtown":
            last_newtown = message["id"]

    chat.set_banner(**demo_data.BANNER, actor="wcc-duty-controller")

    # Flag the last Newtown message so the demo has a worked example of
    # moderation: out of the public feed, marker left behind, flag in the log.
    if last_newtown:
        chat.flag(last_newtown, reason=demo_data.FLAG_LAST_NEWTOWN_REASON,
                  actor="wcc-moderator")

    agency_count = len(demo_data.AGENCY_MESSAGES)
    public_count = len(demo_data.PUBLIC_MESSAGES)
    print(f"  seeded message board: {agency_count} agency + {public_count} public "
          f"messages, 1 banner, 1 flagged message\n")


def reset(store_path: str | None, *, assume_yes: bool = False) -> int:
    """Wipe the log. There is no undo, so it asks first unless told not to.

    On a public demo this is the "clear the graffiti before judging" button:

        python3 run.py --reset --yes && python3 run.py --seed-only
    """
    store = SignalStore(store_path) if store_path else SignalStore()
    count = store.count()
    if not count:
        print("  Log is already empty.")
        return 0

    if not assume_yes:
        print(f"\n  This deletes all {count} signals in {store.path}.")
        print("  There is no undo.")
        if input("  Type 'reset' to confirm: ").strip().lower() != "reset":
            print("  Cancelled — nothing was deleted.\n")
            return 1

    discarded = store.reset()
    print(f"  Deleted {discarded} signals. Log is empty.\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="WCC two-way community channel")
    parser.add_argument("--host", default="127.0.0.1",
                        help="bind address (default 127.0.0.1; 0.0.0.0 exposes it to the network)")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--store", default=None,
                        help="path to the signal log (default data/signals.jsonl)")
    parser.add_argument("--seed", action="store_true",
                        help="add demo reports if the store is empty, then serve")
    parser.add_argument("--seed-only", action="store_true",
                        help="add demo reports and exit without serving")
    parser.add_argument("--reset", action="store_true",
                        help="DELETE every signal, then exit. Use before a demo.")
    parser.add_argument("--yes", action="store_true",
                        help="skip the confirmation prompt on --reset")
    args = parser.parse_args(argv)

    if args.reset:
        return reset(args.store, assume_yes=args.yes)

    if args.seed or args.seed_only:
        seed(args.store)
    if args.seed_only:
        return 0

    if args.host == "0.0.0.0":  # noqa: S104 - deliberate, and warned about
        print("\n  ⚠  Bound to 0.0.0.0 — anyone on this network can read and file reports.")
        print("     There is no authentication in this prototype. Fine for a demo room.")

    serve(host=args.host, port=args.port, store_path=args.store)
    return 0


if __name__ == "__main__":
    sys.exit(main())
