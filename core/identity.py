"""Offline auth cards, delegation, and earned trust.

The problem this solves
-----------------------
Everything else here runs on possession: a reference code proves you filed a
report, an author_id shows you your own messages. That is right for the public
and wrong for officials — until now anyone could claim the official role and
put words in an agency's mouth.

An emergency can't rely on an online identity provider. The network is the
thing that fails. So: **a printed card in a wallet**. WCC prints them, hands
them to hub coordinators and staff in advance, and on the day you type the
code off the card. No email, no SMS, no SSO round-trip, no internet — just
this server on whatever network survives.

Be precise about "offline": the card removes the dependency on an *external*
identity provider, and works on an isolated local network. The client still
has to reach this server. A card that verified with no server at all would
need signed, self-contained credentials and a way to check revocation, which
is a different and much larger design.

What a card is
--------------
A bearer credential. Whoever holds it is that person — same as a hotel key.
That is the deliberate trade: something usable by a stressed volunteer at 3am
with no signal, at the cost of being only as secure as the wallet. Which is
why cards are revocable, scoped to one role, and never grant more than they
must.

Storage
-------
Card secrets live in their own file and are served by **no endpoint**. They
are never signals, because the signal log is exposed over HTTP — see
`redact_for_public` in core/chat.py for what happens when a filter lives in
only one read path. What does go in the signal log is the *event*: card
issued, redeemed, revoked, promoted, with the card's id and role but never its
code. Delegation stays auditable; the secret stays out of the audit stream.

Codes are stored as SHA-256 of the normalised code. No KDF: a code carries
~74 bits of entropy, so there is no dictionary to attack and nothing for a
slow hash to buy. The hash is there so a leaked card file is not a stack of
working credentials.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import time
from pathlib import Path

from .signals import make_signal, utc_now

# Same unambiguous alphabet as the reference codes: no O/0, no I/1/L. These
# are read off a printed card, often in bad light by someone in a hurry.
_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_GROUPS = 4
_GROUP_LEN = 4

CARDS_PATH = Path(__file__).resolve().parent.parent / "data" / "cards.jsonl"

# How long a redeemed card stays signed in on that device.
SESSION_TTL_SECONDS = 12 * 3600


# ---------------------------------------------------------------------------
# roles
# ---------------------------------------------------------------------------

# post.public      write to a public board
# post.agency      write inside an inter-agency channel
# moderate.flag    flag / unflag a public message
# report.status    move a report through its statuses
# banner.publish   push the important-comms banner
# card.issue       mint a card for somebody else, up to `max_issue`

ROLES: dict[str, dict] = {
    "resident": {
        "rank": 0,
        "label": "Resident",
        "permissions": {"post.public"},
        "max_issue": None,
    },
    "verified": {
        "rank": 1,
        "label": "Verified resident",
        "permissions": {"post.public"},
        "max_issue": None,
    },
    "moderator": {
        "rank": 2,
        "label": "Community moderator",
        "permissions": {"post.public", "moderate.flag"},
        "max_issue": None,
    },
    "hub-lead": {
        "rank": 3,
        "label": "Emergency hub lead",
        "permissions": {"post.public", "moderate.flag", "report.status", "card.issue"},
        "max_issue": "moderator",
    },
    "official": {
        "rank": 4,
        "label": "Official",
        "permissions": {"post.public", "post.agency", "moderate.flag",
                        "report.status", "banner.publish", "card.issue"},
        "max_issue": "hub-lead",
    },
    "coordinator": {
        "rank": 5,
        "label": "Emergency coordinator",
        "permissions": {"post.public", "post.agency", "moderate.flag",
                        "report.status", "banner.publish", "card.issue"},
        "max_issue": "official",
    },
}

# The ceiling on automation. A bot may promote somebody to moderator and no
# further — and note `moderator` has no `card.issue`, so a bot-granted role
# cannot mint anything. That containment is the whole safety argument for
# letting a heuristic hand out permissions at all: the worst case is a badly
# chosen moderator, never a manufactured official.
AUTO_PROMOTE_MAX_ROLE = "moderator"


def role_rank(role: str) -> int:
    return ROLES.get(role, ROLES["resident"])["rank"]


def permissions_for(role: str) -> set[str]:
    return set(ROLES.get(role, ROLES["resident"])["permissions"])


def can(role: str, permission: str) -> bool:
    return permission in permissions_for(role)


def can_issue(issuer_role: str, target_role: str) -> bool:
    """May `issuer_role` mint a card for `target_role`?

    Bounded by an explicit `max_issue` per role rather than "anything below
    me". Explicit beats derived here: this is the rule that stops a leaked
    mid-level card from manufacturing its way upward, so it should be readable
    at a glance rather than inferred from arithmetic.
    """
    ceiling = ROLES.get(issuer_role, {}).get("max_issue")
    if ceiling is None or target_role not in ROLES:
        return False
    return role_rank(target_role) <= role_rank(ceiling)


# ---------------------------------------------------------------------------
# code format
# ---------------------------------------------------------------------------

def _checksum(body: str) -> str:
    """One check character, so a mistyped card is rejected as a typo rather
    than silently looked up and reported as 'unknown card'. The difference
    matters when the person typing is stressed and the light is bad.
    """
    total = sum(_ALPHABET.index(c) * (i + 1) for i, c in enumerate(body))
    return _ALPHABET[total % len(_ALPHABET)]


def new_code() -> str:
    """A printed card code: WCC-XXXX-XXXX-XXXX, last character a checksum.

    15 random characters over a 31-character alphabet is about 74 bits — far
    past brute force, even before the attempt throttling in redeem().
    """
    body = "".join(secrets.choice(_ALPHABET)
                   for _ in range(_GROUPS * _GROUP_LEN - 1))
    full = body + _checksum(body)
    groups = [full[i:i + _GROUP_LEN] for i in range(0, len(full), _GROUP_LEN)]
    return "WCC-" + "-".join(groups)


def normalise(code: str) -> str:
    """Strip everything a human might add: spaces, dashes, lower case, the
    WCC prefix. A card typed as 'wcc a1b2 c3d4' should work.
    """
    cleaned = "".join(ch for ch in (code or "").upper() if ch.isalnum())
    if cleaned.startswith("WCC"):
        cleaned = cleaned[3:]
    return cleaned


def code_looks_valid(code: str) -> bool:
    body = normalise(code)
    if len(body) != _GROUPS * _GROUP_LEN:
        return False
    if any(ch not in _ALPHABET for ch in body):
        return False
    return _checksum(body[:-1]) == body[-1]


def hash_code(code: str) -> str:
    return hashlib.sha256(normalise(code).encode("ascii")).hexdigest()


# ---------------------------------------------------------------------------
# the card store
# ---------------------------------------------------------------------------

class CardStore:
    """Cards and sessions. Deliberately NOT the signal log.

    Never exposed by an endpoint. If you add one, don't.
    """

    def __init__(self, path: str | Path = CARDS_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # RLock, not Lock: redeem() records a failed attempt while already
        # holding the lock, and a plain Lock is not reentrant — that
        # deadlocked the thread AND left the lock held, so one mistyped card
        # code froze every subsequent card operation on the server. Found by
        # the throttling test hanging.
        self._lock = threading.RLock()
        self._cards: dict[str, dict] = {}      # code_hash -> card
        self._by_id: dict[str, dict] = {}      # card_id -> card
        self._sessions: dict[str, dict] = {}   # token -> {card_id, expires}
        self._attempts: dict[str, list[float]] = {}  # client -> failure times
        self._load()

    # -- persistence -------------------------------------------------------

    def _load(self) -> None:
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    card = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # Later lines supersede earlier ones for the same card, which
                # is how revocation persists without rewriting the file.
                self._cards[card["code_hash"]] = card
                self._by_id[card["card_id"]] = card

    def _append(self, card: dict) -> None:
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(card, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    # -- issuing -----------------------------------------------------------

    def issue(self, *, role: str, holder: str, issued_by: str = "system",
              note: str = "", subject: str | None = None) -> tuple[str, dict]:
        """Mint a card. Returns (plaintext code, card record).

        The plaintext is returned exactly once, here, so it can be printed. It
        is not stored and cannot be recovered — a lost card is reissued, never
        looked up.
        """
        if role not in ROLES:
            raise ValueError(f"unknown role {role!r}")

        code = new_code()
        card = {
            "card_id": "CARD-" + secrets.token_hex(4).upper(),
            "code_hash": hash_code(code),
            "role": role,
            "holder": (holder or "Unnamed").strip()[:80],
            "issued_by": issued_by,
            "issued_at": utc_now(),
            "note": note[:200],
            # Which board author this card was granted to, when it was granted
            # for something they did. Lets auto-promotion rebuild what it has
            # already handed out from disk instead of from memory, so a
            # restart doesn't re-promote everybody.
            "subject": subject,
            "revoked": False,
            "redeemed_count": 0,
        }
        with self._lock:
            self._cards[card["code_hash"]] = card
            self._by_id[card["card_id"]] = card
            self._append(card)
        return code, card

    def plant(self, *, code: str, role: str, holder: str,
              issued_by: str = "system", note: str = "") -> dict:
        """Install a card with a KNOWN code, for the published demo cards.

        Separate from issue() and deliberately awkward to reach for: a code
        somebody chose is only safe when it is meant to be public. Everything
        else about the record is identical — the plaintext is still never
        written to disk, only its hash.
        """
        if role not in ROLES:
            raise ValueError(f"unknown role {role!r}")
        if not code_looks_valid(code):
            raise ValueError(f"{code!r} is not a well-formed card code")

        card = {
            "card_id": "CARD-" + hashlib.sha256(
                normalise(code).encode()).hexdigest()[:8].upper(),
            "code_hash": hash_code(code),
            "role": role,
            "holder": holder.strip()[:80],
            "issued_by": issued_by,
            "issued_at": utc_now(),
            "note": note[:200],
            "subject": None,
            "revoked": False,
            "redeemed_count": 0,
        }
        with self._lock:
            self._cards[card["code_hash"]] = card
            self._by_id[card["card_id"]] = card
            self._append(card)
        return card

    def revoke(self, card_id: str, *, by: str = "system") -> dict:
        with self._lock:
            card = self._by_id.get(card_id)
            if card is None:
                raise KeyError(f"no card {card_id!r}")
            card["revoked"] = True
            card["revoked_by"] = by
            card["revoked_at"] = utc_now()
            self._append(card)
            # Any session opened with this card dies immediately.
            for token, session in list(self._sessions.items()):
                if session["card_id"] == card_id:
                    self._sessions.pop(token, None)
            return card

    # -- redeeming ---------------------------------------------------------

    def redeem(self, code: str, *, client: str = "unknown") -> tuple[str, dict]:
        """Exchange a printed code for a session token.

        Throttled per client. 74 bits of entropy already makes guessing
        hopeless, but throttling costs nothing and turns a brute-force attempt
        into something visible rather than silent.
        """
        now = time.time()
        with self._lock:
            recent = [t for t in self._attempts.get(client, []) if now - t < 300]
            self._attempts[client] = recent
            if len(recent) >= 10:
                raise PermissionError(
                    "too many incorrect codes from this device — wait five minutes")

        if not code_looks_valid(code):
            self._record_failure(client, now)
            raise ValueError("that code doesn't look right — check it against the card")

        with self._lock:
            card = self._cards.get(hash_code(code))
            if card is None or card.get("revoked"):
                self._record_failure(client, now)
                raise ValueError("that card is not recognised, or has been cancelled")

            token = secrets.token_urlsafe(24)
            self._sessions[token] = {
                "card_id": card["card_id"],
                "expires": now + SESSION_TTL_SECONDS,
            }
            card["redeemed_count"] = card.get("redeemed_count", 0) + 1
            card["last_redeemed_at"] = utc_now()
            self._append(card)
            return token, card

    def _record_failure(self, client: str, now: float) -> None:
        with self._lock:
            self._attempts.setdefault(client, []).append(now)

    # -- sessions ----------------------------------------------------------

    def resolve(self, token: str | None) -> dict | None:
        """Token -> {role, holder, card_id, permissions}, or None."""
        if not token:
            return None
        with self._lock:
            session = self._sessions.get(token)
            if session is None:
                return None
            if session["expires"] < time.time():
                self._sessions.pop(token, None)
                return None
            card = self._by_id.get(session["card_id"])
            if card is None or card.get("revoked"):
                return None
            return {
                "card_id": card["card_id"],
                "role": card["role"],
                "holder": card["holder"],
                "permissions": sorted(permissions_for(card["role"])),
                "expires_in": int(session["expires"] - time.time()),
            }

    def sign_out(self, token: str | None) -> None:
        if token:
            with self._lock:
                self._sessions.pop(token, None)

    # -- listing (for the admin screen; never includes a code) -------------

    def cards(self) -> list[dict]:
        with self._lock:
            return [
                {k: v for k, v in card.items() if k != "code_hash"}
                for card in sorted(self._by_id.values(),
                                   key=lambda c: c.get("issued_at") or "")
            ]


# ---------------------------------------------------------------------------
# events into the signal log (never the code)
# ---------------------------------------------------------------------------

CARD_EVENT_TYPE = "card-event"


def card_event(store, *, action: str, card_id: str, role: str,
               holder: str, actor: str, module_id: str = "team-6-two-way",
               detail: dict | None = None) -> dict:
    """Record a card lifecycle event in the append-only log.

    Carries the card id, role and who did it — never the code. Delegation and
    promotion are exactly the kind of thing that has to be answerable after
    the event: who gave this person the ability to moderate, and when.
    """
    return store.publish(make_signal(
        module_id=module_id,
        title=f"Card {action}: {role}",
        signal_type=CARD_EVENT_TYPE,
        source_type="official",
        description=f"{action} — {role} card for {holder}, by {actor}",
        raw={"action": action, "card_id": card_id, "role": role,
             "holder": holder, "actor": actor, **(detail or {})},
    ))
