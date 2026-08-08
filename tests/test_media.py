"""Tests for reading and stripping photo metadata.

Includes a minimal EXIF-writing JPEG builder, because the point of the feature
is that a real camera's GPS lands on the map — so the test has to produce the
byte layout a camera actually writes, not a mock of our own parser.
"""

from __future__ import annotations

import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.media import (  # noqa: E402
    BadImage, has_metadata, read_location, sniff, strip_metadata,
)
from core.uploads import TooLarge, parse_multipart, resolve, store_image  # noqa: E402


# ---------------------------------------------------------------------------
# a JPEG with real EXIF GPS
# ---------------------------------------------------------------------------

def _rational(num: int, den: int) -> bytes:
    return struct.pack("<II", num, den)


def build_jpeg_with_gps(lat: float, lng: float, taken: str = "2026:08:08 13:45:00") -> bytes:
    """A tiny but structurally valid JPEG carrying GPS in EXIF.

    Little-endian TIFF, IFD0 with pointers to an Exif IFD and a GPS IFD —
    the layout a phone writes.
    """
    lat_ref = b"N" if lat >= 0 else b"S"
    lng_ref = b"E" if lng >= 0 else b"W"
    lat, lng = abs(lat), abs(lng)

    def dms(value: float) -> bytes:
        degrees = int(value)
        minutes = int((value - degrees) * 60)
        seconds = (value - degrees - minutes / 60) * 3600
        return (_rational(degrees, 1) + _rational(minutes, 1)
                + _rational(int(round(seconds * 10000)), 10000))

    # Layout inside the TIFF block:
    #   0   header (8)
    #   8   IFD0: 2 entries + next-offset
    #   ..  GPS IFD, Exif IFD, then the values they point at
    header = b"II" + struct.pack("<HI", 42, 8)

    ifd0_size = 2 + 2 * 12 + 4
    gps_offset = 8 + ifd0_size
    gps_entries = 5
    gps_size = 2 + gps_entries * 12 + 4
    exif_offset = gps_offset + gps_size
    exif_size = 2 + 1 * 12 + 4
    values_offset = exif_offset + exif_size

    lat_bytes, lng_bytes = dms(lat), dms(lng)
    taken_bytes = taken.encode("ascii") + b"\x00"

    lat_at = values_offset
    lng_at = lat_at + len(lat_bytes)
    taken_at = lng_at + len(lng_bytes)

    ifd0 = struct.pack("<H", 2)
    ifd0 += struct.pack("<HHII", 0x8825, 4, 1, gps_offset)   # GPS IFD pointer
    ifd0 += struct.pack("<HHII", 0x8769, 4, 1, exif_offset)  # Exif IFD pointer
    ifd0 += struct.pack("<I", 0)

    gps = struct.pack("<H", gps_entries)
    gps += struct.pack("<HHI", 1, 2, 2) + lat_ref + b"\x00\x00\x00"
    gps += struct.pack("<HHII", 2, 5, 3, lat_at)
    gps += struct.pack("<HHI", 3, 2, 2) + lng_ref + b"\x00\x00\x00"
    gps += struct.pack("<HHII", 4, 5, 3, lng_at)
    gps += struct.pack("<HHII", 0, 4, 1, 0)
    gps += struct.pack("<I", 0)

    exif = struct.pack("<H", 1)
    exif += struct.pack("<HHII", 0x9003, 2, len(taken_bytes), taken_at)
    exif += struct.pack("<I", 0)

    tiff = header + ifd0 + gps + exif + lat_bytes + lng_bytes + taken_bytes
    payload = b"Exif\x00\x00" + tiff
    app1 = b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload

    # SOI + APP1 + a minimal SOS/EOI so it is a whole file.
    return b"\xff\xd8" + app1 + b"\xff\xda\x00\x08\x01\x01\x00\x00\x3f\x00" + b"\xff\xd9"


def build_plain_jpeg() -> bytes:
    return b"\xff\xd8\xff\xdb\x00\x04\x00\x00" + b"\xff\xda\x00\x08\x01\x01\x00\x00\x3f\x00" + b"\xff\xd9"


