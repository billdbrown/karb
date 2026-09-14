#!/usr/bin/env python3
"""Generate Karb's app icons. Run after changing the mark; commit the output.

    python3 make_icons.py

The icons are the app's own energy ring - a three-quarter arc, which is what
the diary screen shows - on the accent green. That is deliberate: an icon that
is a shrunken screenshot of the thing it opens is easier to find on a home
screen full of coloured squares than a generic glyph.

PNG rather than SVG only, because Chrome's install criteria want a raster icon
of at least 192px and SVG support for manifest icons has been inconsistent.
Written with zlib and struct rather than a dependency, since this container
has no pip and the whole app is stdlib-only on purpose.

Two variants, because Android masks icons to whatever shape the launcher uses:
  icon-*.png           rounded square, for contexts that draw it as-is
  icon-maskable-*.png  full bleed, art inside the centre 80% safe zone, so a
                       circular mask cannot clip the ring
"""

import struct
import zlib

BG = (14, 156, 134)        # --accent
RING = (255, 255, 255)
TRACK_ALPHA = 0.30         # the unfilled part of the ring
FILLED = 0.75              # three quarters, matching a good day on the diary
SS = 3                     # supersampling factor per axis


def _blend(dst, src, a):
    return tuple(round(d + (s - d) * a) for d, s in zip(dst, src))


def _sample(x, y, size, radius_frac, stroke_frac, corner_frac):
    """Colour and alpha for one sample point, in the unit square."""
    cx = cy = size / 2.0
    dx, dy = x - cx, y - cy
    dist = (dx * dx + dy * dy) ** 0.5

    # Rounded-square background. corner_frac 0 gives full bleed.
    a = 1.0
    if corner_frac > 0:
        r = size * corner_frac
        qx, qy = abs(dx) - (size / 2.0 - r), abs(dy) - (size / 2.0 - r)
        if qx > 0 and qy > 0 and (qx * qx + qy * qy) ** 0.5 > r:
            a = 0.0
    if a == 0.0:
        return (0, 0, 0), 0.0

    col = BG
    ring_r = size * radius_frac
    half = size * stroke_frac / 2.0
    if abs(dist - ring_r) <= half:
        # Angle from 12 o'clock, clockwise, so the arc fills like the UI does.
        import math
        ang = (math.degrees(math.atan2(dx, -dy)) + 360.0) % 360.0
        col = _blend(BG, RING, 1.0 if ang <= FILLED * 360.0 else TRACK_ALPHA)
    return col, 1.0


def render(size, corner_frac, radius_frac, stroke_frac):
    rows = []
    for py in range(size):
        row = bytearray()
        for px in range(size):
            acc = [0.0, 0.0, 0.0, 0.0]
            for sy in range(SS):
                for sx in range(SS):
                    x = px + (sx + 0.5) / SS
                    y = py + (sy + 0.5) / SS
                    (r, g, b), a = _sample(x, y, size, radius_frac,
                                           stroke_frac, corner_frac)
                    acc[0] += r * a; acc[1] += g * a; acc[2] += b * a; acc[3] += a
            n = SS * SS
            alpha = acc[3] / n
            if alpha <= 0:
                row += bytes((0, 0, 0, 0))
            else:
                row += bytes((round(acc[0] / acc[3]), round(acc[1] / acc[3]),
                              round(acc[2] / acc[3]), round(alpha * 255)))
        rows.append(bytes(row))
    return rows


def write_png(path, rows, size):
    raw = b"".join(b"\x00" + r for r in rows)

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw, 9))
           + chunk(b"IEND", b""))
    with open(path, "wb") as f:
        f.write(png)
    print("%-28s %d x %d, %d bytes" % (path, size, size, len(png)))


if __name__ == "__main__":
    for size in (192, 512):
        # Rounded square: ring at 30% radius leaves the corners breathing.
        write_png("icon-%d.png" % size, render(size, 0.22, 0.30, 0.085), size)
        # Maskable: full bleed, ring pulled in so a circular crop keeps it.
        write_png("icon-maskable-%d.png" % size,
                  render(size, 0.0, 0.26, 0.075), size)
