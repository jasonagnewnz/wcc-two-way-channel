"""Demo access cards — published on purpose, so anyone can try the other side.

⚠ THESE CODES ARE PUBLIC AND WORK.

Every card below is seeded by `run.py --seed` and the codes are printed in
DEMO_CARDS.md, so a judge, a teammate, or anyone who clones the repo can sign
in as a coordinator and see the official surfaces instead of taking a
screenshot's word for it.

The names are invented. They are not Wellington City Council staff, not
Impact Lab participants, and not anybody.

What that means on a public deployment
--------------------------------------
Anyone who reads the repo can act as a coordinator on that instance: post in
agency channels, publish the banner, move report statuses, and issue further
cards. That is the intended trade for a demo, and it is not a trade you would
make for anything real.

Two ways out, both one step:

  python3 run.py --seed --no-demo-cards     # seed everything except these
  python3 run.py --revoke-demo-cards        # cancel them on a running instance

`--revoke-demo-cards` cancels the cards and kills their live sessions, so it
is the switch to reach for if a public instance starts being played with.
"""

from __future__ import annotations

# (role, holder, code, note)
#
# Codes are fixed rather than generated so the README can print them. Each one
# carries a valid check character, so they behave exactly like a real printed
# card — including rejecting a typo as a typo.
DEMO_CARDS = [
    ("coordinator", "Alex Whitcombe", "WCC-JYCE-VWQE-U3PX-S2DY",
     "Demo: emergency coordinator. Can do everything, including issuing cards."),
    ("official", "Sam Tuilagi", "WCC-BNWD-M9VS-QSBU-JHMH",
     "Demo: WCC Emergency Management duty officer."),
    ("official", "Robin Kearsley", "WCC-S58S-Z48T-ZRJG-KMEJ",
     "Demo: second official, so two people can run the agency wall at once."),
    ("hub-lead", "Moana Reti", "WCC-NYUX-HEAH-SFXT-9XD9",
     "Demo: Aro Valley Community Emergency Hub lead."),
    ("moderator", "Tessa Iremonger", "WCC-U69C-USPV-J6TP-CRG6",
     "Demo: community moderator. Can flag and approve, cannot issue cards."),
    ("verified", "Danny Okafor", "WCC-BG6B-T6MH-UMSN-R7B3",
     "Demo: verified resident. Higher rate limits, no moderation powers."),
]

DEMO_ISSUER = "demo-seed"


def seed_demo_cards(cards, store=None) -> list[dict]:
    """Install the published demo cards. Idempotent."""
    from core.identity import card_event, hash_code

    existing = {c.get("card_id") for c in cards.cards()}
    created = []

    for role, holder, code, note in DEMO_CARDS:
        # issue() mints its own random code, so plant these directly. Same
        # record shape, same hashing — the plaintext is still never stored.
        if any(c.get("holder") == holder and not c.get("revoked")
               for c in cards.cards()):
            continue

        card = cards.plant(code=code, role=role, holder=holder,
                           issued_by=DEMO_ISSUER, note=note)
        created.append(card)
        if store is not None and card["card_id"] not in existing:
            card_event(store, action="issued", card_id=card["card_id"],
                       role=role, holder=holder, actor=DEMO_ISSUER)

    return created


def revoke_demo_cards(cards, store=None) -> int:
    """Cancel every demo card and kill its sessions. Returns how many."""
    from core.identity import card_event

    revoked = 0
    for card in cards.cards():
        if card.get("issued_by") == DEMO_ISSUER and not card.get("revoked"):
            cards.revoke(card["card_id"], by="console")
            revoked += 1
            if store is not None:
                card_event(store, action="revoked", card_id=card["card_id"],
                           role=card["role"], holder=card["holder"],
                           actor="console")
    return revoked
