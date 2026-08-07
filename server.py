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
"""

from __future__ import annotations

import json
import mimetypes
import posixpath
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

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

_REF_RE = re.compile(r"^WLG-[A-Z2-9]{5}$")


class Handler(BaseHTTPRequestHandler):
    server_version = "wcc-two-way/1.0"
    service: ReportService  # injected in serve()

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
        try:
            body = self._read_json()
        except ValueError as exc:
            return self._error(400, str(exc))

        if path == "/api/reports":
            return self._post_report(body)

        match = re.fullmatch(r"/api/reports/([^/]+)/status", path)
        if match:
            return self._post_status(match.group(1), body)

        return self._error(404, "no such endpoint")

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
            return self._send(200, {"cursor": svc.store.count(), "signals": rows})

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
        reference = reference.upper()
        status = str(body.get("status") or "").strip()
        note = str(body.get("note") or "").strip()[:2000]
        actor = str(body.get("actor") or "wcc-staff")[:40]

        try:
            signal = self.service.set_status(reference, status, note=note, actor=actor)
        except KeyError:
            return self._error(404, "no report with that reference code")
        except StoreFull as exc:
            return self._error(503, str(exc))
        except ValueError as exc:
            return self._error(400, str(exc))

        return self._send(201, {"ok": True, "status": status, "signal": signal})

    # -- static ------------------------------------------------------------

    def _serve_static(self, path: str) -> None:
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

    handler = type("BoundHandler", (Handler,), {"service": service})
    httpd = ThreadingHTTPServer((host, port), handler)

    print(f"\n  Two-way channel running:  http://{host}:{port}")
    print(f"  Signal log:               {store.path}  ({store.count()} signals)")
    print(f"  Shared map reads:         http://{host}:{port}/api/geojson")
    print("\n  Ctrl-C to stop.\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.")
    finally:
        httpd.server_close()
