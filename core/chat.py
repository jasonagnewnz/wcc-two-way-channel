"""Message board — agency coordination, public boards, and per-person threads.

Three surfaces, one log:

  agency channels   officials coordinating with each other. The public are not
                    in these and cannot see them.
  public boards     a city-wide board plus a board per suburb. Anyone posts.
  my thread         one resident's own conversation with officials, hung off
                    the reference code from their report.

Plus an **important comms banner**: officials publish one line that appears at
the top of every public surface at once.

Everything here is a signal on the same append-only log as reports. A chat
message is a `chat-message`; flagging one publishes a `chat-flag` that chains
to it; the banner is a `comms-banner`. Nothing is edited and nothing is
deleted, which is the property that matters most on this surface: an official
moderating a public emergency board can never silently erase what somebody
said. The message leaves the public feed and a visible marker stays behind.

Identity
--------
There is none, deliberately, and that is consistent with the reference-code
model in reference/report-status-design.md. A browser mints a random
`author_id` and keeps it in localStorage. It proves nothing; it just lets the
board show you your own private messages after a reload. Officials are marked
by an `author_role` the client sets, so on a public deployment anyone can
claim to be one — see the honest-limitations note in the README.
"""

from __future__ import annotations

from .signals import idempotency_key, make_signal
from .store import SignalStore

MESSAGE_TYPE = "chat-message"
FLAG_TYPE = "chat-flag"
BANNER_TYPE = "comms-banner"

PUBLIC = "public"
OFFICIALS = "officials"
VISIBILITIES = (PUBLIC, OFFICIALS)

BANNER_LEVELS = ("info", "advisory", "warning", "critical")

# Real agencies, named so a WCC judge sees their own operating picture. Every
# message in these channels is invented for the demo and the interface says so
# in the channel header — nothing here should ever read as a genuine
# operational statement from any of these bodies.
AGENCY_CHANNELS = [
    {"id": "wcc-em", "name": "WCC Emergency Management", "short": "WCC EM", "colour": "#0b5cd5"},
    {"id": "wremo", "name": "WREMO", "short": "WREMO", "colour": "#7a4bd0"},
    {"id": "wellington-water", "name": "Wellington Water", "short": "Water", "colour": "#0f8fa8"},
    {"id": "fenz", "name": "Fire and Emergency NZ", "short": "FENZ", "colour": "#d64545"},
    {"id": "nz-police", "name": "NZ Police", "short": "Police", "colour": "#26497d"},
    {"id": "gwrc", "name": "Greater Wellington", "short": "GWRC", "colour": "#17794a"},
    {"id": "wfa", "name": "Wellington Free Ambulance", "short": "Ambulance", "colour": "#b8860b"},
    {"id": "red-cross", "name": "NZ Red Cross", "short": "Red Cross", "colour": "#c2185b"},
]

PUBLIC_CHANNELS = [
    {"id": "wellington", "name": "Wellington — everyone", "short": "City-wide"},
    {"id": "ngauranga", "name": "Ngauranga", "short": "Ngauranga"},
    {"id": "wadestown", "name": "Wadestown", "short": "Wadestown"},
    {"id": "aro-valley", "name": "Aro Valley", "short": "Aro Valley"},
    {"id": "island-bay", "name": "Island Bay", "short": "Island Bay"},
    {"id": "newtown", "name": "Newtown", "short": "Newtown"},
    {"id": "karori", "name": "Karori", "short": "Karori"},
    {"id": "miramar", "name": "Miramar", "short": "Miramar"},
]

_AGENCY_IDS = {c["id"] for c in AGENCY_CHANNELS}
_PUBLIC_IDS = {c["id"] for c in PUBLIC_CHANNELS}

MAX_BODY = 2000


def channel_kind(channel_id: str) -> str:
    """agency | public | thread. A thread id is a report reference code."""
    if channel_id in _AGENCY_IDS:
        return "agency"
    if channel_id in _PUBLIC_IDS:
        return "public"
    return "thread"


