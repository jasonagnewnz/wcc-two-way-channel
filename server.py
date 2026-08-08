"""HTTP server for the two-way channel — standard library only.

No Flask, no FastAPI, no pip install. A teammate clones the repo and runs
`python3 run.py`. That constraint is not minimalism for its own sake: on a
build day, every dependency is a chance for someone's laptop to be the one
where it does not install.

Routes
------
GET  /                          the app (report / my report / WCC ops)
GET  /api/health                liveness
GET  /api/meta                  issue types, statuses, map extent
POST /api/reports               submit a report      -> reference code
GET  /api/reports               all reports + status + group (WCC view)
GET  /api/reports/<ref>         one report + its status timeline (reporter view)
POST /api/reports/<ref>/status  one-tap status update
GET  /api/geojson               all reports as GeoJSON  <- the shared map reads this
GET  /api/signals               the raw append-only log
GET  /api/basemap               cached WCC hazard geometry for the map

GET  /api/banner                the important-comms banner, if one is showing
POST /api/banner                publish or clear it
GET  /api/chat/channels         channel lists, filtered by ?viewer=
GET  /api/chat/messages         ?channel= &viewer= &author_id=
POST /api/chat/messages         post to a board or an agency channel
POST /api/chat/flag             flag / unflag a message
"""

from __future__ import annotations

import json
import mimetypes
import posixpath
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from core.adaptation import summarise as adaptation_summary
from core.chat import ChatService, channel_kind, redact_for_public
from core.community import (
    APPROVED, EVIDENCE_TYPE, FEED_KINDS, FEED_TYPE, PENDING, RESOURCE_KINDS,
    RESOURCE_TYPE, CommunityService,
)
from core.liveops import (
    ISSUE_STATES, ISSUE_TYPE, LIKELIHOOD, NEED_KINDS, NEWS_AGENCIES,
    NEWS_CATEGORIES, REQUEST_TYPE, TIMEFRAME, URGENCY, LiveOpsService,
)
from core.media import read_location
from core.uploads import BadImage, TooLarge, parse_multipart, resolve, store_image
from core.identity import CardStore, ROLES, can, can_issue, card_event
from core.moderation import (
    ContentChallenge, RateLimited, RateLimiter, auto_promote, candidates,
    challenge, score_author,
)
from core.hazard import lookup_async, summary
from core.reports import ISSUE_TYPES, STATUS_LABELS, STATUSES, ReportService
from core.signals import SEVERITIES
from core.store import SignalStore, StoreFull

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"

# Wellington, (w, s, e, n) — same extent wcc_gis uses.
WELLINGTON = (174.62, -41.36, 174.94, -41.14)

# A report body is a title, a sentence or two and a couple of URLs. Anything
# larger is a mistake or an attack; either way we are not reading it into
# memory.
MAX_BODY_BYTES = 64 * 1024
# Bigger for photos, still bounded, and checked from Content-Length before the
# body is read rather than after.
MAX_UPLOAD_BYTES = 9 * 1024 * 1024

_REF_RE = re.compile(r"^WLG-[A-Z2-9]{5}$")


