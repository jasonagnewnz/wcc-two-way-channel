"""Append-only signal log, backed by a JSONL file.

Why append-only: the platform SDK exposes `publish_signal` (insert),
`fetch_signals` (read) and `on_new_signals` (subscribe) — but no
`update_signal`. Status changes therefore chain as *new* signals that
reference the original, rather than mutating a row. See
reference/report-status-design.md, "Why append-only, not update-in-place".

That constraint turns out to be a feature. An emergency-management body has
to be able to answer "what did we know, and when did we know it" after the
event. An immutable log answers that; a mutable status column does not.

This file is the local stand-in for the platform's Supabase table. Same
shape, same semantics, no network and no credentials — so the prototype runs
on a bare checkout. `loader.py` swaps it for the real SDK when that exists.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
from pathlib import Path

from .signals import utc_now

# Unambiguous alphabet: no 0/O, no 1/I/L. These codes get read aloud over a
# phone and written on paper during an emergency, so a character that can be
# mistaken for another is a real defect, not a nicety.
_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "data" / "signals.jsonl"


def new_reference() -> str:
    """A short, speakable reference code — the reporter's only claim on their
    report. Possession of the code is the whole auth model (see the design
    doc); there are no accounts.
    """
    body = "".join(secrets.choice(_ALPHABET) for _ in range(5))
    return f"WLG-{body}"


class SignalStore:
    """Append-only JSONL store. Safe for concurrent readers and writers within
    one process, which is all ThreadingHTTPServer needs.
    """

    def __init__(self, path: str | Path = DEFAULT_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._signals: list[dict] = []
        self._by_id: dict[str, dict] = {}
        self._by_idem: dict[str, dict] = {}
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
                    sig = json.loads(line)
                except json.JSONDecodeError:
                    # A torn final line from a hard kill. Skip it rather than
                    # refusing to start — a demo that will not boot because of
                    # one bad row is worse than a demo missing one row.
                    continue
                self._index(sig)

    def _index(self, sig: dict) -> None:
        self._signals.append(sig)
        self._by_id[sig["id"]] = sig
        idem = sig.get("idempotency_key")
        if idem:
            self._by_idem[idem] = sig

    def _write(self, sig: dict) -> None:
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(sig, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    # -- writes ------------------------------------------------------------

    def publish(self, signal: dict) -> dict:
        """Append a signal and return it, now carrying `id` and `created_at`.

        Mirrors `wcc_impact.publish_signal(**fields)`. If the signal carries an
        `idempotency_key` that has been seen before, the original is returned
        untouched and nothing is written — a restart mid-poll must not create
        a second copy of the same report.
        """
        with self._lock:
            idem = signal.get("idempotency_key")
            if idem and idem in self._by_idem:
                return self._by_idem[idem]

            stored = dict(signal)
            stored["id"] = new_reference()
            while stored["id"] in self._by_id:  # collision: 31^5 space, but cheap to check
                stored["id"] = new_reference()
            stored["created_at"] = utc_now()
            stored.setdefault("reported_at", stored["created_at"])

            self._index(stored)
            self._write(stored)
            return stored

    # -- reads -------------------------------------------------------------

    def get(self, signal_id: str) -> dict | None:
        return self._by_id.get(signal_id)

    def fetch(self, *, limit: int = 50, signal_type: str | None = None,
              module_id: str | None = None, since: int = 0) -> list[dict]:
        """Mirrors `wcc_impact.fetch_signals`, plus a `since` cursor so the
        browser can poll for only what is new.
        """
        with self._lock:
            rows = self._signals[since:]
            if signal_type:
                rows = [s for s in rows if s.get("signal_type") == signal_type]
            if module_id:
                rows = [s for s in rows if s.get("module_id") == module_id]
            return list(rows[-limit:]) if limit else list(rows)

    def count(self) -> int:
        with self._lock:
            return len(self._signals)
