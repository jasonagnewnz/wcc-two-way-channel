"""Images: where they were taken, and what to strip before publishing.

A photo already knows where it was taken. That is the whole idea here — a
resident photographs a flooded street and the pin places itself, with no map
interaction at all, which matters when someone is standing in the rain holding
an umbrella.

The same fact is a privacy problem. EXIF is a precise record of where a person
was and when, plus the camera, sometimes its serial number, sometimes the
owner's name. So the deal is explicit:

    read the location  ->  show the uploader what was read  ->  publish a copy
                           with the metadata removed

We keep the coordinates we were given and throw the rest away. The published
image carries no EXIF at all.

No Pillow, no exifread. A JPEG is a chain of length-prefixed segments and EXIF
is a TIFF structure inside one of them; both are a hundred lines of struct
unpacking. Keeping this dependency-free is the same argument as the rest of
the repo — one `pip install` that fails on someone's laptop is a demo that
does not happen.
"""

from __future__ import annotations

import struct

JPEG_MAGIC = b"\xff\xd8\xff"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

# Only these two. An "image" that is really SVG or HTML executes as script on
# our own origin the moment a browser opens it, which is a stored-XSS hole
# dressed up as a photo. Magic bytes, not the filename and not the
# Content-Type the client claims.
ALLOWED = {"image/jpeg": JPEG_MAGIC, "image/png": PNG_MAGIC}

MAX_IMAGE_BYTES = 8 * 1024 * 1024


class BadImage(ValueError):
    """Not an image we will accept."""


def sniff(data: bytes) -> str:
    """Return the real content type, or raise. Never trusts the client."""
    if len(data) > MAX_IMAGE_BYTES:
        raise BadImage(f"images are limited to {MAX_IMAGE_BYTES // (1024 * 1024)} MB")
    if data.startswith(PNG_MAGIC):
        return "image/png"
    if data.startswith(JPEG_MAGIC):
        return "image/jpeg"
    raise BadImage("that is not a JPEG or PNG — those are the only formats accepted")


# ---------------------------------------------------------------------------
# JPEG segment walking
# ---------------------------------------------------------------------------

def _segments(data: bytes):
    """Yield (marker, start, end) for each JPEG segment.

    `end` is one past the segment's payload. Stops at start-of-scan, because
    everything after that is entropy-coded pixel data with no segment
    structure to walk.
    """
    i = 2  # past SOI
    while i < len(data) - 1:
        if data[i] != 0xFF:
            break
        marker = data[i + 1]
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        if marker == 0xDA:  # start of scan
            yield marker, i, len(data)
            return
        if i + 4 > len(data):
            break
        length = struct.unpack(">H", data[i + 2:i + 4])[0]
        yield marker, i, i + 2 + length
        i += 2 + length


def _exif_block(data: bytes) -> bytes | None:
    for marker, start, end in _segments(data):
        if marker == 0xE1 and data[start + 4:start + 10] == b"Exif\x00\x00":
            return data[start + 10:end]
    return None


# ---------------------------------------------------------------------------
# TIFF / EXIF parsing
# ---------------------------------------------------------------------------

_TYPE_SIZE = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 7: 1, 9: 4, 10: 8}


def _read_ifd(block: bytes, offset: int, endian: str) -> dict:
    """Read one IFD into {tag: (type, count, value_offset_or_inline)}."""
    if offset + 2 > len(block):
        return {}
    count = struct.unpack(endian + "H", block[offset:offset + 2])[0]
    entries = {}
    for i in range(count):
        at = offset + 2 + i * 12
        if at + 12 > len(block):
            break
        tag, typ, n = struct.unpack(endian + "HHI", block[at:at + 8])
        raw = block[at + 8:at + 12]
        size = _TYPE_SIZE.get(typ, 0) * n
        if size and size <= 4:
            payload = raw[:size]
        else:
            (ptr,) = struct.unpack(endian + "I", raw)
            payload = block[ptr:ptr + size] if 0 <= ptr < len(block) else b""
        entries[tag] = (typ, n, payload)
    return entries


def _rationals(payload: bytes, endian: str, n: int) -> list[float]:
    out = []
    for i in range(n):
        chunk = payload[i * 8:(i + 1) * 8]
        if len(chunk) < 8:
            break
        num, den = struct.unpack(endian + "II", chunk)
        out.append(num / den if den else 0.0)
    return out


