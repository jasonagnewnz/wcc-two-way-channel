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

from core.identity import CardStore, ROLES, card_event
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
         place_name="Ngauranga", severity="moderate", reporter_kind="resident",
         author_id="demo-priya"),
    dict(title="Hutt Road flooding getting deeper",
         description="Was ankle deep twenty minutes ago, now over the kerb.",
         issue_type="flooding", lat=-41.2434, lng=174.8112,
         place_name="Ngauranga", severity="severe", reporter_kind="resident",
         author_id="demo-priya"),
    dict(title="Cars stuck in water, Hutt Rd",
         description="Two vehicles stopped in the flooded section. Occupants are out and safe.",
         issue_type="flooding", lat=-41.2430, lng=174.8095,
         place_name="Ngauranga", severity="severe", reporter_kind="hub",
         author_id="demo-dave"),
    dict(title="Slip across the footpath on Wadestown Road",
         description="Mud and small rocks across the whole footpath, pedestrians on the road.",
         issue_type="slip-or-landslide", lat=-41.2660, lng=174.7710,
         place_name="Wadestown", severity="moderate", reporter_kind="resident",
         author_id="demo-ang"),
    dict(title="Power out along Aro Street",
         description="Whole block dark since about twenty past. Hub has opened.",
         issue_type="power-or-water", lat=-41.2950, lng=174.7690,
         place_name="Aro Valley", severity="moderate", reporter_kind="hub",
         author_id="demo-aro-valley-community-hub"),
    # Deliberately inside the mapped 1% AEP flood extent for Porirua Stream,
    # so the adaptation panel has a finding that is actually true rather than
    # a demo that only works because the numbers were chosen to work.
    dict(title="Stream coming up over the bank near the walkway",
         description="Water is out of the channel and across the path. Rising while I watch it.",
         issue_type="flooding", lat=-41.2124, lng=174.8229,
         place_name="Tawa", severity="severe", reporter_kind="resident",
         author_id="demo-priya"),
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

    # Advance a few more so the trust score has real corroboration to find:
    # these reporters had something an official chose to act on.
    for report in service.reports()[1:5]:
        service.set_status(report["id"], "reviewing",
                           note="Checked against the hazard layer.")
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

    _seed_live(store)
    _seed_bootstrap_card(store)

    agency_count = len(demo_data.AGENCY_MESSAGES)
    public_count = len(demo_data.PUBLIC_MESSAGES)
    print(f"  seeded message board: {agency_count} agency + {public_count} public "
          f"messages, 1 banner, 1 flagged message\n")


def issue_card(role: str, holder: str, store_path: str | None) -> int:
    """Mint a card from the command line.

    This is the bootstrap. Permissions are delegated downward from a card, so
    the first one has to come from somewhere with no card to authorise it —
    which means shell access to the server. That is the right root of trust:
    whoever can run this can already read the database.
    """
    if role not in ROLES:
        print(f"  Unknown role {role!r}. Choose from: {', '.join(ROLES)}")
        return 1

    store = SignalStore(store_path) if store_path else SignalStore()
    cards = CardStore()
    code, card = cards.issue(role=role, holder=holder, issued_by="console",
                             note="Issued from the command line.")
    card_event(store, action="issued", card_id=card["card_id"], role=role,
               holder=holder, actor="console")

    print()
    print("  ┌─────────────────────────────────────────────┐")
    print("  │  WELLINGTON EMERGENCY — ACCESS CARD         │")
    print("  ├─────────────────────────────────────────────┤")
    print(f"  │  {code:<41} │")
    print("  ├─────────────────────────────────────────────┤")
    print(f"  │  {ROLES[role]['label']:<20} {holder[:20]:<20} │")
    print("  └─────────────────────────────────────────────┘")
    print()
    print("  Write it down now. It is not stored and cannot be shown again —")
    print("  only its hash is kept, so a lost card is reissued, never recovered.")
    print(f"  Card id {card['card_id']} (use this to revoke it).")
    print()
    return 0


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


def _seed_live(store: SignalStore) -> None:
    """Published issues, requests for help, and WCC's answers to some of them."""
    import demo_data
    from core.liveops import LiveOpsService

    live = LiveOpsService(store)

    for title, detail, state, place, lat, lng, severity in demo_data.PUBLISHED_ISSUES:
        live.publish_issue(title=title, detail=detail, actor="Duty Controller",
                           state=state, lat=lat, lng=lng, place_name=place,
                           severity=severity)

    requests = []
    for need, detail, urgency, place, lat, lng, people, visibility, who in demo_data.HELP_REQUESTS:
        requests.append(live.request_help(
            need=need, detail=detail, urgency=urgency, place_name=place,
            lat=lat, lng=lng, people=people, visibility=visibility,
            author_id=f"demo-{who.lower().replace(' ', '-')}", author_name=who))

    # Deliberately not every request. One waiting with no answer is the honest
    # state of a real board, and it is what makes the answered ones mean
    # something.
    for index, likelihood, timeframe, note in demo_data.RESPONSES:
        live.post_update(requests[index]["id"], likelihood=likelihood,
                         timeframe=timeframe, note=note, actor="Duty Controller")

    for agency, category, title, body, area, link in demo_data.NEWS:
        live.post_news(title=title, body=body, agency=agency, category=category,
                       area=area, link=link, actor="Duty Controller")

    print(f"  seeded {len(demo_data.NEWS)} news updates")
    print(f"  seeded {len(demo_data.PUBLISHED_ISSUES)} published issues, "
          f"{len(requests)} help requests, {len(demo_data.RESPONSES)} WCC responses")