def redact_for_public(signals: list[dict]) -> list[dict]:
    """Strip anything a public caller must not see from a raw signal list.

    `/api/signals` exposes the append-only log so other teams' modules can read
    this one. That endpoint bypasses every service's read filter, so without
    this it hands out exactly what the rest of the app is careful to withhold.

    Written as GENERAL RULES rather than a list of known types, because the
    per-type version was wrong within a day: it knew about chat messages, and
    then help requests arrived carrying `visibility: officials` and leaked
    straight through it. Anything marked officials-only is withheld here
    whatever type it is, so a new private type is safe by default instead of
    safe only if somebody remembers this function.
    """
    out = []
    for signal in signals:
        raw = signal.get("raw") or {}

        # Rule 1: anything its author marked officials-only. Type-agnostic.
        if raw.get("visibility") == OFFICIALS:
            continue

        # Rule 2: agency channels are not public at all.
        if raw.get("channel_kind") == "agency":
            continue

        # Rule 3: moderation and access decisions are official business —
        # they name a moderator, carry their reasoning, or describe a card.
        if signal.get("signal_type") in (FLAG_TYPE, "moderation-decision",
                                         "card-event"):
            continue

        # Rule 4: community content that has not been approved yet must not
        # reach the public through the back door either.
        if raw.get("state") in ("pending", "rejected"):
            continue

        # Rule 5: never publish a contact detail, whatever else is public
        # about the item. Someone offering water publicly did not thereby
        # publish their phone number.
        if "contact" in raw:
            signal = dict(signal)
            signal["raw"] = {k: v for k, v in raw.items() if k != "contact"}

        out.append(signal)
    return out


