# SPDX-License-Identifier: GPL-3.0-only
"""Tests for the bundled chart font and the tool that reads its advance widths."""

import hashlib
import json
import pathlib
import struct
import sys
import unittest
import xml.etree.ElementTree as ET

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "tools"))

import font_metrics  # noqa: E402
from datahorizon import chart_font  # noqa: E402
from datahorizon import chart_render as cr  # noqa: E402

FONTS = HERE.parent / "service" / "datahorizon" / "fonts"
FIX = HERE / "fixtures" / "charts"


def load(name):
    return json.loads((FIX / f"{name}.json").read_text())


def _cmap(chars, glyph_of, cmap_format):
    """A cmap table with one subtable; format 12 is (3, 10), format 4 is (3, 1)."""
    codes = sorted(ord(c) for c in chars)
    if cmap_format == 12:
        body = b"".join(struct.pack(">LLL", c, c, glyph_of[c]) for c in codes)
        sub = struct.pack(">HHLLL", 12, 0, 16 + len(body), 0, len(codes)) + body
        record = struct.pack(">HHL", 3, 10, 12)
    else:
        ends = codes + [0xFFFF]
        starts = codes + [0xFFFF]
        deltas = [(glyph_of[c] - c) % 65536 for c in codes] + [1]
        count = len(ends)
        length = 16 + 8 * count
        sub = struct.pack(">HHHHHHH", 4, length, 0, count * 2, 0, 0, 0)
        sub += struct.pack(f">{count}H", *ends) + struct.pack(">H", 0) + struct.pack(f">{count}H", *starts)
        sub += struct.pack(f">{count}H", *deltas) + struct.pack(f">{count}H", *([0] * count))
        record = struct.pack(">HHL", 3, 1, 12)
    return struct.pack(">HH", 0, 1) + record + sub


def build_font(units_per_em, advances, cmap_format=4, long_metrics=None):
    """A minimal TrueType file: head, hhea, maxp, hmtx and a cmap, enough for ``advances``."""
    chars = list(advances)
    glyph_of = {ord(c): index + 1 for index, c in enumerate(chars)}
    units = [advances[chars[0]]] + [advances[c] for c in chars]  # glyph 0 is .notdef
    long_count = len(units) if long_metrics is None else long_metrics
    head = bytearray(54)
    struct.pack_into(">H", head, 18, units_per_em)
    hhea = bytearray(36)
    struct.pack_into(">H", hhea, 34, long_count)
    maxp = struct.pack(">LH", 0x00005000, len(units))
    hmtx = b"".join(struct.pack(">Hh", units[i], 0) for i in range(long_count))
    hmtx += b"".join(struct.pack(">h", 0) for _ in range(len(units) - long_count))
    tables = {b"cmap": _cmap(chars, glyph_of, cmap_format), b"head": bytes(head), b"hhea": bytes(hhea),
              b"hmtx": hmtx, b"maxp": maxp}
    out = struct.pack(">LHHHH", 0x00010000, len(tables), 0, 0, 0)
    offset = 12 + 16 * len(tables)
    body = b""
    for tag, data in sorted(tables.items()):
        out += struct.pack(">4sLLL", tag, 0, offset + len(body), len(data))
        body += data + b"\0" * (-len(data) % 4)
    return out + body


class Reader(unittest.TestCase):
    def test_reads_advances_from_a_minimal_truetype_file(self):
        data = build_font(1000, {"A": 600, "i": 250, " ": 300})
        self.assertEqual(font_metrics.advances(data), {" ": 0.3, "A": 0.6, "i": 0.25})

    def test_reads_format_12_cmap_and_shared_last_advance(self):
        data = build_font(2048, {"A": 1024, "\U0001F600": 2048}, cmap_format=12, long_metrics=1)
        self.assertEqual(font_metrics.advances(data), {"A": 0.5, "\U0001F600": 0.5})

    def test_rejects_a_file_that_is_not_truetype(self):
        with self.assertRaises(ValueError):
            font_metrics.advances(b"not a font")


class Bundled(unittest.TestCase):
    def test_table_matches_the_bundled_font(self):
        table = json.loads((FONTS / "DejaVuSans-metrics.json").read_text())
        data = chart_font.font_path().read_bytes()
        self.assertEqual(table["sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(table["advances"], font_metrics.advances(data))
        self.assertEqual(table["family"], chart_font.FAMILY)

    def test_digits_share_one_advance(self):
        m = chart_font.default_metrics()
        self.assertEqual(len({m[d] for d in "0123456789"}), 1)

    def test_render_names_the_family_and_uses_the_table(self):
        out = cr.render(load("column_clustered"), 800, 500)
        families = {element.get("font-family") for element in ET.fromstring(out["svg"]).iter() if element.get("font-family")}
        self.assertEqual(families, {"DejaVu Sans"})  # one family: Qt SVG reads a list as one unknown name
        self.assertEqual(cr.render(load("column_clustered"), 800, 500, chart_font.default_metrics()), out)


if __name__ == "__main__":
    unittest.main()