class Handler(BaseHTTPRequestHandler):
    server_version = "wcc-two-way/1.1"
    service: ReportService  # injected in serve()
    chat: ChatService       # injected in serve()
    cards: CardStore        # injected in serve()
    community: CommunityService
    live: LiveOpsService
    limiter: RateLimiter    # injected in serve()
    promoted: dict          # author_id -> role already auto-granted

    # -- plumbing ----------------------------------------------------------

    def log_message(self, fmt: str, *args) -> None:
        # Structured-ish and quiet. Never logs a request body: a report can
        # contain someone's address and what is wrong at it.
        print(f"  {self.command} {self.path.split('?')[0]} -> {args[1] if len(args) > 1 else ''}")

    def _send(self, status: int, payload: dict | list, *, content_type: str = "application/json") -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # The GeoJSON endpoint exists to be read by the other teams' maps, so
        # it has to be cross-origin readable. Read-only data, no credentials,
        # no cookies anywhere in this app.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, message: str) -> None:
        self._send(status, {"error": message})

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_BODY_BYTES:
            raise ValueError("request body too large")
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"body is not valid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("body must be a JSON object")
        return data

    # -- identity ----------------------------------------------------------

    def session(self) -> dict | None:
        """Resolve the caller from their bearer token, or None.

        THE role comes from here and nowhere else. It used to arrive in the
        request body, which meant any caller could post as an agency — the
        client may say who it *is*, never what it is *allowed to do*.
        """
        header = self.headers.get("Authorization") or ""
        token = header[7:].strip() if header.lower().startswith("bearer ") else None
        return self.cards.resolve(token)

    def role(self) -> str:
        session = self.session()
        return session["role"] if session else "resident"

    def require(self, permission: str) -> dict | None:
        """Return the session if it carries `permission`, else send 401/403."""
        session = self.session()
        if session is None:
            self._error(401, "this needs a card — redeem yours under 'Sign in with a card'")
            return None
        if permission not in session["permissions"]:
            self._error(403, _article(ROLES[session["role"]]["label"]) + " card cannot do that")
            return None
        return session

    def client_key(self) -> str:
        """Coarse client identifier for redeem throttling only."""
        return self.client_address[0] if self.client_address else "unknown"

    # -- routing -----------------------------------------------------------

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path.startswith("/api/"):
            return self._get_api(path, query)
        return self._serve_static(path)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path

        # Handled before the JSON body read: this one is multipart.
        if path == "/api/community/evidence":
            return self._post_evidence()

        try:
            body = self._read_json()
        except ValueError as exc:
            return self._error(400, str(exc))

        if path == "/api/reports":
            return self._post_report(body)

        if path == "/api/chat/messages":
            return self._post_message(body)

        if path == "/api/chat/flag":
            return self._post_flag(body)

        if path == "/api/banner":
            return self._post_banner(body)

        if path == "/api/auth/redeem":
            return self._post_redeem(body)

        if path == "/api/auth/signout":
            header = self.headers.get("Authorization") or ""
            if header.lower().startswith("bearer "):
                self.cards.sign_out(header[7:].strip())
            return self._send(200, {"ok": True})

        if path == "/api/auth/issue":
            return self._post_issue(body)

        if path == "/api/auth/revoke":
            return self._post_revoke(body)

        if path == "/api/trust/run":
            return self._post_trust_run(body)

        if path == "/api/community/resource":
            return self._post_resource(body)

        if path == "/api/community/feed":
            return self._post_feed(body)

        if path == "/api/community/moderate":
            return self._post_moderate(body)

        if path == "/api/live/request":
            return self._post_request(body)

        if path == "/api/live/issue":
            return self._post_issue(body)

        if path == "/api/live/update":
            return self._post_update(body)

        if path == "/api/news":
            return self._post_news(body)

        match = re.fullmatch(r"/api/reports/([^/]+)/status", path)
        if match:
            return self._post_status(match.group(1), body)

        return self._error(404, "no such endpoint")

    # -- chat --------------------------------------------------------------

    def _post_message(self, body: dict) -> None:
        session = self.session()
        role = session["role"] if session else "resident"
        text = str(body.get("body") or "")

        # An author_id from the client identifies which browser is posting so
        # it can see its own private messages. It confers nothing. A card
        # holder is keyed on the card instead, so their standing cannot be
        # reset by clearing localStorage.
        author_id = (f"card:{session['card_id']}" if session
                     else str(body.get("author_id") or "")[:64])
        if not author_id:
            return self._error(400, "missing author_id")

        # Card holders post under the name on the card. Everyone else picks
        # one, which is honest: an unbadged name is not a claim about anybody.
        author_name = (session["holder"] if session
                       else str(body.get("author_name") or "Anonymous")[:80])

        # Role is derived, never accepted. Agency posting is gated on the
        # permission, not on a string in the request body.
        author_role = "official" if can(role, "post.agency") else (
            "moderator" if can(role, "moderate.flag") else
            str(body.get("author_role") or "resident")[:20])
        if author_role not in ("resident", "hub", "community-group", "moderator", "official"):
            author_role = "resident"

        try:
            challenge(text, role=role)
            self.limiter.check(author_id, role)
            self.limiter.check_duplicate(author_id, text)
        except RateLimited as exc:
            self._send(429, {"error": str(exc), "retry_after": exc.retry_after})
            return
        except ContentChallenge as exc:
            self._send(422, {"error": str(exc), "challenge": True})
            return

        try:
            message = self.chat.post(
                channel_id=str(body.get("channel_id") or "").strip(),
                body=text,
                author_name=author_name,
                author_id=author_id,
                author_role=author_role,
                agency=(str(body.get("agency"))[:80] if body.get("agency") else None),
                visibility=str(body.get("visibility") or "public"),
                reply_to=(str(body.get("reply_to"))[:32] if body.get("reply_to") else None),
            )
        except PermissionError as exc:
            return self._error(403, str(exc))
        except StoreFull as exc:
            return self._error(503, str(exc))
        except ValueError as exc:
            return self._error(400, str(exc))

        self.limiter.record(author_id, text)
        return self._send(201, {"ok": True, "id": message["id"]})

    def _post_flag(self, body: dict) -> None:
        session = self.require("moderate.flag")
        if session is None:
            return
        try:
            self.chat.flag(
                str(body.get("message_id") or ""),
                reason=str(body.get("reason") or "")[:500],
                actor=session["holder"],
                unflag=bool(body.get("unflag")),
            )
        except KeyError:
            return self._error(404, "no message with that id")
        except StoreFull as exc:
            return self._error(503, str(exc))
        except ValueError as exc:
            return self._error(400, str(exc))
        return self._send(201, {"ok": True})

    def _post_banner(self, body: dict) -> None:
        session = self.require("banner.publish")
        if session is None:
            return
        try:
            self.chat.set_banner(
                text=str(body.get("text") or ""),
                level=str(body.get("level") or "warning"),
                actor=session["holder"],
                active=body.get("active", True) is not False,
            )
        except StoreFull as exc:
            return self._error(503, str(exc))
        except ValueError as exc:
            return self._error(400, str(exc))
        return self._send(201, {"ok": True, "banner": self.chat.banner()})

    # -- GET handlers ------------------------------------------------------

    def _get_api(self, path: str, query: dict) -> None:
        svc = self.service

        if path == "/api/health":
            return self._send(200, {
                "ok": True,
                "module_id": svc.module_id,
                "signals": svc.store.count(),
            })

        if path == "/api/meta":
            return self._send(200, {
                "module_id": svc.module_id,
                "issue_types": list(ISSUE_TYPES),
                "statuses": list(STATUSES),
                "status_labels": STATUS_LABELS,
                "severities": sorted(SEVERITIES),
                "extent": WELLINGTON,
            })

        if path == "/api/reports":
            return self._send(200, {"reports": svc.reports()})

        if path == "/api/geojson":
            return self._send(200, svc.geojson(),
                              content_type="application/geo+json")

        if path == "/api/signals":
            since = _int(query.get("since", ["0"])[0], 0)
            rows = svc.store.fetch(limit=0, since=since)
            if not can(self.role(), "moderate.flag"):
                rows = redact_for_public(rows)
            return self._send(200, {"cursor": svc.store.count(), "signals": rows})

        if path == "/api/chat/channels":
            # Agency channels appear only for a caller whose card can post in
            # them. Previously this read ?viewer= straight off the query
            # string, so anyone could ask for the official view and get it.
            viewer = "official" if can(self.role(), "post.agency") else "public"
            return self._send(200, self.chat.channels(viewer=viewer))

        if path == "/api/chat/messages":
            channel = query.get("channel", [""])[0]
            if not channel:
                return self._error(400, "channel is required")
            role = self.role()
            # Moderators see flags and flagged content; only a card that can
            # post in an agency channel may read one.
            if channel_kind(channel) == "agency":
                viewer = "official" if can(role, "post.agency") else "public"
            else:
                viewer = "official" if can(role, "moderate.flag") else "public"
            try:
                return self._send(200, {"messages": self.chat.messages(
                    channel,
                    viewer=viewer,
                    author_id=query.get("author_id", [None])[0],
                )})
            except PermissionError as exc:
                return self._error(403, str(exc))

        if path == "/api/banner":
            return self._send(200, {"banner": self.chat.banner()})

        if path == "/api/community":
            viewer = "official" if can(self.role(), "moderate.flag") else "public"
            author = query.get("author_id", [None])[0]
            session = self.session()
            if session:
                author = f"card:{session['card_id']}"
            return self._send(200, {
                "resources": self.community.items(RESOURCE_TYPE, viewer=viewer,
                                                  author_id=author),
                "evidence": self.community.items(EVIDENCE_TYPE, viewer=viewer,
                                                 author_id=author),
                "feeds": self.community.items(FEED_TYPE, viewer=viewer,
                                              author_id=author),
                "stacks": self.community.stacks(viewer=viewer),
                "resource_kinds": RESOURCE_KINDS,
                "feed_kinds": FEED_KINDS,
            })

        if path == "/api/live":
            # One page, everything. Composed here so a single request gives the
            # whole operating picture rather than the client stitching six.
            official = can(self.role(), "moderate.flag")
            viewer = "official" if official else "public"
            session = self.session()
            author = (f"card:{session['card_id']}" if session
                      else query.get("author_id", [None])[0])
            return self._send(200, {
                "reports": svc.reports(),
                "stacks": self.community.stacks(viewer=viewer),
                "resources": self.community.items(RESOURCE_TYPE, viewer=viewer,
                                                  author_id=author),
                "evidence": self.community.items(EVIDENCE_TYPE, viewer=viewer,
                                                 author_id=author),
                "feeds": self.community.items(FEED_TYPE, viewer=viewer,
                                              author_id=author),
                "requests": self.live.requests(viewer=viewer, author_id=author),
                "issues": self.live.issues(),
                "banner": self.chat.banner(),
                "vocab": {
                    "needs": NEED_KINDS, "urgency": URGENCY,
                    "likelihood": LIKELIHOOD, "timeframe": TIMEFRAME,
                    "issue_states": ISSUE_STATES,
                },
                "official_view": official,
            })

        if path == "/api/news":
            return self._send(200, {
                "news": self.live.news(
                    agency=query.get("agency", [None])[0] or None,
                    category=query.get("category", [None])[0] or None),
                "agencies": NEWS_AGENCIES,
                "categories": NEWS_CATEGORIES,
                "can_post": can(self.role(), "banner.publish"),
            })

        if path == "/api/adaptation":
            return self._send(200, adaptation_summary(svc.store, svc.module_id))

        if path == "/api/community/queue":
            if self.require("moderate.flag") is None:
                return
            return self._send(200, {"queue": self.community.queue()})

        if path == "/api/auth/me":
            session = self.session()
            return self._send(200, {
                "signed_in": session is not None,
                "session": session,
                "roles": {name: {"label": r["label"], "rank": r["rank"],
                                 "permissions": sorted(r["permissions"]),
                                 "max_issue": r["max_issue"]}
                          for name, r in ROLES.items()},
            })

        if path == "/api/auth/demo-cards":
            # Public on purpose: these codes are printed in the repo. Serving
            # them here as well is what turns signing in from "find the README,
            # copy a code, type it" into one tap.
            try:
                from demo_cards import DEMO_CARDS, DEMO_ISSUER
            except ImportError:
                return self._send(200, {"cards": []})
            live = {c["holder"] for c in self.cards.cards()
                    if c.get("issued_by") == DEMO_ISSUER and not c.get("revoked")}
            return self._send(200, {"cards": [
                {"role": role, "holder": holder, "code": code,
                 "label": ROLES[role]["label"], "note": note}
                for role, holder, code, note in DEMO_CARDS if holder in live
            ]})

        if path == "/api/auth/cards":
            if self.require("card.issue") is None:
                return
            return self._send(200, {"cards": self.cards.cards()})

        if path == "/api/trust/candidates":
            if self.require("moderate.flag") is None:
                return
            return self._send(200, {
                "threshold": __import__("core.moderation", fromlist=["THRESHOLD"]).THRESHOLD,
                "candidates": candidates(self.service.store, self.service.module_id),
                "promoted": self.promoted,
            })

        if path == "/api/basemap":
            return self._serve_file(WEB / "data" / "basemap.json",
                                    fallback={"type": "FeatureCollection", "features": [],
                                              "note": "run tools/fetch_basemap.py to populate"})

        match = re.fullmatch(r"/api/reports/([^/]+)", path)
        if match:
            reference = match.group(1).upper()
            if not _REF_RE.match(reference):
                return self._error(400, "that is not a valid reference code")
            view = svc.report_view(reference)
            if view is None:
                return self._error(404, "no report with that reference code")
            ctx = (view["report"].get("raw") or {}).get("hazard_context") or {}
            view["hazard_summary"] = summary(ctx)
            return self._send(200, view)

        return self._error(404, "no such endpoint")

    # -- POST handlers -----------------------------------------------------

    def _post_report(self, body: dict) -> None:
        title = (body.get("title") or "").strip()
        if not title:
            return self._error(400, "title is required — say what is happening")

        lat, lng = _coord(body.get("lat")), _coord(body.get("lng"))
        media = body.get("media_urls") or []
        if not isinstance(media, list):
            media = []
        media = [str(m)[:2000] for m in media[:4] if str(m).strip()]

        try:
            report = self.service.submit_report(
                title=title,
                description=str(body.get("description") or "").strip(),
                issue_type=str(body.get("issue_type") or "other"),
                lat=lat,
                lng=lng,
                place_name=(str(body.get("place_name")).strip()
                            if body.get("place_name") else None),
                severity=(body.get("severity")
                          if body.get("severity") in SEVERITIES else "unknown"),
                media_urls=media or None,
                reporter_kind=str(body.get("reporter_kind") or "resident")[:40],
                author_id=(f"card:{self.session()['card_id']}" if self.session()
                           else (str(body.get("author_id"))[:64]
                                 if body.get("author_id") else None)),
            )
        except StoreFull as exc:
            # 503, not 500: the request was fine, the service is temporarily
            # unable to accept it. Says exactly what to do about it.
            return self._error(503, str(exc))
        except ValueError as exc:
            return self._error(400, str(exc))

        # Enrichment happens after the reporter already has their code.
        if lat is not None and lng is not None:
            self._enrich_later(report["id"], lat, lng)

        return self._send(201, {
            "reference": report["id"],
            "status": "received",
            "message": "Received by Wellington City Council.",
            "report": report,
        })

    def _enrich_later(self, reference: str, lat: float, lng: float) -> None:
        store = self.service.store

        def attach(context: dict) -> None:
            # The store is append-only, so this is the one place we reach past
            # it: the in-memory record gets the context so the UI can show it
            # this session. It is intentionally NOT rewritten to disk -- the
            # log stays exactly what was published. On restart the context is
            # simply recomputed. Inference does not belong in the audit trail.
            record = store.get(reference)
            if record is not None:
                record.setdefault("raw", {})["hazard_context"] = context

        lookup_async(lat, lng, attach)

    def _post_status(self, reference: str, body: dict) -> None:
        session = self.require("report.status")
        if session is None:
            return
        reference = reference.upper()
        status = str(body.get("status") or "").strip()
        note = str(body.get("note") or "").strip()[:2000]
        actor = session["holder"]

        try:
            signal = self.service.set_status(reference, status, note=note, actor=actor)
        except KeyError:
            return self._error(404, "no report with that reference code")
        except StoreFull as exc:
            return self._error(503, str(exc))
        except ValueError as exc:
            return self._error(400, str(exc))

        return self._send(201, {"ok": True, "status": status, "signal": signal})

    # -- community ---------------------------------------------------------

    def _who(self, body: dict) -> tuple[str, str, bool]:
        """(author_id, author_name, trusted). Trusted skips the queue."""
        session = self.session()
        if session:
            return (f"card:{session['card_id']}", session["holder"], True)
        return (str(body.get("author_id") or "")[:64],
                str(body.get("author_name") or "Anonymous")[:80],
                False)

    def _post_resource(self, body: dict) -> None:
        author_id, author_name, trusted = self._who(body)
        if not author_id:
            return self._error(400, "missing author_id")
        detail = str(body.get("detail") or "").strip()
        if not detail:
            return self._error(400, "say what you're offering, in a few words")
        try:
            item = self.community.offer_resource(
                kind=str(body.get("kind") or "other"),
                detail=detail,
                author_id=author_id, author_name=author_name,
                lat=_coord(body.get("lat")), lng=_coord(body.get("lng")),
                place_name=(str(body.get("place_name"))[:200]
                            if body.get("place_name") else None),
                contact=(str(body.get("contact"))[:120] if body.get("contact") else None),
                visibility=("officials" if body.get("visibility") == "officials"
                            else "public"),
                trusted=trusted,
            )
        except StoreFull as exc:
            return self._error(503, str(exc))
        except ValueError as exc:
            return self._error(400, str(exc))
        return self._send(201, {"ok": True, "id": item["id"],
                                "state": APPROVED if trusted else PENDING})

    def _post_feed(self, body: dict) -> None:
        author_id, author_name, trusted = self._who(body)
        url = str(body.get("url") or "").strip()
        # http(s) only. A javascript: or data: URL rendered as a link is a
        # script somebody else gets to run on this origin.
        if not re.match(r"^https?://[^\s]+$", url, re.I):
            return self._error(400, "that needs to be a full http:// or https:// link")
        try:
            item = self.community.add_feed(
                url=url[:2000], kind=str(body.get("kind") or "page"),
                label=str(body.get("label") or "")[:200],
                author_id=author_id, author_name=author_name,
                lat=_coord(body.get("lat")), lng=_coord(body.get("lng")),
                trusted=trusted)
        except StoreFull as exc:
            return self._error(503, str(exc))
        return self._send(201, {"ok": True, "id": item["id"],
                                "state": APPROVED if trusted else PENDING})

    def _post_moderate(self, body: dict) -> None:
        session = self.require("moderate.flag")
        if session is None:
            return
        try:
            self.community.decide(str(body.get("item_id") or ""),
                                  state=str(body.get("state") or ""),
                                  actor=session["holder"],
                                  reason=str(body.get("reason") or "")[:500])
        except KeyError:
            return self._error(404, "nothing with that id to moderate")
        except ValueError as exc:
            return self._error(400, str(exc))
        return self._send(201, {"ok": True})

    def _post_evidence(self) -> None:
        """multipart/form-data: an image plus a caption."""
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return self._error(400, "empty upload")
        if length > MAX_UPLOAD_BYTES:
            return self._error(413, "that upload is too large — 8 MB maximum")

        raw = self.rfile.read(length)
        try:
            fields = parse_multipart(raw, self.headers.get("Content-Type", ""))
        except ValueError as exc:
            return self._error(400, str(exc))

        image = fields.get("image")
        if not isinstance(image, bytes):
            return self._error(400, "no image in that upload")

        # Read the location BEFORE stripping, since stripping is what removes
        # it. The uploader is shown what was read.
        found = read_location(image)

        try:
            stored = store_image(image)
        except TooLarge as exc:
            return self._error(413, str(exc))
        except BadImage as exc:
            return self._error(415, str(exc))

        author_id, author_name, trusted = self._who(fields)
        lat = _coord(fields.get("lat")) if fields.get("lat") else found.get("lat")
        lng = _coord(fields.get("lng")) if fields.get("lng") else found.get("lng")

        item = self.community.add_evidence(
            image_url=stored["url"],
            caption=str(fields.get("caption") or "")[:500],
            author_id=author_id or "anon", author_name=author_name,
            lat=lat, lng=lng,
            place_name=(str(fields.get("place_name"))[:200]
                        if fields.get("place_name") else None),
            located_by=("photo metadata" if found.get("lat") and not fields.get("lat")
                        else "manual"),
            taken_at=None,
            trusted=trusted)

        return self._send(201, {
            "ok": True, "id": item["id"],
            "state": APPROVED if trusted else PENDING,
            "url": stored["url"],
            "located_by": item["raw"]["located_by"] if item.get("raw") else "manual",
            "lat": lat, "lng": lng,
            "found_location": bool(found.get("lat")),
            "metadata_stripped": stored["stripped"],
        })

    # -- live ops ----------------------------------------------------------

    def _post_request(self, body: dict) -> None:
        author_id, author_name, _ = self._who(body)
        if not author_id:
            return self._error(400, "missing author_id")
        detail = str(body.get("detail") or "").strip()
        if len(detail) < 10:
            return self._error(
                400, "tell us a bit about what you need, so someone can act on it")
        try:
            people = int(body.get("people")) if body.get("people") else None
        except (TypeError, ValueError):
            people = None
        try:
            item = self.live.request_help(
                need=str(body.get("need") or "other"), detail=detail,
                author_id=author_id, author_name=author_name,
                urgency=str(body.get("urgency") or "today"),
                people=people,
                lat=_coord(body.get("lat")), lng=_coord(body.get("lng")),
                place_name=(str(body.get("place_name"))[:200]
                            if body.get("place_name") else None),
                contact=(str(body.get("contact"))[:120] if body.get("contact") else None),
                visibility=str(body.get("visibility") or "officials"))
        except StoreFull as exc:
            return self._error(503, str(exc))
        return self._send(201, {"ok": True, "id": item["id"]})

    def _post_issue(self, body: dict) -> None:
        session = self.require("report.status")
        if session is None:
            return
        title = str(body.get("title") or "").strip()
        if not title:
            return self._error(400, "the issue needs a title")
        try:
            item = self.live.publish_issue(
                title=title, detail=str(body.get("detail") or ""),
                actor=session["holder"],
                state=str(body.get("state") or "active"),
                lat=_coord(body.get("lat")), lng=_coord(body.get("lng")),
                place_name=(str(body.get("place_name"))[:200]
                            if body.get("place_name") else None),
                severity=(body.get("severity")
                          if body.get("severity") in SEVERITIES else "moderate"))
        except StoreFull as exc:
            return self._error(503, str(exc))
        return self._send(201, {"ok": True, "id": item["id"]})

    def _post_news(self, body: dict) -> None:
        # Publishing under an agency's name is the same authority as pushing
        # the banner: it speaks for an organisation to the whole city.
        session = self.require("banner.publish")
        if session is None:
            return
        try:
            item = self.live.post_news(
                title=str(body.get("title") or ""),
                body=str(body.get("body") or ""),
                agency=str(body.get("agency") or ""),
                category=str(body.get("category") or "general"),
                area=(str(body.get("area"))[:120] if body.get("area") else None),
                link=(str(body.get("link"))[:500] if body.get("link") else None),
                actor=session["holder"])
        except StoreFull as exc:
            return self._error(503, str(exc))
        except ValueError as exc:
            return self._error(400, str(exc))
        return self._send(201, {"ok": True, "id": item["id"]})

    def _post_update(self, body: dict) -> None:
        session = self.require("report.status")
        if session is None:
            return
        try:
            self.live.post_update(
                str(body.get("target_id") or ""),
                likelihood=str(body.get("likelihood") or ""),
                timeframe=str(body.get("timeframe") or ""),
                note=str(body.get("note") or ""),
                actor=session["holder"])
        except KeyError:
            return self._error(404, "nothing with that id to update")
        except ValueError as exc:
            return self._error(400, str(exc))
        return self._send(201, {"ok": True})

    # -- auth --------------------------------------------------------------

    def _post_redeem(self, body: dict) -> None:
        try:
            token, card = self.cards.redeem(str(body.get("code") or ""),
                                            client=self.client_key())
        except PermissionError as exc:
            return self._error(429, str(exc))
        except ValueError as exc:
            # Deliberately the same message for "no such card" and "revoked":
            # a redeem endpoint should not tell an unknown caller which codes
            # exist.
            return self._error(401, str(exc))

        card_event(self.service.store, action="redeemed", card_id=card["card_id"],
                   role=card["role"], holder=card["holder"], actor=card["holder"],
                   module_id=self.service.module_id)
        return self._send(200, {"token": token, "session": self.cards.resolve(token)})

    def _post_issue(self, body: dict) -> None:
        session = self.require("card.issue")
        if session is None:
            return
        role = str(body.get("role") or "").strip()
        holder = str(body.get("holder") or "").strip()
        if not holder:
            return self._error(400, "who is this card for?")
        if not can_issue(session["role"], role):
            return self._error(
                403, f"{_article(ROLES[session['role']]['label'])} card can issue "
                     f"up to {ROLES[session['role']]['max_issue']}, not {role!r}")

        code, card = self.cards.issue(role=role, holder=holder,
                                      issued_by=session["holder"],
                                      note=str(body.get("note") or "")[:200])
        card_event(self.service.store, action="issued", card_id=card["card_id"],
                   role=role, holder=holder, actor=session["holder"],
                   module_id=self.service.module_id)
        # The only time the plaintext exists. Print it now or reissue.
        return self._send(201, {"code": code, "card": {k: v for k, v in card.items()
                                                        if k != "code_hash"}})

    def _post_revoke(self, body: dict) -> None:
        session = self.require("card.issue")
        if session is None:
            return
        try:
            card = self.cards.revoke(str(body.get("card_id") or ""),
                                     by=session["holder"])
        except KeyError:
            return self._error(404, "no card with that id")
        card_event(self.service.store, action="revoked", card_id=card["card_id"],
                   role=card["role"], holder=card["holder"], actor=session["holder"],
                   module_id=self.service.module_id)
        return self._send(200, {"ok": True})

    def _post_trust_run(self, body: dict) -> None:
        session = self.require("card.issue")
        if session is None:
            return
        promoted = auto_promote(self.service.store, self.cards,
                                module_id=self.service.module_id,
                                granted=self.promoted)
        return self._send(200, {"promoted": promoted, "count": len(promoted)})

    # -- static ------------------------------------------------------------

    def _serve_static(self, path: str) -> None:
        if path.startswith("/uploads/"):
            return self._serve_upload(path[len("/uploads/"):])
        if path in ("/", ""):
            path = "/index.html"

        # Normalise, then confirm the resolved path is still inside web/.
        # Belt and braces: posixpath.normpath kills ../ sequences, and the
        # relative_to check catches anything symlink-shaped that survives.
        clean = posixpath.normpath(path).lstrip("/")
        target = (WEB / clean).resolve()
        try:
            target.relative_to(WEB.resolve())
        except ValueError:
            return self._error(403, "forbidden")

        if not target.is_file():
            return self._error(404, "not found")
        self._serve_file(target)

    def _serve_upload(self, name: str) -> None:
        """Serve a stored photo, with the headers that keep it a photo."""
        found = resolve(name)
        if found is None:
            return self._error(404, "not found")
        path, content_type = found
        data = path.read_bytes()

        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        # The file is a stranger's upload. nosniff stops a browser deciding it
        # is really HTML; the CSP makes it inert even if one ever did; and
        # attachment-free inline display is fine for an image but never for
        # anything active.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; sandbox")
        self.send_header("Cache-Control", "public, max-age=3600")
        self.end_headers()
        self.wfile.write(data)

    def _serve_file(self, target: Path, fallback: dict | None = None) -> None:
        if not target.is_file():
            if fallback is not None:
                return self._send(200, fallback)
            return self._error(404, "not found")

        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8"
                         if ctype.startswith("text/") or ctype.endswith("json") else ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)


def _article(label: str) -> str:
    """'an Official card', not 'a Official card'."""
    return ("an " if label[:1].lower() in "aeiou" else "a ") + label


def _int(value: str, default: int) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def _coord(value) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def serve(host: str = "127.0.0.1", port: int = 8080,
          store_path: str | None = None) -> None:
    store = SignalStore(store_path) if store_path else SignalStore()
    service = ReportService(store)

    cards = CardStore()
    handler = type("BoundHandler", (Handler,), {
        "service": service,
        "chat": ChatService(store),
        "community": CommunityService(store),
        "live": LiveOpsService(store),
        "cards": cards,
        "limiter": RateLimiter(),
        "promoted": {},
    })
    httpd = ThreadingHTTPServer((host, port), handler)

    print(f"\n  Two-way channel running:  http://{host}:{port}")
    print(f"  Signal log:               {store.path}  ({store.count()} signals)")
    print(f"  Shared map reads:         http://{host}:{port}/api/geojson")
    print(f"  Auth cards:               {cards.path}  ({len(cards.cards())} cards)")
    print("\n  Ctrl-C to stop.\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.")
    finally:
        httpd.server_close()