def _seed_demo_cards(store: SignalStore) -> None:
    """Install the published demo cards unless told not to."""
    if _SKIP_DEMO_CARDS:
        print("  Demo cards skipped (--no-demo-cards).")
        return
    from demo_cards import seed_demo_cards
    created = seed_demo_cards(CardStore(), store)
    if created:
        print(f"  Seeded {len(created)} PUBLIC demo cards — codes are in DEMO_CARDS.md.")
        print("  Anyone who reads the repo can sign in with them. Revoke with")
        print("  `python3 run.py --revoke-demo-cards` if that stops being what you want.")


_SKIP_DEMO_CARDS = False


def _seed_bootstrap_card(store: SignalStore) -> None:
    """Mint one coordinator card so a fresh clone can demo the official side.

    Printed to the console only. On a public deployment this is the card that
    matters most — anyone reading the startup logs holds the top role — so it
    is minted once and never reprinted.
    """
    cards = CardStore()
    if any(c["role"] == "coordinator" for c in cards.cards()):
        return
    code, card = cards.issue(role="coordinator", holder="Duty Coordinator",
                             issued_by="seed", note="Seeded for the demo.")
    card_event(store, action="issued", card_id=card["card_id"], role="coordinator",
               holder="Duty Coordinator", actor="seed")
    print()
    print("  ╔══════════════════════════════════════════════════╗")
    print("  ║  DEMO COORDINATOR CARD — write this down          ║")
    print(f"  ║  {code:<48}║")
    print("  ╚══════════════════════════════════════════════════╝")
    print("  Sign in with it under 'Sign in with a card'. Shown once only.")


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
    parser.add_argument("--no-demo-cards", action="store_true",
                        help="seed everything except the published demo cards")
    parser.add_argument("--seed-demo-cards", action="store_true",
                        help="install the published demo cards on a running "
                             "instance without touching any other data, then exit")
    parser.add_argument("--revoke-demo-cards", action="store_true",
                        help="cancel the published demo cards and their sessions, then exit")
    parser.add_argument("--issue-card", nargs=2, metavar=("ROLE", "HOLDER"),
                        help="mint an auth card and print it, then exit "
                             f"(roles: {', '.join(ROLES)})")
    parser.add_argument("--yes", action="store_true",
                        help="skip the confirmation prompt on --reset")
    args = parser.parse_args(argv)

    global _SKIP_DEMO_CARDS
    _SKIP_DEMO_CARDS = args.no_demo_cards

    if args.seed_demo_cards:
        store = SignalStore(args.store) if args.store else SignalStore()
        _seed_demo_cards(store)
        from demo_cards import DEMO_CARDS
        print()
        for role, holder, code, _ in DEMO_CARDS:
            print(f"  {code}   {ROLES[role]['label']:<22} {holder}")
        print()
        return 0

    if args.revoke_demo_cards:
        from demo_cards import revoke_demo_cards
        store = SignalStore(args.store) if args.store else SignalStore()
        count = revoke_demo_cards(CardStore(), store)
        print(f"  Cancelled {count} demo card(s). Anyone signed in with one is signed out.")
        return 0

    if args.issue_card:
        return issue_card(args.issue_card[0], args.issue_card[1], args.store)

    if args.reset:
        return reset(args.store, assume_yes=args.yes)

    if args.seed or args.seed_only:
        seed(args.store)
        # Outside seed(), which returns early when the store already has
        # reports. Nested, the demo cards silently never installed on any
        # instance that already had data — which is every running one.
        _seed_demo_cards(SignalStore(args.store) if args.store else SignalStore())
    if args.seed_only:
        return 0

    if args.host == "0.0.0.0":  # noqa: S104 - deliberate, and warned about
        print("\n  ⚠  Bound to 0.0.0.0 — anyone on this network can read and file reports.")
        print("     There is no authentication in this prototype. Fine for a demo room.")

    serve(host=args.host, port=args.port, store_path=args.store)
    return 0


if __name__ == "__main__":
    sys.exit(main())
