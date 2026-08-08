"""The community map: what people can offer, what they can show, what is live.

Three things a resident can put on the map, all moderated before the public
sees them, all on the same append-only log:

  resource   something you have — first aid, water, food, shelter, a generator,
             a vehicle. Attached to you, so it is also your profile.
  evidence   a photo. If the photo carries GPS the pin places itself.
  feed       a link to something live — a webcam, a stream, a status page.

Why everything is pending first
-------------------------------
These are the surfaces where a stranger's content appears on a map that looks
like it comes from the council. A wrong address for "water here" sends people
to the wrong street during an emergency. So nothing from an unverified
resident is public until somebody with `moderate.flag` says so — and, because
the log is append-only, the decision is a signal chaining to the item rather
than an edit, so afterwards you can say who approved what and when.

Card holders skip the queue. Someone who typed a code off a printed card has
already been trusted by a human.

Evidence stacks, it does not dedupe
-----------------------------------
Five photos of the same flooded road is not four duplicates and one report. It
is five independent witnesses, and the count is the most useful thing on the
screen. Everything within `STACK_RADIUS_M` is gathered into one stack and the
strength is shown, rather than collapsed into a single pin as a mapping
library would.
"""

from __future__ import annotations

from .reports import GROUP_RADIUS_M, REPORT_TYPE, haversine_m
from .signals import make_signal, safe_link

RESOURCE_TYPE = "community-resource"
EVIDENCE_TYPE = "evidence-photo"
FEED_TYPE = "live-feed"
DECISION_TYPE = "moderation-decision"

PENDING = "pending"
APPROVED = "approved"
REJECTED = "rejected"

# What a household or hub can realistically offer. Kept short: a list of forty
# options is a list nobody reads during an emergency.
RESOURCE_KINDS = {
    "first-aid": "First aid",
    "water": "Drinking water",
    "food": "Food",
    "shelter": "Shelter / somewhere warm",
    "power": "Power or charging",
    "toilet": "Toilet or washing",
    "transport": "Transport / 4WD",
    "tools": "Tools or equipment",
    "medical": "Medical training",
    "comms": "Radio or satellite",
    "other": "Something else",
}

FEED_KINDS = {
    "camera": "Live camera",
    "stream": "Video or audio stream",
    "page": "Status page",
    "sensor": "Sensor readings",
}

# Anything this close is the same thing being witnessed more than once.
# Matches the report grouping radius so a photo and a report of one incident
# land in the same stack.
STACK_RADIUS_M = GROUP_RADIUS_M

MODERATED_TYPES = (RESOURCE_TYPE, EVIDENCE_TYPE, FEED_TYPE)