def build_png() -> bytes:
    png = b"\x89PNG\r\n\x1a\n"
    png += struct.pack(">I", 13) + b"IHDR" + struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0) + b"\x00\x00\x00\x00"
    text = b"a comment that should not survive"
    png += struct.pack(">I", len(text)) + b"tEXt" + text + b"\x00\x00\x00\x00"
    png += struct.pack(">I", 0) + b"IEND" + b"\x00\x00\x00\x00"
    return png


# ---------------------------------------------------------------------------

class TestReadLocation(unittest.TestCase):
    def test_gps_is_read_back_accurately(self):
        data = build_jpeg_with_gps(-41.2865, 174.7762)
        found = read_location(data)
        self.assertAlmostEqual(found["lat"], -41.2865, places=3)
        self.assertAlmostEqual(found["lng"], 174.7762, places=3)
        self.assertEqual(found["source"], "photo metadata")

    def test_southern_and_western_hemispheres_are_signed(self):
        # Wellington is south and east. Getting the ref characters wrong puts
        # every New Zealand photo in the North Atlantic.
        found = read_location(build_jpeg_with_gps(-41.2865, 174.7762))
        self.assertLess(found["lat"], 0)
        self.assertGreater(found["lng"], 0)

    def test_capture_time_is_read(self):
        found = read_location(build_jpeg_with_gps(-41.28, 174.77))
        self.assertEqual(found["taken_at"], "2026:08:08 13:45:00")

    def test_a_photo_without_gps_returns_nothing(self):
        # The common case — phones and social platforms strip it. Not an
        # error, so the interface asks for a pin instead of complaining.
        self.assertEqual(read_location(build_plain_jpeg()), {})

    def test_a_png_returns_nothing(self):
        self.assertEqual(read_location(build_png()), {})

    def test_garbage_does_not_raise(self):
        self.assertEqual(read_location(b"\xff\xd8\xff" + b"\x00" * 200), {})


class TestStripping(unittest.TestCase):
    def test_gps_does_not_survive_stripping(self):
        data = build_jpeg_with_gps(-41.2865, 174.7762)
        self.assertTrue(read_location(data))
        cleaned = strip_metadata(data)
        # The whole privacy argument: we keep the coordinates we were given
        # and publish an image that no longer carries them.
        self.assertEqual(read_location(cleaned), {})

    def test_stripping_leaves_a_valid_jpeg(self):
        cleaned = strip_metadata(build_jpeg_with_gps(-41.28, 174.77))
        self.assertTrue(cleaned.startswith(b"\xff\xd8"))
        self.assertTrue(cleaned.endswith(b"\xff\xd9"))

    def test_png_text_chunks_are_removed(self):
        cleaned = strip_metadata(build_png())
        self.assertNotIn(b"tEXt", cleaned)
        self.assertNotIn(b"should not survive", cleaned)
        self.assertTrue(cleaned.startswith(b"\x89PNG"))

    def test_has_metadata_reports_honestly(self):
        self.assertTrue(has_metadata(build_jpeg_with_gps(-41.28, 174.77)))
        self.assertFalse(has_metadata(strip_metadata(build_jpeg_with_gps(-41.28, 174.77))))


class TestSniffing(unittest.TestCase):
    def test_real_images_are_accepted(self):
        self.assertEqual(sniff(build_plain_jpeg()), "image/jpeg")
        self.assertEqual(sniff(build_png()), "image/png")

    def test_svg_is_refused_however_it_is_labelled(self):
        # An SVG served from our origin is script execution wearing a photo's
        # name. The filename and the client's Content-Type are not consulted.
        svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
        with self.assertRaises(BadImage):
            sniff(svg)

    def test_html_is_refused(self):
        with self.assertRaises(BadImage):
            sniff(b"<!doctype html><script>alert(1)</script>")

    def test_oversized_is_refused(self):
        with self.assertRaises(BadImage):
            sniff(b"\xff\xd8\xff" + b"\x00" * (9 * 1024 * 1024))


