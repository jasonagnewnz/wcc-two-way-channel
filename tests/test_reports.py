"""Tests for the two-way loop.

    python3 -m unittest discover tests -v

Standard library `unittest` on purpose — same reason the app has no
framework. `python3 -m unittest` works on a bare checkout on anyone's laptop.

What is worth testing in a one-day prototype is not coverage. It is the
handful of behaviours that, if they broke, would break the demo or mislead
the council: the acknowledgement actually fires, status is derived from the
chain rather than stored, grouping does what the interface claims, and the
log stays append-only across a restart.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.reports import (  # noqa: E402
    RECEIVED, REPORT_TYPE, RESOLVED, RESPONDING, REVIEWING, STATUS_TYPE,
    ReportService, haversine_m,
)
from core.signals import make_signal  # noqa: E402
from core.store import SignalStore, new_reference  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "signals.jsonl"
        self.store = SignalStore(self.path)
        self.svc = ReportService(self.store)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def submit(self, **overrides) -> dict:
        payload = dict(title="Water over the road", description="Getting deeper",
                       issue_type="flooding", lat=-41.2432, lng=174.8100,
                       place_name="Ngauranga")
        payload.update(overrides)
        return self.svc.submit_report(**payload)


class TestTheLoop(Base):
    """The thing Problem 02 actually asks for."""

    def test_submitting_returns_a_speakable_reference_code(self):
        report = self.submit()
        self.assertRegex(report["id"], r"^WLG-[A-Z2-9]{5}$")
        # No character that can be misheard or misread when it is spelled out
        # over a phone or written on a whiteboard.
        for bad in "OI10L":
            self.assertNotIn(bad, report["id"].split("-")[1])

    def test_a_new_report_is_acknowledged_without_a_human(self):
        report = self.submit()
        self.assertEqual(self.svc.latest_status(report["id"]), RECEIVED)

    def test_reporter_sees_every_step_in_order(self):
        ref = self.submit()["id"]
        self.svc.set_status(ref, REVIEWING, note="Duty officer checking.")
        self.svc.set_status(ref, RESPONDING, note="Crew on the way.")
        self.svc.set_status(ref, RESOLVED, note="Lane reopened.")

        view = self.svc.report_view(ref)
        self.assertEqual([t["status"] for t in view["timeline"]],
                         [RECEIVED, REVIEWING, RESPONDING, RESOLVED])
        self.assertEqual(view["status"], RESOLVED)
        self.assertEqual(view["status_label"], "Resolved")
        self.assertEqual(view["timeline"][-1]["note"], "Lane reopened.")

    def test_unknown_reference_is_not_found_rather_than_empty(self):
        self.assertIsNone(self.svc.report_view("WLG-ZZZZZ"))

    def test_status_of_a_missing_report_is_refused(self):
        with self.assertRaises(KeyError):
            self.svc.set_status("WLG-ZZZZZ", REVIEWING)

    def test_status_vocabulary_is_closed(self):
        ref = self.submit()["id"]
        with self.assertRaises(ValueError):
            self.svc.set_status(ref, "having-a-look")


class TestAppendOnly(Base):
    """The log is the audit trail. It must never be rewritten."""

    def test_status_change_appends_rather_than_mutates(self):
        report = self.submit()
        before = dict(self.store.get(report["id"]))
        self.svc.set_status(report["id"], REVIEWING)
        after = self.store.get(report["id"])
        # The report signal itself is untouched by a status change.
        self.assertEqual(before.get("raw"), after.get("raw"))
        self.assertEqual(before["title"], after["title"])

    def test_status_signals_chain_to_the_original(self):
        ref = self.submit()["id"]
        self.svc.set_status(ref, REVIEWING)
        chain = self.store.fetch(limit=0, signal_type=STATUS_TYPE)
        self.assertTrue(chain)
        for signal in chain:
            self.assertEqual(signal["raw"]["original_signal_id"], ref)
            self.assertEqual(signal["source_type"], "official")

    def test_double_tap_does_not_duplicate_an_update(self):
        ref = self.submit()["id"]
        self.svc.set_status(ref, REVIEWING)
        self.svc.set_status(ref, REVIEWING)
        reviewing = [s for s in self.svc.timeline(ref) if s["raw"]["status"] == REVIEWING]
        self.assertEqual(len(reviewing), 1)

    def test_the_log_survives_a_restart(self):
        ref = self.submit()["id"]
        self.svc.set_status(ref, RESPONDING)

        # A completely fresh process reading the same file.
        reopened = ReportService(SignalStore(self.path))
        self.assertEqual(reopened.latest_status(ref), RESPONDING)
        self.assertEqual(len(reopened.reports()), 1)

    def test_a_torn_final_line_does_not_stop_the_server_starting(self):
        self.submit()
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write('{"id": "WLG-BROKEN", "title": incomplete')
        reopened = ReportService(SignalStore(self.path))
        self.assertEqual(len(reopened.reports()), 1)


class TestGrouping(Base):
    """The interface claims 'same issue, within 250 m, within six hours'."""

    def test_nearby_reports_of_the_same_type_group_together(self):
        a = self.submit(title="Water over the road", lat=-41.2432, lng=174.8100)
        b = self.submit(title="Flooding getting deeper", lat=-41.2434, lng=174.8112)
        groups = self.svc.group()
        self.assertEqual(groups[a["id"]], groups[b["id"]])

    def test_distant_reports_stay_separate(self):
        a = self.submit(lat=-41.2432, lng=174.8100)
        b = self.submit(lat=-41.3110, lng=174.7810)  # Island Bay, ~9 km away
        groups = self.svc.group()
        self.assertNotEqual(groups[a["id"]], groups[b["id"]])

    def test_different_issue_types_never_group(self):
        a = self.submit(issue_type="flooding", lat=-41.2432, lng=174.8100)
        b = self.submit(issue_type="slip-or-landslide", lat=-41.2432, lng=174.8100)
        groups = self.svc.group()
        self.assertNotEqual(groups[a["id"]], groups[b["id"]])

    def test_unlocated_reports_do_not_all_collapse_into_one_group(self):
        a = self.submit(lat=None, lng=None, place_name="Karori")
        b = self.submit(lat=None, lng=None, place_name="Miramar")
        groups = self.svc.group()
        self.assertNotEqual(groups[a["id"]], groups[b["id"]])

    def test_distance_maths_is_right(self):
        # Wellington railway station to the Beehive, ~900 m on the ground.
        metres = haversine_m(-41.2790, 174.7804, -41.2784, 174.7767)
        self.assertLess(metres, 400)
        self.assertGreater(metres, 250)


class TestSchema(Base):
    """A signal we build must be one the platform would accept."""

    def test_required_fields_are_enforced(self):
        with self.assertRaises(ValueError):
            make_signal(module_id="", title="x", signal_type="y", source_type="official")
        with self.assertRaises(ValueError):
            make_signal(module_id="m", title="", signal_type="y", source_type="official")
        with self.assertRaises(ValueError):
            make_signal(module_id="m", title="x", signal_type="y", source_type="invented")

    def test_long_text_is_truncated_not_rejected(self):
        # Cheatsheet rule 6. A report during an emergency must never be
        # thrown away for being wordy.
        signal = make_signal(module_id="m", title="t" * 500, signal_type="s",
                             source_type="community", description="d" * 5000)
        self.assertEqual(len(signal["title"]), 200)
        self.assertEqual(len(signal["description"]), 2000)

    def test_report_signals_carry_the_platform_shape(self):
        report = self.submit()
        for field in ("module_id", "title", "signal_type", "source_type", "severity"):
            self.assertIn(field, report)
        self.assertEqual(report["signal_type"], REPORT_TYPE)
        self.assertEqual(report["source_type"], "community")

    def test_references_are_unique_across_many_reports(self):
        refs = {new_reference() for _ in range(2000)}
        self.assertGreater(len(refs), 1990)  # collisions must be rare, not impossible


class TestComposability(Base):
    """The brief asks for outputs other teams' maps can read."""

    def test_geojson_is_valid_and_carries_status(self):
        ref = self.submit()["id"]
        self.svc.set_status(ref, RESPONDING)
        fc = self.svc.geojson()

        self.assertEqual(fc["type"], "FeatureCollection")
        self.assertEqual(len(fc["features"]), 1)
        feature = fc["features"][0]
        self.assertEqual(feature["geometry"]["type"], "Point")
        lng, lat = feature["geometry"]["coordinates"]
        # GeoJSON is lng,lat — the classic way to put Wellington in the sea.
        self.assertAlmostEqual(lng, 174.8100, places=3)
        self.assertAlmostEqual(lat, -41.2432, places=3)
        self.assertEqual(feature["properties"]["status"], RESPONDING)
        self.assertEqual(feature["properties"]["reference"], ref)

    def test_reports_without_coordinates_are_left_out_of_geojson(self):
        self.submit(lat=None, lng=None)
        self.assertEqual(self.svc.geojson()["features"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
