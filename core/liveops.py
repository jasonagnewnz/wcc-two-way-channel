"""Asking for help, publishing an issue, and being honest about the response.

Two things the loop was missing.

**A request for help is not a report.** "There is water over the road" and "I
need help getting my father out" are different messages with different
urgency, and flattening them into one queue buries the second. So a request is
its own type, with who needs help, how many people, and what would stop
someone reaching them.

**An honest answer is not the same as a fast one.** The hardest thing a
council does during an event is tell somebody "not tonight". A status of
"received" forever is what makes people ring the call centre. So an official
answers two separate questions:

    likelihood   are we coming
    timeframe    roughly when

Kept separate on purpose. "Likely, but not before tomorrow" and "confirmed,
within the hour" are both useful; a single blended field can express neither.
`unable` is a first-class answer with a reason attached, because during a real
event it is sometimes the true one, and saying it early lets a neighbour step
in instead.

Published issues are the other direction: WCC confirming something itself, so
the map shows what the council knows as well as what the public reported.
"""

from __future__ import annotations

from .signals import make_signal

NEWS_TYPE = "news-update"
REQUEST_TYPE = "help-request"
ISSUE_TYPE = "published-issue"
UPDATE_TYPE = "response-update"

NEED_KINDS = {
    "evacuation": "Help getting out",
    "welfare": "Someone needs checking on",
    "medical": "Medical help",
    "water": "Drinking water",
    "food": "Food",
    "shelter": "Somewhere to stay",
    "power": "Power or heating",
    "access": "Blocked in / can't get out",
    "other": "Something else",
}

URGENCY = {
    "now": "Right now",
    "today": "Today",
    "soon": "In the next day or two",
    "planning": "Not urgent, planning ahead",
}

# Are we coming?
LIKELIHOOD = {
    "confirmed": "Confirmed — someone is coming",
    "likely": "Likely",
    "unlikely": "Unlikely",
    "unable": "We can't get to this",
}

# Roughly when?
TIMEFRAME = {
    "within-hour": "Within the hour",
    "today": "Today",
    "24-hours": "Within 24 hours",
    "no-eta": "No time yet",
}

ISSUE_STATES = {
    "active": "Active",
    "monitoring": "Being monitored",
    "resolved": "Resolved",
}


# What a council actually needs to broadcast during an event. Built by asking
# "what would WCC have to tell people about?" rather than by generalising from
# what the prototype already had — the gaps were evacuation notices, welfare
# centre status, public health advisories and the whole recovery phase, none of
# which a report-and-respond loop covers.
#
# EVACUATION is first deliberately. It is the highest-stakes thing a council
# ever says, and it should never be one of several equal-looking categories in
# a list.
NEWS_CATEGORIES = {
    "evacuation": "Evacuation",
    "road": "Roads & transport",
    "water": "Water & wastewater",
    "power": "Power & utilities",
    "health": "Public health",
    "welfare": "Welfare centres & hubs",
    "service": "Council services",
    "weather": "Weather",
    "recovery": "Recovery & assistance",
    "general": "General update",
}

# Who can be the named source of an update. Same real agencies as the comms
# hub, so a reader sees one consistent set of names across the whole product.
NEWS_AGENCIES = {
    "wcc-em": "WCC Emergency Management",
    "wremo": "WREMO",
    "wellington-water": "Wellington Water",
    "fenz": "Fire and Emergency NZ",
    "nz-police": "NZ Police",
    "gwrc": "Greater Wellington",
    "wfa": "Wellington Free Ambulance",
    "red-cross": "NZ Red Cross",
    "metlink": "Metlink",
    "wellington-electricity": "Wellington Electricity",
    "health-nz": "Health New Zealand",
}

# Categories where being wrong or late is dangerous rather than annoying.
URGENT_CATEGORIES = ("evacuation", "health", "water")