class TestStoring(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_stored_file_is_stripped_and_renamed(self):
        data = build_jpeg_with_gps(-41.2865, 174.7762)
        stored = store_image(data, self.dir)
        self.assertTrue(stored["stripped"])
        self.assertRegex(stored["name"], r"^[a-f0-9]{16}\.jpg$")
        on_disk = (self.dir / stored["name"]).read_bytes()
        self.assertEqual(read_location(on_disk), {})

    def test_a_client_filename_is_never_used(self):
        stored = store_image(build_png(), self.dir)
        self.assertRegex(stored["name"], r"^[a-f0-9]{16}\.png$")

    def test_oversize_is_refused(self):
        with self.assertRaises((TooLarge, BadImage)):
            store_image(b"\xff\xd8\xff" + b"\x00" * (9 * 1024 * 1024), self.dir)

    def test_resolve_refuses_traversal_and_odd_names(self):
        stored = store_image(build_png(), self.dir)
        self.assertIsNotNone(resolve(stored["name"], self.dir))
        for bad in ("../../etc/passwd", "..%2fx.jpg", "x.svg", "évil.jpg",
                    "", "a" * 40 + ".jpg", "shell.jpg.php"):
            self.assertIsNone(resolve(bad, self.dir), bad)


class TestMultipart(unittest.TestCase):
    def test_fields_and_a_file_are_separated(self):
        boundary = "----abc123"
        body = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="caption"\r\n\r\n'
            "Water over the road\r\n"
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="image"; filename="p.jpg"\r\n'
            "Content-Type: image/jpeg\r\n\r\n"
        ).encode() + build_plain_jpeg() + f"\r\n--{boundary}--\r\n".encode()

        fields = parse_multipart(body, f"multipart/form-data; boundary={boundary}")
        self.assertEqual(fields["caption"], "Water over the road")
        self.assertIsInstance(fields["image"], bytes)
        self.assertTrue(fields["image"].startswith(b"\xff\xd8"))

    def test_a_missing_boundary_is_refused(self):
        with self.assertRaises(ValueError):
            parse_multipart(b"whatever", "multipart/form-data")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestLinkSchemes(unittest.TestCase):
    """A URL is the one user-supplied value a browser treats as an instruction.

    Escaping an href stops an attribute breakout but does nothing about the
    scheme: `javascript:alert(1)` contains no character that escaping touches,
    so it survives intact and runs on click. Caught by an automated review
    after news links shipped without the check that feeds already had.
    """

    def test_plain_http_and_https_are_accepted(self):
        from core.signals import safe_link
        for url in ("http://example.test/a", "https://example.test",
                    "https://example.test/a?b=c#d"):
            self.assertEqual(safe_link(url), url)

    def test_executable_and_inline_schemes_are_refused(self):
        from core.signals import safe_link
        for url in ("javascript:alert(1)", "JaVaScRiPt:alert(1)",
                    "data:text/html,<script>alert(1)</script>",
                    "vbscript:msgbox", "file:///etc/passwd",
                    "//evil.test", "ftp://example.test"):
            with self.assertRaises(ValueError, msg=url):
                safe_link(url)

    def test_empty_is_none_rather_than_an_error(self):
        from core.signals import safe_link
        self.assertIsNone(safe_link(None))
        self.assertIsNone(safe_link("   "))

    def test_news_refuses_a_dangerous_link_at_the_boundary(self):
        import tempfile
        from pathlib import Path as P
        from core.liveops import LiveOpsService
        from core.store import SignalStore
        with tempfile.TemporaryDirectory() as tmp:
            live = LiveOpsService(SignalStore(P(tmp) / "s.jsonl"))
            with self.assertRaises(ValueError):
                live.post_news(title="t", body="b", agency="wcc-em",
                               category="general", actor="x",
                               link="javascript:alert(1)")

    def test_feeds_refuse_a_dangerous_link_at_the_boundary(self):
        import tempfile
        from pathlib import Path as P
        from core.community import CommunityService
        from core.store import SignalStore
        with tempfile.TemporaryDirectory() as tmp:
            community = CommunityService(SignalStore(P(tmp) / "s.jsonl"))
            with self.assertRaises(ValueError):
                community.add_feed(url="javascript:alert(1)", kind="camera",
                                   label="x", author_id="a", author_name="A")