def _dms_to_degrees(parts: list[float], ref: str) -> float | None:
    """Degrees/minutes/seconds to a signed decimal degree."""
    if len(parts) < 3:
        return None
    degrees = parts[0] + parts[1] / 60 + parts[2] / 3600
    if ref.upper() in ("S", "W"):
        degrees = -degrees
    return round(degrees, 6)


def _ascii(payload: bytes) -> str:
    return payload.split(b"\x00")[0].decode("ascii", "ignore").strip()


def read_location(data: bytes) -> dict:
    """Extract only what we intend to use: GPS position and capture time.

    Returns {} when there is nothing, which is the common case — most phones
    strip GPS when sharing, and every social platform does. That is not a
    failure, so the interface asks for a pin instead rather than complaining.
    """
    if not data.startswith(JPEG_MAGIC):
        return {}
    block = _exif_block(data)
    if not block or len(block) < 8:
        return {}

    endian = "<" if block[:2] == b"II" else ">" if block[:2] == b"MM" else None
    if endian is None:
        return {}
    try:
        (ifd0_offset,) = struct.unpack(endian + "I", block[4:8])
        ifd0 = _read_ifd(block, ifd0_offset, endian)

        result: dict = {}

        # Capture time lives in the Exif sub-IFD.
        if 0x8769 in ifd0:
            (exif_ptr,) = struct.unpack(endian + "I", ifd0[0x8769][2][:4])
            exif_ifd = _read_ifd(block, exif_ptr, endian)
            if 0x9003 in exif_ifd:
                taken = _ascii(exif_ifd[0x9003][2])
                if taken:
                    result["taken_at"] = taken

        if 0x8825 not in ifd0:
            return result

        (gps_ptr,) = struct.unpack(endian + "I", ifd0[0x8825][2][:4])
        gps = _read_ifd(block, gps_ptr, endian)

        lat_ref = _ascii(gps[1][2]) if 1 in gps else "N"
        lng_ref = _ascii(gps[3][2]) if 3 in gps else "E"
        lat = _dms_to_degrees(_rationals(gps[2][2], endian, 3), lat_ref) if 2 in gps else None
        lng = _dms_to_degrees(_rationals(gps[4][2], endian, 3), lng_ref) if 4 in gps else None

        if lat is not None and lng is not None and (lat or lng):
            result["lat"] = lat
            result["lng"] = lng
            result["source"] = "photo metadata"
        return result
    except (struct.error, KeyError, IndexError, ValueError):
        # A malformed or truncated EXIF block is not worth a stack trace. The
        # photo is still a photo; it just has to be placed by hand.
        return {}


# ---------------------------------------------------------------------------
# stripping
# ---------------------------------------------------------------------------

def strip_metadata(data: bytes) -> bytes:
    """Return the image with its metadata removed.

    JPEG: drop every APPn segment. That takes EXIF (APP1) with it, and also
    XMP, IPTC and colour-profile blocks — none of which we need, all of which
    can carry names, serial numbers and location. JFIF (APP0) goes too; every
    decoder in use copes without it.

    PNG: drop the ancillary text chunks (tEXt/zTXt/iTXt) and the timestamp.

    Deliberately blunt. The alternative is deciding which metadata is harmless
    on someone else's behalf, in a hurry, about a photo of their own street.
    """
    if data.startswith(PNG_MAGIC):
        return _strip_png(data)
    if not data.startswith(JPEG_MAGIC):
        return data

    out = bytearray(data[:2])  # SOI
    for marker, start, end in _segments(data):
        if 0xE0 <= marker <= 0xEF:  # APP0..APP15
            continue
        out += data[start:end]
    return bytes(out)


def _strip_png(data: bytes) -> bytes:
    drop = {b"tEXt", b"zTXt", b"iTXt", b"tIME", b"eXIf"}
    out = bytearray(data[:8])
    i = 8
    while i + 8 <= len(data):
        (length,) = struct.unpack(">I", data[i:i + 4])
        kind = data[i + 4:i + 8]
        nxt = i + 12 + length
        if kind not in drop:
            out += data[i:nxt]
        if kind == b"IEND":
            break
        i = nxt
    return bytes(out)


def has_metadata(data: bytes) -> bool:
    """Whether anything would be stripped — used to tell the uploader."""
    if data.startswith(JPEG_MAGIC):
        return any(0xE0 <= m <= 0xEF for m, _, _ in _segments(data))
    if data.startswith(PNG_MAGIC):
        return len(strip_metadata(data)) != len(data)
    return False