class ChatService:
    def __init__(self, store: SignalStore, module_id: str = "team-6-two-way"):
        self.store = store
        self.module_id = module_id

    # -- writing -----------------------------------------------------------

    def post(self, *, channel_id: str, body: str, author_name: str,
             author_id: str, author_role: str = "resident",
             agency: str | None = None, visibility: str = PUBLIC,
             reply_to: str | None = None) -> dict:
        """Post a message. Returns the stored signal.

        The public cannot post into an agency channel. That is the one hard
        rule on this surface: agency channels are for agencies talking to each
        other, and a resident appearing in one would misrepresent who said
        what during an emergency.
        """
        body = (body or "").strip()
        if not body:
            raise ValueError("a message needs some text")
        if visibility not in VISIBILITIES:
            raise ValueError(f"visibility must be one of {VISIBILITIES}")

        kind = channel_kind(channel_id)
        if kind == "agency" and author_role != "official":
            raise PermissionError(
                "agency channels are for officials coordinating with each "
                "other; the public board is at /chat/wellington")

        body = body[:MAX_BODY]
        return self.store.publish(make_signal(
            module_id=self.module_id,
            title=body[:200],
            signal_type=MESSAGE_TYPE,
            source_type="official" if author_role == "official" else "community",
            description=body,
            source=f"chat/{channel_id}",
            raw={
                "channel_id": channel_id,
                "channel_kind": kind,
                "author_name": author_name[:80] or "Anonymous",
                "author_id": author_id,
                "author_role": author_role,
                "agency": agency,
                "visibility": visibility,
                "reply_to": reply_to,
            },
        ))

    def flag(self, message_id: str, *, reason: str = "", actor: str = "wcc-staff",
             unflag: bool = False) -> dict:
        """Flag (or clear a flag on) a message.

        Publishes a new signal rather than touching the message. The message
        leaves the public feed, but a marker stays where it was: moderation on
        an emergency board has to be visible, or the board is not trustworthy.
        """
        target = self.store.get(message_id)
        if target is None or target.get("signal_type") != MESSAGE_TYPE:
            raise KeyError(f"no message with id {message_id!r}")

        action = "unflag" if unflag else "flag"
        return self.store.publish(make_signal(
            module_id=self.module_id,
            title=f"Message {action}ged",
            signal_type=FLAG_TYPE,
            source_type="official",
            description=reason[:500],
            raw={"message_id": message_id, "action": action,
                 "reason": reason[:500], "actor": actor},
            idempotency_key_=idempotency_key(FLAG_TYPE, message_id, action, reason),
        ))

    def set_banner(self, *, text: str, level: str = "warning",
                   actor: str = "wcc-staff", active: bool = True) -> dict:
        """Publish (or clear) the important-comms banner.

        Clearing is `active=False` — another entry in the log, not a deletion,
        so afterwards you can say exactly what was displayed and for how long.
        """
        if level not in BANNER_LEVELS:
            raise ValueError(f"level must be one of {BANNER_LEVELS}")
        text = (text or "").strip()[:500]
        if active and not text:
            raise ValueError("a banner needs some text")

        return self.store.publish(make_signal(
            module_id=self.module_id,
            title=(text or "Banner cleared")[:200],
            signal_type=BANNER_TYPE,
            source_type="official",
            description=text,
            severity="severe" if level == "critical" else "unknown",
            raw={"level": level, "text": text, "active": active, "actor": actor},
        ))

    # -- reading -----------------------------------------------------------

    def _flag_state(self) -> dict[str, dict]:
        """Latest flag state per message, by replaying the flag signals."""
        state: dict[str, dict] = {}
        for signal in self.store.fetch(limit=0, signal_type=FLAG_TYPE,
                                       module_id=self.module_id):
            raw = signal.get("raw") or {}
            mid = raw.get("message_id")
            if not mid:
                continue
            if raw.get("action") == "unflag":
                state.pop(mid, None)
            else:
                state[mid] = {"reason": raw.get("reason", ""),
                              "actor": raw.get("actor", ""),
                              "at": signal.get("created_at")}
        return state

    def banner(self) -> dict | None:
        """The banner currently showing, or None."""
        rows = self.store.fetch(limit=0, signal_type=BANNER_TYPE,
                                module_id=self.module_id)
        if not rows:
            return None
        raw = rows[-1].get("raw") or {}
        if not raw.get("active"):
            return None
        return {"level": raw.get("level", "warning"), "text": raw.get("text", ""),
                "actor": raw.get("actor", ""), "at": rows[-1].get("created_at")}

    def messages(self, channel_id: str, *, viewer: str = "public",
                 author_id: str | None = None, limit: int = 200) -> list[dict]:
        """Messages in one channel, filtered for who is asking.

        A public viewer never sees an agency channel, never sees somebody
        else's private message, and sees a flagged message only as a marker.
        An official sees everything, flags included, because that is the point
        of being the moderator.
        """
        official = viewer == "official"
        if channel_kind(channel_id) == "agency" and not official:
            raise PermissionError("agency channels are not public")

        flags = self._flag_state()
        out = []
        for signal in self.store.fetch(limit=0, signal_type=MESSAGE_TYPE,
                                       module_id=self.module_id):
            raw = signal.get("raw") or {}
            if raw.get("channel_id") != channel_id:
                continue

            mine = author_id is not None and raw.get("author_id") == author_id
            private = raw.get("visibility") == OFFICIALS
            if private and not (official or mine):
                continue

            flag = flags.get(signal["id"])
            if flag and not official:
                # The tombstone. Timestamp and role survive so the gap in the
                # conversation is honest; the content does not.
                out.append({
                    "id": signal["id"],
                    "flagged": True,
                    "withheld": True,
                    "author_role": raw.get("author_role"),
                    "at": signal.get("created_at"),
                    "body": "This message was flagged by an official and is under review.",
                })
                continue

            out.append({
                "id": signal["id"],
                "body": signal.get("description", ""),
                "author_name": raw.get("author_name"),
                "author_role": raw.get("author_role"),
                "author_id": raw.get("author_id") if official or mine else None,
                "agency": raw.get("agency"),
                "visibility": raw.get("visibility"),
                "mine": mine,
                "reply_to": raw.get("reply_to"),
                "at": signal.get("created_at"),
                "flagged": bool(flag),
                "withheld": False,
                "flag_reason": (flag or {}).get("reason") if official else None,
            })

        return out[-limit:]

    def channels(self, *, viewer: str = "public") -> dict:
        """Channel lists with unread-ish counts, shaped for the UI."""
        official = viewer == "official"
        counts: dict[str, int] = {}
        latest: dict[str, str] = {}
        for signal in self.store.fetch(limit=0, signal_type=MESSAGE_TYPE,
                                       module_id=self.module_id):
            raw = signal.get("raw") or {}
            cid = raw.get("channel_id")
            if not cid:
                continue
            counts[cid] = counts.get(cid, 0) + 1
            latest[cid] = signal.get("created_at") or ""

        def decorate(channel: dict, kind: str) -> dict:
            return {**channel, "kind": kind,
                    "messages": counts.get(channel["id"], 0),
                    "last_at": latest.get(channel["id"])}

        result = {
            "public": [decorate(c, "public") for c in PUBLIC_CHANNELS],
            "agency": [decorate(c, "agency") for c in AGENCY_CHANNELS] if official else [],
        }
        return result