class LiveOpsService:
    def __init__(self, store, module_id: str = "team-6-two-way"):
        self.store = store
        self.module_id = module_id

    # -- community asks ----------------------------------------------------

    def request_help(self, *, need: str, detail: str, author_id: str,
                     author_name: str, urgency: str = "today",
                     people: int | None = None, lat: float | None = None,
                     lng: float | None = None, place_name: str | None = None,
                     contact: str | None = None,
                     visibility: str = "officials") -> dict:
        """Somebody asks for help.

        Defaults to `officials` visibility, unlike everything else here. A
        request for help says a named person at a known address is vulnerable
        right now, and that is not something to put on a public map by
        accident. The asker can choose to make it public — a neighbour two
        streets away is often the fastest responder — but they have to choose
        it.
        """
        if need not in NEED_KINDS:
            need = "other"
        if urgency not in URGENCY:
            urgency = "today"

        return self.store.publish(make_signal(
            module_id=self.module_id,
            title=f"{NEED_KINDS[need]}" + (f" — {place_name}" if place_name else ""),
            signal_type=REQUEST_TYPE,
            source_type="community",
            description=(detail or "").strip()[:1000],
            severity="severe" if urgency == "now" else "moderate",
            lat=lat, lng=lng, place_name=place_name,
            raw={
                "need": need,
                "urgency": urgency,
                "people": people,
                "author_id": author_id,
                "author_name": author_name,
                "contact": contact,
                "visibility": "public" if visibility == "public" else "officials",
            },
        ))

    # -- WCC publishes -----------------------------------------------------

    def publish_issue(self, *, title: str, detail: str, actor: str,
                      state: str = "active", lat: float | None = None,
                      lng: float | None = None, place_name: str | None = None,
                      severity: str = "moderate") -> dict:
        """WCC confirming something itself, so the map shows what the council
        knows as well as what the public reported.
        """
        if state not in ISSUE_STATES:
            state = "active"
        return self.store.publish(make_signal(
            module_id=self.module_id,
            title=(title or "").strip()[:200],
            signal_type=ISSUE_TYPE,
            source_type="official",
            description=(detail or "").strip()[:1000],
            severity=severity,
            lat=lat, lng=lng, place_name=place_name,
            raw={"state": state, "actor": actor},
        ))

    # -- officials answer --------------------------------------------------

    def post_update(self, target_id: str, *, likelihood: str, timeframe: str,
                    note: str, actor: str) -> dict:
        """Answer 'are we coming' and 'roughly when', separately.

        Chains to the request or issue rather than editing it, so the whole
        sequence of answers survives — including the ones that turned out to be
        wrong, which is the part an after-action review actually needs.
        """
        if likelihood not in LIKELIHOOD:
            raise ValueError(f"likelihood must be one of {sorted(LIKELIHOOD)}")
        if timeframe not in TIMEFRAME:
            raise ValueError(f"timeframe must be one of {sorted(TIMEFRAME)}")
        target = self.store.get(target_id)
        if target is None or target.get("signal_type") not in (REQUEST_TYPE, ISSUE_TYPE):
            raise KeyError(f"nothing to update with id {target_id!r}")

        return self.store.publish(make_signal(
            module_id=self.module_id,
            title=f"{LIKELIHOOD[likelihood]} · {TIMEFRAME[timeframe]}",
            signal_type=UPDATE_TYPE,
            source_type="official",
            description=(note or "").strip()[:500],
            raw={"target_id": target_id, "likelihood": likelihood,
                 "timeframe": timeframe, "actor": actor},
        ))

    def _updates(self) -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = {}
        for signal in self.store.fetch(limit=0, signal_type=UPDATE_TYPE,
                                       module_id=self.module_id):
            raw = signal.get("raw") or {}
            if not raw.get("target_id"):
                continue
            out.setdefault(raw["target_id"], []).append({
                "likelihood": raw.get("likelihood"),
                "likelihood_label": LIKELIHOOD.get(raw.get("likelihood"), ""),
                "timeframe": raw.get("timeframe"),
                "timeframe_label": TIMEFRAME.get(raw.get("timeframe"), ""),
                "note": signal.get("description", ""),
                "actor": raw.get("actor"),
                "at": signal.get("created_at"),
            })
        return out

    # -- news --------------------------------------------------------------

    def post_news(self, *, title: str, body: str, agency: str, category: str,
                  actor: str, area: str | None = None, link: str | None = None,
                  lat: float | None = None, lng: float | None = None) -> dict:
        """A periodic update from a named agency.

        The agency is recorded as a field rather than baked into the text, so
        a reader can filter to the source they trust and an after-action review
        can ask who said what — the same reason the rest of this uses typed
        signals instead of free text.
        """
        if category not in NEWS_CATEGORIES:
            category = "general"
        if agency not in NEWS_AGENCIES:
            raise ValueError(f"unknown agency {agency!r}")
        title = (title or "").strip()[:200]
        if not title:
            raise ValueError("an update needs a headline")

        return self.store.publish(make_signal(
            module_id=self.module_id,
            title=title,
            signal_type=NEWS_TYPE,
            source_type="official",
            description=(body or "").strip()[:2000],
            source=NEWS_AGENCIES[agency],
            severity="severe" if category in URGENT_CATEGORIES else "moderate",
            place_name=area,
            link=link,
            lat=lat, lng=lng,
            raw={"agency": agency, "agency_name": NEWS_AGENCIES[agency],
                 "category": category, "category_label": NEWS_CATEGORIES[category],
                 "urgent": category in URGENT_CATEGORIES, "actor": actor},
        ))

    def news(self, *, agency: str | None = None,
             category: str | None = None, limit: int = 100) -> list[dict]:
        """Newest first, optionally filtered by who said it or what it is about."""
        out = []
        for signal in self.store.fetch(limit=0, signal_type=NEWS_TYPE,
                                       module_id=self.module_id):
            raw = signal.get("raw") or {}
            if agency and raw.get("agency") != agency:
                continue
            if category and raw.get("category") != category:
                continue
            out.append({
                "id": signal["id"],
                "title": signal.get("title"),
                "body": signal.get("description", ""),
                "agency": raw.get("agency"),
                "agency_name": raw.get("agency_name"),
                "category": raw.get("category"),
                "category_label": raw.get("category_label"),
                "urgent": bool(raw.get("urgent")),
                "area": signal.get("place_name"),
                "link": signal.get("link"),
                "lat": signal.get("lat"), "lng": signal.get("lng"),
                "actor": raw.get("actor"),
                "at": signal.get("created_at"),
            })
        out.reverse()
        return out[:limit]

    # -- reading -----------------------------------------------------------

    def requests(self, *, viewer: str = "public",
                 author_id: str | None = None) -> list[dict]:
        official = viewer == "official"
        updates = self._updates()
        out = []
        for signal in self.store.fetch(limit=0, signal_type=REQUEST_TYPE,
                                       module_id=self.module_id):
            raw = signal.get("raw") or {}
            mine = author_id is not None and raw.get("author_id") == author_id
            if raw.get("visibility") != "public" and not (official or mine):
                continue

            timeline = updates.get(signal["id"], [])
            latest = timeline[-1] if timeline else None
            item = {
                "id": signal["id"], "type": REQUEST_TYPE,
                "title": signal.get("title"),
                "detail": signal.get("description", ""),
                "need": raw.get("need"), "urgency": raw.get("urgency"),
                "urgency_label": URGENCY.get(raw.get("urgency"), ""),
                "people": raw.get("people"),
                "author_name": raw.get("author_name"),
                "lat": signal.get("lat"), "lng": signal.get("lng"),
                "place_name": signal.get("place_name"),
                "at": signal.get("created_at"),
                "visibility": raw.get("visibility"),
                "mine": mine,
                "answered": bool(latest),
                "likelihood": latest["likelihood"] if latest else None,
                "likelihood_label": latest["likelihood_label"] if latest else "Waiting for WCC",
                "timeframe_label": latest["timeframe_label"] if latest else "",
                "timeline": timeline,
            }
            # A phone number belongs to whoever needs help, and to the people
            # coming to help them. Not to the map.
            if official or mine:
                item["contact"] = raw.get("contact")
            out.append(item)
        return out

    def issues(self) -> list[dict]:
        """Published issues are public by definition — that is the point."""
        updates = self._updates()
        out = []
        for signal in self.store.fetch(limit=0, signal_type=ISSUE_TYPE,
                                       module_id=self.module_id):
            raw = signal.get("raw") or {}
            timeline = updates.get(signal["id"], [])
            latest = timeline[-1] if timeline else None
            out.append({
                "id": signal["id"], "type": ISSUE_TYPE,
                "title": signal.get("title"),
                "detail": signal.get("description", ""),
                "state": raw.get("state", "active"),
                "state_label": ISSUE_STATES.get(raw.get("state", "active"), "Active"),
                "severity": signal.get("severity"),
                "lat": signal.get("lat"), "lng": signal.get("lng"),
                "place_name": signal.get("place_name"),
                "actor": raw.get("actor"),
                "at": signal.get("created_at"),
                "likelihood_label": latest["likelihood_label"] if latest else "",
                "timeframe_label": latest["timeframe_label"] if latest else "",
                "timeline": timeline,
            })
        return out
