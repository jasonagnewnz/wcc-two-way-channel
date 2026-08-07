"""The two-way loop — Problem 02.

Community sends a report in; WCC sends an acknowledgement back out. Both
directions are signals on the same append-only log, so the whole exchange is
one auditable timeline rather than two disconnected systems.

    resident submits          -> community-report signal, id = reference code
    system acknowledges       -> report-status signal, status "received"
    staff taps a button       -> report-status signal, status "reviewing" ...
    reporter opens their code -> reads the chain, sees the latest state

Nothing here mutates a signal. Status is derived by replaying the chain, the
way an event-sourced system derives state — which is why the reporter's view
and WCC's view can never disagree.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from .signals import idempotency_key, make_signal, utc_now
from .store import SignalStore

MODULE_ID = "team-6-two-way"

REPORT_TYPE = "community-report"
STATUS_TYPE = "report-status"

# The one-tap vocabulary. Deliberately small and fixed: staff during an event
# tap a button, they do not compose a sentence. Kept clear of track4's
# action/verify/awareness triage words — different scope, own words (see
# reference/report-status-design.md).
RECEIVED = "received"
REVIEWING = "reviewing"
RESPONDING = "responding"
RESOLVED = "resolved"
STATUSES = (RECEIVED, REVIEWING, RESPONDING, RESOLVED)

STATUS_LABELS = {
    RECEIVED: "Received",
    REVIEWING: "Being checked",
    RESPONDING: "Crew responding",
    RESOLVED: "Resolved",
}

# What a resident can report. Free text stays the primary field; this is only
# to make grouping and map symbology possible.
ISSUE_TYPES = (
    "flooding",
    "slip-or-landslide",
    "road-blocked",
    "power-or-water",
    "building-damage",
    "people-need-help",
    "other",
)

# Two reports are "the same thing" if they are the same issue type, within
# this radius, inside this window. Deliberately arithmetic, not a language
# model: grouping has to be explainable to a duty officer, and has to work
# with no API key and no network.
GROUP_RADIUS_M = 250.0
GROUP_WINDOW_HOURS = 6


# ---------------------------------------------------------------------------
# geometry
# ---------------------------------------------------------------------------

def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in metres."""
    radius = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lng2 - lng1)
    a = (math.sin(dphi / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dlam / 2) ** 2)
    return 2 * radius * math.asin(math.sqrt(a))


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# the service
# ---------------------------------------------------------------------------

