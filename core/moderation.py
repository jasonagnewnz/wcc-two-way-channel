"""Spam limits, content challenges, and earned trust.

Three jobs, all on the public board:

1. **Stop the board being flooded.** Rate limits per author, and a duplicate
   check, because the fastest way to make an emergency board useless is to
   bury it.
2. **Challenge thin messages.** "help" and "FLOODING!!!" are not reports. The
   board asks for enough to act on — and says specifically what is missing,
   rather than rejecting and leaving the person guessing.
3. **Notice who is actually useful, and give them moderation.** A heuristic,
   deliberately explainable: no model, no opaque score, just countable
   behaviour a duty officer could check by hand.

On the third: automation grants at most `AUTO_PROMOTE_MAX_ROLE`, which is
`moderator` — a role that carries no `card.issue` permission. So the worst
case of a bad heuristic is a badly chosen moderator, never a manufactured
official, and never a chain of them. Every promotion is written to the log
with the score that caused it, and can be revoked in one call.

None of this applies to card-holding officials. Someone who typed a code off a
printed card has already been trusted by a human.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone

from .identity import AUTO_PROMOTE_MAX_ROLE, role_rank

# ---------------------------------------------------------------------------
# rate limits
# ---------------------------------------------------------------------------

# (messages, seconds). Generous enough that no honest person in an emergency
# hits them, tight enough that a script is stopped in seconds.
LIMITS = {
    "resident":  [(5, 60), (40, 3600)],
    "verified":  [(12, 60), (120, 3600)],
    "moderator": [(20, 60), (300, 3600)],
    "hub-lead":  [(30, 60), (600, 3600)],
    "official":  [(60, 60), (1200, 3600)],
    "coordinator": [(60, 60), (1200, 3600)],
}

DUPLICATE_WINDOW_SECONDS = 600


class RateLimited(Exception):
    """Too many messages, too fast. Carries the wait in seconds."""

    def __init__(self, message: str, retry_after: int):
        super().__init__(message)
        self.retry_after = retry_after


class ContentChallenge(Exception):
    """The message is too thin to act on. Carries a specific ask."""


class RateLimiter:
    """In-memory sliding window, keyed by author.

    Per author rather than per IP on purpose: a Community Emergency Hub is one
    building where forty people share a connection, and rate-limiting them as
    one IP would silence a hub during the event it exists for.
    """

    def __init__(self):
        self._events: dict[str, list[float]] = {}
        self._recent_bodies: dict[str, list[tuple[float, str]]] = {}

    def check(self, author_id: str, role: str = "resident") -> None:
        now = time.time()
        limits = LIMITS.get(role, LIMITS["resident"])
        window = max(seconds for _, seconds in limits)

        events = [t for t in self._events.get(author_id, []) if now - t < window]
        self._events[author_id] = events

        for allowed, seconds in limits:
            in_window = [t for t in events if now - t < seconds]
            if len(in_window) >= allowed:
                retry = int(seconds - (now - min(in_window))) + 1
                unit = "minute" if seconds <= 60 else "hour"
                raise RateLimited(
                    f"That's {allowed} messages in a {unit}. "
                    f"Give it {retry} seconds — it keeps the board readable.",
                    retry_after=retry)

    def check_duplicate(self, author_id: str, body: str) -> None:
        now = time.time()
        key = _normalise_body(body)
        recent = [(t, b) for t, b in self._recent_bodies.get(author_id, [])
                  if now - t < DUPLICATE_WINDOW_SECONDS]
        self._recent_bodies[author_id] = recent
        if any(b == key for _, b in recent):
            raise ContentChallenge(
                "You've already posted that. If something has changed, say what "
                "changed — that's the useful part.")

    def record(self, author_id: str, body: str) -> None:
        now = time.time()
        self._events.setdefault(author_id, []).append(now)
        self._recent_bodies.setdefault(author_id, []).append((now, _normalise_body(body)))


def _normalise_body(body: str) -> str:
    return re.sub(r"\s+", " ", (body or "").strip().lower())


# ---------------------------------------------------------------------------
# content challenge
# ---------------------------------------------------------------------------

MIN_CHARS = 15
MIN_WORDS = 4

# Words that carry no information on their own. A message made only of these
# is someone in distress who needs prompting, not a spammer — so the response
# is a question, never a rejection.
_EMPTY = {"help", "please", "urgent", "hi", "hello", "hey", "asap", "now",
          "anyone", "someone", "there", "test", "testing", "yes", "no", "ok",
          "okay", "thanks", "thank", "you", "the", "a", "is", "it", "this"}

_URL = re.compile(r"https?://\S+", re.I)


def challenge(body: str, *, role: str = "resident") -> None:
    """Raise ContentChallenge if the message is too thin to be useful.

    Every message is checked, whatever the role. This is about whether a duty
    officer can act on it, not about whether the sender is trusted — a
    one-word message from an official is just as useless.
    """
    text = (body or "").strip()

    if len(text) < MIN_CHARS:
        raise ContentChallenge(
            "Tell us a bit more — what is happening, and roughly where? "
            "A sentence is plenty.")

    words = [w for w in re.findall(r"[a-z']+", text.lower()) if len(w) > 1]
    if len(words) < MIN_WORDS:
        raise ContentChallenge(
            "That's very short. What is happening, and where? "
            "Even a rough street name helps.")

    meaningful = [w for w in words if w not in _EMPTY]
    if len(meaningful) < 3:
        raise ContentChallenge(
            "We can see it's urgent. Say what you're seeing and where, so "
            "someone can act on it — for example 'water over the road on Hutt "
            "Road near the Ngauranga onramp'.")

    letters = [c for c in text if c.isalpha()]
    if len(letters) >= 20 and sum(1 for c in letters if c.isupper()) / len(letters) > 0.7:
        raise ContentChallenge(
            "Could you write that in normal case? Everything in capitals is "
            "hard to read quickly, which is the opposite of what you want here.")

    if role in ("resident",) and len(_URL.findall(text)) > 2:
        raise ContentChallenge(
            "That's a lot of links for one message. Say what you're seeing in "
            "your own words and keep one link if it helps.")


# ---------------------------------------------------------------------------
# trust scoring
# ---------------------------------------------------------------------------

# Deliberately small, countable, and explainable. Anyone can check it by hand.
#
# The maximum reachable score is 98, so 45 is roughly "two reports WCC acted
# on, plus a few substantive posts across more than one board". It is a policy
# dial, not a discovered constant — raise it and the bot promotes almost
# nobody, lower it and volume starts to be enough on its own.
THRESHOLD = 45

# Volume is the weakest input and deliberately the smallest: it is the one
# thing a spammer can manufacture. Corroboration — a report an official chose
# to act on — is worth five messages, because a human made that judgement.
WEIGHTS = {
    "messages": 2,          # per message, capped
    "messages_cap": 12,
    "substantive": 3,       # per message over 80 chars, capped
    "substantive_cap": 18,
    "channels": 6,          # per distinct channel, capped
    "channels_cap": 18,
    "reports_actioned": 15,  # a report an official moved past 'received'
    "reports_actioned_cap": 30,
    "age_hours": 2,         # per hour since first message, capped
    "age_cap": 20,
}


def _plural(count: int, singular: str, plural: str | None = None) -> str:
    """'1 message', '2 messages'. These strings are read off a screen during a
    demo and in an incident review; '1 channel(s)' looks like a placeholder
    nobody finished.
    """
    return f"{count} {singular if count == 1 else (plural or singular + 's')}"


def _parse(value: str | None):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def score_author(store, author_id: str, module_id: str = "team-6-two-way") -> dict:
    """Score one author's contribution history. Returns the breakdown.

    Returns the *reasons*, not just a number, because a permission granted by
    a heuristic has to be explainable to the person who has to defend it.
    """
    from .chat import FLAG_TYPE, MESSAGE_TYPE
    from .reports import REPORT_TYPE, STATUS_TYPE

    messages = [s for s in store.fetch(limit=0, signal_type=MESSAGE_TYPE, module_id=module_id)
                if (s.get("raw") or {}).get("author_id") == author_id]

    if not messages:
        return {"author_id": author_id, "score": 0, "eligible": False,
                "reasons": [], "blocked_by": "no messages"}

    # Any flag against them is disqualifying, full stop. A moderator has to be
    # someone whose own posts have never needed moderating — and it makes the
    # heuristic hard to farm, since volume cannot outrun a single flag.
    ids = {s["id"] for s in messages}
    flagged = [
        s for s in store.fetch(limit=0, signal_type=FLAG_TYPE, module_id=module_id)
        if (s.get("raw") or {}).get("message_id") in ids
        and (s.get("raw") or {}).get("action") != "unflag"
    ]
    if flagged:
        blocked = (f"{_plural(len(flagged), 'message')} of theirs "
                   f"{'was' if len(flagged) == 1 else 'were'} flagged")
        return {"author_id": author_id, "score": 0, "eligible": False,
                "reasons": [], "blocked_by": blocked}

    reasons = []
    score = 0

    points = min(len(messages) * WEIGHTS["messages"], WEIGHTS["messages_cap"])
    score += points
    reasons.append({"what": f"{_plural(len(messages), 'message')} posted", "points": points})

    substantive = [m for m in messages if len(m.get("description", "")) > 80]
    points = min(len(substantive) * WEIGHTS["substantive"], WEIGHTS["substantive_cap"])
    score += points
    reasons.append({"what": f"{len(substantive)} of them substantive (80+ characters)"
                            if len(substantive) != 1 else "1 of them substantive (80+ characters)",
                    "points": points})

    channels = {(m.get("raw") or {}).get("channel_id") for m in messages}
    channels.discard(None)
    points = min(len(channels) * WEIGHTS["channels"], WEIGHTS["channels_cap"])
    score += points
    reasons.append({"what": f"active in {_plural(len(channels), 'channel')}", "points": points})

    # The strongest signal available: they filed a report an official thought
    # worth acting on. That is corroboration by a human, not by volume.
    their_reports = {
        s["id"] for s in store.fetch(limit=0, signal_type=REPORT_TYPE, module_id=module_id)
        if (s.get("raw") or {}).get("author_id") == author_id
    }
    actioned = {
        (s.get("raw") or {}).get("original_signal_id")
        for s in store.fetch(limit=0, signal_type=STATUS_TYPE, module_id=module_id)
        if (s.get("raw") or {}).get("status") in ("reviewing", "responding", "resolved")
        and (s.get("raw") or {}).get("original_signal_id") in their_reports
    }
    points = min(len(actioned) * WEIGHTS["reports_actioned"], WEIGHTS["reports_actioned_cap"])
    score += points
    reasons.append({"what": f"{_plural(len(actioned), 'report')} of theirs "
                            f"{'was' if len(actioned) == 1 else 'were'} acted on by WCC",
                    "points": points})

    first = _parse(messages[0].get("created_at"))
    hours = int((datetime.now(timezone.utc) - first).total_seconds() // 3600) if first else 0
    points = min(hours * WEIGHTS["age_hours"], WEIGHTS["age_cap"])
    score += points
    reasons.append({"what": f"active for {_plural(hours, 'hour')}", "points": points})

    return {
        "author_id": author_id,
        "display_name": (messages[-1].get("raw") or {}).get("author_name"),
        "score": score,
        "threshold": THRESHOLD,
        "eligible": score >= THRESHOLD,
        "reasons": reasons,
        "blocked_by": None,
    }


def candidates(store, module_id: str = "team-6-two-way") -> list[dict]:
    """Score every author who has posted, best first."""
    from .chat import MESSAGE_TYPE

    seen = []
    for signal in store.fetch(limit=0, signal_type=MESSAGE_TYPE, module_id=module_id):
        raw = signal.get("raw") or {}
        author = raw.get("author_id")
        # Officials already hold a card; there is nothing to earn.
        if author and author not in seen and raw.get("author_role") != "official":
            seen.append(author)

    scored = [score_author(store, author, module_id) for author in seen]
    return sorted(scored, key=lambda s: s["score"], reverse=True)


def auto_promote(store, cards, *, module_id: str = "team-6-two-way",
                 granted: dict[str, str] | None = None) -> list[dict]:
    """Promote every eligible author to AUTO_PROMOTE_MAX_ROLE.

    `granted` maps author_id -> role for people already promoted, so this is
    idempotent and can run on a timer.

    The ceiling is enforced here as well as documented: even if the constant
    were edited to something dangerous, the assert below refuses to let
    automation mint a role that can issue cards.
    """
    from .identity import ROLES, card_event

    ceiling = ROLES[AUTO_PROMOTE_MAX_ROLE]
    assert "card.issue" not in ceiling["permissions"], (
        "automation must never grant a role that can mint further cards")

    # `granted or {}` would have replaced a caller's EMPTY dict with a
    # throwaway — an empty dict is falsy — so the first run's bookkeeping went
    # nowhere and every subsequent run promoted the same people again.
    if granted is None:
        granted = {}

    # Rebuild from disk as well as from the caller. In-memory state does not
    # survive a restart, and re-promoting somebody on every reboot would fill
    # the log with duplicate grants.
    for card in cards.cards():
        if card.get("issued_by") == "trust-bot" and card.get("subject") \
                and not card.get("revoked"):
            granted.setdefault(card["subject"], card["role"])

    promoted = []

    for candidate in candidates(store, module_id):
        author = candidate["author_id"]
        if not candidate["eligible"]:
            continue
        if role_rank(granted.get(author, "resident")) >= role_rank(AUTO_PROMOTE_MAX_ROLE):
            continue

        code, card = cards.issue(
            role=AUTO_PROMOTE_MAX_ROLE,
            holder=candidate.get("display_name") or author,
            issued_by="trust-bot",
            note=f"Auto-granted at score {candidate['score']}/{THRESHOLD}.",
            subject=author,
        )
        card_event(store, action="auto-promoted", card_id=card["card_id"],
                   role=AUTO_PROMOTE_MAX_ROLE,
                   holder=card["holder"], actor="trust-bot",
                   module_id=module_id,
                   detail={"author_id": author, "score": candidate["score"],
                           "reasons": candidate["reasons"]})
        granted[author] = AUTO_PROMOTE_MAX_ROLE
        promoted.append({**candidate, "card_id": card["card_id"], "code": code})

    return promoted