class CommunityService:
    def __init__(self, store, module_id: str = "team-6-two-way"):
        self.store = store
        self.module_id = module_id

    # -- submitting --------------------------------------------------------

    def _initial_state(self, trusted: bool) -> str:
        return APPROVED if trusted else PENDING

    def offer_resource(self, *, kind: str, detail: str, author_id: str,
                       author_name: str, lat: float | None = None,
                       lng: float | None = None, place_name: str | None = None,
                       contact: str | None = None, visibility: str = "public",
                       trusted: bool = False) -> dict:
        """Someone offers something. This is also how a profile gets its tags."""
        if kind not in RESOURCE_KINDS:
            kind = "other"
        detail = (detail or "").strip()[:500]

        return self.store.publish(make_signal(
            module_id=self.module_id,
            title=f"{RESOURCE_KINDS[kind]}" + (f" — {place_name}" if place_name else ""),
            signal_type=RESOURCE_TYPE,
            source_type="community",
            description=detail,
            lat=lat, lng=lng, place_name=place_name,
            raw={
                "kind": kind,
                "author_id": author_id,
                "author_name": author_name,
                # An address and a contact number are personal data about a
                # real household. "officials" keeps them out of the public
                # feed entirely, including out of /api/signals.
                "contact": contact,
                "visibility": visibility,
                "state": self._initial_state(trusted),
            },
        ))

    def add_evidence(self, *, image_url: str, caption: str, author_id: str,
                     author_name: str, lat: float | None = None,
                     lng: float | None = None, place_name: str | None = None,
                     located_by: str = "manual", taken_at: str | None = None,
                     trusted: bool = False) -> dict:
        return self.store.publish(make_signal(
            module_id=self.module_id,
            title=(caption or "Photo")[:200],
            signal_type=EVIDENCE_TYPE,
            source_type="community",
            description=(caption or "").strip()[:500],
            lat=lat, lng=lng, place_name=place_name,
            media_urls=[image_url],
            observed_at=taken_at,
            raw={
                "author_id": author_id,
                "author_name": author_name,
                # "photo metadata" or "manual" — shown on the pin, because a
                # location the camera recorded and a location someone tapped
                # are different kinds of claim.
                "located_by": located_by,
                "state": self._initial_state(trusted),
            },
        ))

    def add_feed(self, *, url: str, kind: str, label: str, author_id: str,
                 author_name: str, lat: float | None = None,
                 lng: float | None = None, trusted: bool = False) -> dict:
        if kind not in FEED_KINDS:
            kind = "page"
        url = safe_link(url)
        if not url:
            raise ValueError("a feed needs a link")
        return self.store.publish(make_signal(
            module_id=self.module_id,
            title=(label or FEED_KINDS[kind])[:200],
            signal_type=FEED_TYPE,
            source_type="community",
            description=label[:500],
            link=url,
            lat=lat, lng=lng,
            raw={
                "kind": kind,
                "url": url,
                "author_id": author_id,
                "author_name": author_name,
                "state": self._initial_state(trusted),
            },
        ))

    # -- moderating --------------------------------------------------------

    def decide(self, item_id: str, *, state: str, actor: str,
               reason: str = "") -> dict:
        """Approve or reject. Chains to the item; never edits it."""
        if state not in (APPROVED, REJECTED, PENDING):
            raise ValueError(f"state must be one of {(APPROVED, REJECTED, PENDING)}")
        item = self.store.get(item_id)
        if item is None or item.get("signal_type") not in MODERATED_TYPES:
            raise KeyError(f"nothing moderatable with id {item_id!r}")

        return self.store.publish(make_signal(
            module_id=self.module_id,
            title=f"Moderation: {state}",
            signal_type=DECISION_TYPE,
            source_type="official",
            description=reason[:500],
            raw={"item_id": item_id, "state": state, "actor": actor,
                 "reason": reason[:500]},
        ))

    def _states(self) -> dict[str, dict]:
        """Latest moderation decision per item, by replaying the log."""
        out: dict[str, dict] = {}
        for signal in self.store.fetch(limit=0, signal_type=DECISION_TYPE,
                                       module_id=self.module_id):
            raw = signal.get("raw") or {}
            if raw.get("item_id"):
                out[raw["item_id"]] = {
                    "state": raw.get("state"), "actor": raw.get("actor"),
                    "reason": raw.get("reason", ""), "at": signal.get("created_at"),
                }
        return out

    # -- reading -----------------------------------------------------------

    def items(self, signal_type: str, *, viewer: str = "public",
              author_id: str | None = None) -> list[dict]:
        official = viewer == "official"
        decisions = self._states()
        out = []

        for signal in self.store.fetch(limit=0, signal_type=signal_type,
                                       module_id=self.module_id):
            raw = signal.get("raw") or {}
            decision = decisions.get(signal["id"])
            state = decision["state"] if decision else raw.get("state", PENDING)
            mine = author_id is not None and raw.get("author_id") == author_id

            # Pending and rejected items are visible to their author and to
            # moderators. Nobody else sees them at all: an unreviewed pin on a
            # council-looking map is the thing this queue exists to prevent.
            if state != APPROVED and not (official or mine):
                continue

            private = raw.get("visibility") == "officials"
            if private and not (official or mine):
                continue

            item = {
                "id": signal["id"],
                "type": signal_type,
                "title": signal.get("title"),
                "detail": signal.get("description", ""),
                "lat": signal.get("lat"), "lng": signal.get("lng"),
                "place_name": signal.get("place_name"),
                "kind": raw.get("kind"),
                "author_name": raw.get("author_name"),
                "state": state,
                "mine": mine,
                "at": signal.get("created_at"),
                "located_by": raw.get("located_by"),
                "url": raw.get("url"),
                "media_urls": signal.get("media_urls") or [],
                "visibility": raw.get("visibility", "public"),
            }
            # Contact details and the exact address go to moderators and to
            # the person who offered them. Never to the public feed.
            if official or mine:
                item["contact"] = raw.get("contact")
                if decision:
                    item["decided_by"] = decision["actor"]
                    item["decision_reason"] = decision["reason"]
            out.append(item)
        return out

    def queue(self) -> list[dict]:
        """Everything awaiting a moderator, oldest first."""
        pending = []
        for signal_type in MODERATED_TYPES:
            pending += [i for i in self.items(signal_type, viewer="official")
                        if i["state"] == PENDING]
        return sorted(pending, key=lambda i: i.get("at") or "")

    # -- stacking ----------------------------------------------------------

    def stacks(self, *, viewer: str = "public") -> list[dict]:
        """Gather reports and approved evidence into corroboration stacks.

        Not deduplication. Five photos of one flooded road are five witnesses,
        and the count is the most useful number on the screen — so the stack
        reports its strength and keeps every contributor.
        """
        pieces = []
        for report in self.store.fetch(limit=0, signal_type=REPORT_TYPE,
                                       module_id=self.module_id):
            if report.get("lat") is None:
                continue
            pieces.append({
                "id": report["id"], "kind": "report",
                "lat": report["lat"], "lng": report["lng"],
                "title": report.get("title"), "at": report.get("created_at"),
                "author_name": (report.get("raw") or {}).get("reporter_kind", "resident"),
                "media_urls": report.get("media_urls") or [],
            })
        for photo in self.items(EVIDENCE_TYPE, viewer=viewer):
            if photo["lat"] is None or photo["state"] != APPROVED:
                continue
            pieces.append({
                "id": photo["id"], "kind": "photo",
                "lat": photo["lat"], "lng": photo["lng"],
                "title": photo["title"], "at": photo["at"],
                "author_name": photo["author_name"],
                "media_urls": photo["media_urls"],
                "located_by": photo["located_by"],
            })

        # Single-link clustering, same shape as report grouping.
        parent = {p["id"]: p["id"] for p in pieces}

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for i, a in enumerate(pieces):
            for b in pieces[i + 1:]:
                if haversine_m(a["lat"], a["lng"], b["lat"], b["lng"]) <= STACK_RADIUS_M:
                    ra, rb = find(a["id"]), find(b["id"])
                    if ra != rb:
                        parent[rb] = ra

        grouped: dict[str, list] = {}
        for piece in pieces:
            grouped.setdefault(find(piece["id"]), []).append(piece)

        stacks = []
        for key, members in grouped.items():
            photos = [m for m in members if m["kind"] == "photo"]
            reports = [m for m in members if m["kind"] == "report"]
            witnesses = {m["author_name"] for m in members if m.get("author_name")}
            stacks.append({
                "id": key,
                "lat": sum(m["lat"] for m in members) / len(members),
                "lng": sum(m["lng"] for m in members) / len(members),
                "title": members[0]["title"],
                "pieces": len(members),
                "photos": len(photos),
                "reports": len(reports),
                "witnesses": len(witnesses),
                "images": [u for m in photos for u in m["media_urls"]][:6],
                "members": sorted(members, key=lambda m: m.get("at") or ""),
            })
        return sorted(stacks, key=lambda s: s["pieces"], reverse=True)