class ReportService:
    """Wraps a store. Takes the store as an argument rather than reaching for
    a global so the tests can run against a throwaway file.
    """

    def __init__(self, store: SignalStore, module_id: str = MODULE_ID):
        self.store = store
        self.module_id = module_id

    # -- inbound: community -> WCC ----------------------------------------

    def submit_report(self, *, title: str, description: str = "",
                      issue_type: str = "other",
                      lat: float | None = None, lng: float | None = None,
                      place_name: str | None = None,
                      severity: str = "unknown",
                      media_urls: list[str] | None = None,
                      reporter_kind: str = "resident",
                      hazard_context: dict | None = None) -> dict:
        """A resident, community group or Emergency Hub files a report.

        Returns the stored signal. Its `id` is the reference code handed back
        to the reporter — the only thing they need to check on it later.
        """
        if issue_type not in ISSUE_TYPES:
            issue_type = "other"

        signal = make_signal(
            module_id=self.module_id,
            title=title,
            signal_type=REPORT_TYPE,
            source_type="community",
            source=f"two-way-channel/{reporter_kind}",
            description=description,
            lat=lat,
            lng=lng,
            place_name=place_name,
            severity=severity,
            media_urls=media_urls,
            observed_at=utc_now(),
            raw={
                "issue_type": issue_type,
                "reporter_kind": reporter_kind,
                # Everything the prototype inferred rather than was told, kept
                # in one place and labelled. The interface renders it as
                # inferred, never as fact — the failure mode these problem
                # statements are most wary of.
                "hazard_context": hazard_context or {},
            },
        )
        stored = self.store.publish(signal)

        # Acknowledge immediately. This is the entire point of Problem 02:
        # the reporter must see that their information landed, and no human
        # should have to be free for that to happen.
        self.set_status(stored["id"], RECEIVED,
                        note="Received by Wellington City Council.",
                        actor="system")
        return stored

    # -- outbound: WCC -> community ---------------------------------------

    def set_status(self, reference: str, status: str, *, note: str = "",
                   actor: str = "wcc-staff") -> dict:
        """The one tap. Publishes a NEW status signal chained to the original
        report; never edits the report itself.
        """
        if status not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}, got {status!r}")
        if self.store.get(reference) is None:
            raise KeyError(f"no report with reference {reference!r}")

        signal = make_signal(
            module_id=self.module_id,
            title=f"Status: {status}",
            signal_type=STATUS_TYPE,
            source_type="official",
            description=note,
            source=f"two-way-channel/{actor}",
            raw={
                "original_signal_id": reference,
                "status": status,
                "actor": actor,
            },
            # One acknowledgement per (report, status). A double-tap or a
            # restart mid-poll must not append a second identical update.
            idempotency_key_=idempotency_key(STATUS_TYPE, reference, status),
        )
        return self.store.publish(signal)

    # -- reads -------------------------------------------------------------

    def timeline(self, reference: str) -> list[dict]:
        """Every status update for one report, oldest first."""
        return [
            s for s in self.store.fetch(limit=0, signal_type=STATUS_TYPE,
                                        module_id=self.module_id)
            if (s.get("raw") or {}).get("original_signal_id") == reference
        ]

    def latest_status(self, reference: str) -> str | None:
        chain = self.timeline(reference)
        return (chain[-1]["raw"]["status"] if chain else None)

    def report_view(self, reference: str) -> dict | None:
        """What the reporter's "check my report" screen renders."""
        report = self.store.get(reference)
        if report is None or report.get("signal_type") != REPORT_TYPE:
            return None
        chain = self.timeline(reference)
        return {
            "report": report,
            "status": chain[-1]["raw"]["status"] if chain else None,
            "status_label": STATUS_LABELS.get(
                chain[-1]["raw"]["status"] if chain else "", "Submitted"),
            "timeline": [
                {
                    "status": s["raw"]["status"],
                    "label": STATUS_LABELS.get(s["raw"]["status"], s["raw"]["status"]),
                    "note": s.get("description", ""),
                    "actor": s["raw"].get("actor", ""),
                    "at": s.get("created_at"),
                }
                for s in chain
            ],
        }

    def reports(self) -> list[dict]:
        """Every report with its current status and group — the WCC view."""
        rows = self.store.fetch(limit=0, signal_type=REPORT_TYPE,
                                module_id=self.module_id)
        groups = self.group(rows)
        out = []
        for report in rows:
            ref = report["id"]
            chain = self.timeline(ref)
            status = chain[-1]["raw"]["status"] if chain else None
            out.append({
                **report,
                "status": status,
                "status_label": STATUS_LABELS.get(status or "", "Submitted"),
                "updates": len(chain),
                "group_id": groups.get(ref),
                "issue_type": (report.get("raw") or {}).get("issue_type", "other"),
            })
        return out

    # -- grouping ----------------------------------------------------------

    def group(self, rows: list[dict] | None = None) -> dict[str, str]:
        """Cluster similar reports: same issue type, within GROUP_RADIUS_M,
        within GROUP_WINDOW_HOURS. Returns reference -> group id.

        Single-link agglomeration over a small set. At hackathon scale (tens
        to low hundreds of reports) the quadratic pass is irrelevant, and it
        is far easier for a duty officer to trust than a clustering library.
        """
        if rows is None:
            rows = self.store.fetch(limit=0, signal_type=REPORT_TYPE,
                                    module_id=self.module_id)

        parent: dict[str, str] = {r["id"]: r["id"] for r in rows}

        def find(x: str) -> str:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: str, b: str) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra

        window = timedelta(hours=GROUP_WINDOW_HOURS)
        for i, a in enumerate(rows):
            for b in rows[i + 1:]:
                if not self._similar(a, b, window):
                    continue
                union(a["id"], b["id"])

        return {r["id"]: find(r["id"]) for r in rows}

    @staticmethod
    def _similar(a: dict, b: dict, window: timedelta) -> bool:
        ra, rb = a.get("raw") or {}, b.get("raw") or {}
        if ra.get("issue_type") != rb.get("issue_type"):
            return False

        ta, tb = _parse_time(a.get("created_at")), _parse_time(b.get("created_at"))
        if ta and tb and abs(ta - tb) > window:
            return False

        if None in (a.get("lat"), a.get("lng"), b.get("lat"), b.get("lng")):
            # No coordinates on one side: fall back to a place-name match
            # rather than grouping everything unlocated into one blob.
            pa, pb = (a.get("place_name") or "").lower(), (b.get("place_name") or "").lower()
            return bool(pa) and pa == pb

        return haversine_m(a["lat"], a["lng"], b["lat"], b["lng"]) <= GROUP_RADIUS_M

    # -- composability -----------------------------------------------------

    def geojson(self) -> dict:
        """Every report as a GeoJSON FeatureCollection.

        This is the module's contribution to the shared common operating
        picture. The brief asks for outputs that compose — a feed, an
        endpoint, GeoJSON — over a closed-off UI, so the same data the local
        map draws is available to any other team's map at /api/geojson.
        """
        features = []
        for report in self.reports():
            if report.get("lat") is None or report.get("lng") is None:
                continue
            features.append({
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [report["lng"], report["lat"]],
                },
                "properties": {
                    "reference": report["id"],
                    "title": report.get("title"),
                    "description": report.get("description", ""),
                    "issue_type": report.get("issue_type"),
                    "severity": report.get("severity"),
                    "status": report.get("status"),
                    "status_label": report.get("status_label"),
                    "group_id": report.get("group_id"),
                    "place_name": report.get("place_name"),
                    "reported_at": report.get("created_at"),
                    "source_type": "community",
                    "module_id": self.module_id,
                },
            })
        return {"type": "FeatureCollection", "features": features}
