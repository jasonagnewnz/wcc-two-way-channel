"""Platform loader — the contract the shared platform expects.

    main()    register the module, then poll
    tick()    one cycle: acknowledge anything new
    sample()  one representative signal, no insert

Run it standalone (`python3 loader.py`) and it works against the local
append-only log. Run it with `wcc_impact` importable and it works against the
real platform, unmodified. That is the point: nothing in this repo has to be
rewritten when the SDK lands, and nothing in it is blocked waiting for the
SDK either.

What it actually does
---------------------
Auto-acknowledges. Any `community-report` signal that has no `received`
status yet gets one. That covers reports filed through our own form *and*
reports another module publishes — if the social-feed loader
(loader-sketches/track2_community.py) turns a public post into a report
signal, this closes the loop on it too, without either module knowing about
the other. The append-only log is the whole integration surface.

Resolving the open question about publish_signal's return value
---------------------------------------------------------------
reference/report-status-design.md flags that we do not know whether
`publish_signal()` returns the created signal, and that the reference code
depends on it. PlatformStore below does not care either way: it mints the
reference itself, carries it in `raw.reference` and in `idempotency_key`,
and falls back to that when the SDK returns nothing useful. The question is
worth asking on the day, but it no longer blocks anything.
"""

from __future__ import annotations

import sys

from core.reports import RECEIVED, REPORT_TYPE, STATUS_TYPE, ReportService
from core.signals import utc_now
from core.store import SignalStore, new_reference

MODULE_ID = "team-6-two-way"
POLL_SECONDS = 15


# ---------------------------------------------------------------------------
# backends
# ---------------------------------------------------------------------------

class PlatformStore:
    """Adapts `wcc_impact` to the same surface as SignalStore, so
    ReportService runs against either without knowing which.
    """

    def __init__(self, module_id: str = MODULE_ID):
        import wcc_impact
        self._sdk = wcc_impact
        self.module_id = module_id
        self.path = "wcc_impact (platform)"

    def publish(self, signal: dict) -> dict:
        stored = dict(signal)
        reference = stored.get("id") or new_reference()

        # Carry the reference inside the payload so it survives regardless of
        # what the SDK gives back, and so a status signal can chain to it.
        raw = dict(stored.get("raw") or {})
        raw.setdefault("reference", reference)
        stored["raw"] = raw
        stored.setdefault("idempotency_key", reference)

        returned = self._sdk.publish_signal(**stored)

        if isinstance(returned, dict) and returned.get("id"):
            result = dict(returned)
            result.setdefault("created_at", utc_now())
            return result

        # SDK returned nothing (or nothing with an id) — use our own.
        stored["id"] = reference
        stored.setdefault("created_at", utc_now())
        return stored

    def get(self, signal_id: str) -> dict | None:
        for signal in self.fetch(limit=1000):
            if signal.get("id") == signal_id:
                return signal
            if (signal.get("raw") or {}).get("reference") == signal_id:
                return signal
        return None

    def fetch(self, *, limit: int = 50, signal_type: str | None = None,
              module_id: str | None = None, since: int = 0) -> list[dict]:
        rows = self._sdk.fetch_signals(
            limit=limit or 1000,
            signal_type=signal_type,
            module_id=module_id or self.module_id,
        )
        return list(rows or [])

    def count(self) -> int:
        return len(self.fetch(limit=1000))


def make_service() -> tuple[ReportService, bool]:
    """Return (service, on_platform)."""
    try:
        store = PlatformStore()
        return ReportService(store, MODULE_ID), True
    except ImportError:
        return ReportService(SignalStore(), MODULE_ID), False


_service, ON_PLATFORM = make_service()


# ---------------------------------------------------------------------------
# platform contract
# ---------------------------------------------------------------------------

def tick() -> None:
    """One cycle. Acknowledge every report that has not been acknowledged.

    Idempotent by construction: `set_status` keys on (report, status), so
    re-acknowledging is a no-op rather than a duplicate. That means a crash
    and restart mid-cycle costs nothing.
    """
    acknowledged = {
        (s.get("raw") or {}).get("original_signal_id")
        for s in _service.store.fetch(limit=0, signal_type=STATUS_TYPE,
                                      module_id=MODULE_ID)
        if (s.get("raw") or {}).get("status") == RECEIVED
    }

    new = 0
    for report in _service.store.fetch(limit=0, signal_type=REPORT_TYPE,
                                       module_id=MODULE_ID):
        reference = report.get("id")
        if not reference or reference in acknowledged:
            continue
        _service.set_status(reference, RECEIVED,
                            note="Received by Wellington City Council.",
                            actor="system")
        new += 1

    if new:
        print(f"  acknowledged {new} new report(s)")


def sample() -> dict:
    """One representative signal. No insert — the platform calls this to see
    the shape of what we publish.
    """
    from core.signals import make_signal
    return make_signal(
        module_id=MODULE_ID,
        title="Status: received",
        signal_type=STATUS_TYPE,
        source_type="official",
        description="Received by Wellington City Council.",
        raw={"original_signal_id": "WLG-K7M2Q", "status": RECEIVED, "actor": "system"},
    )


def main() -> None:
    """Entrypoint: register, then poll."""
    if not ON_PLATFORM:
        print("  wcc_impact not importable — running against the local log.")
        print("  This is the same code path the platform uses; only the store differs.\n")
        _poll_locally()
        return

    _service.store._sdk.register_module(
        id=MODULE_ID,
        name="Two-Way Channel",
        icon="\U0001F4EC",
        description="Community report intake with acknowledgement back to the reporter",
    )
    _service.store._sdk.run_every(POLL_SECONDS, tick)


def _poll_locally() -> None:
    import time
    try:
        while True:
            tick()
            time.sleep(POLL_SECONDS)
    except KeyboardInterrupt:
        print("\n  Stopped.")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "sample":
        import json
        print(json.dumps(sample(), indent=2))
    else:
        main()
