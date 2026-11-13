# SPDX-License-Identifier: GPL-3.0-only
"""Read per-character advance widths from a TrueType font (standard library only).

    python3 tools/font_metrics.py <font.ttf> <out.json>

Writes the table the chart renderer lays text out from: each character's
advance in ems for every code point in the font's Unicode cmap from U+0020 up,
except U+007F-U+009F.
"""

import hashlib
import json
import pathlib
import struct
import sys

SIGNATURES = (0x00010000, 0x74727565)  # TrueType outlines, 'true'


def _unpack(fmt: str, data: bytes, offset: int):
    size = struct.calcsize(fmt)
    if offset < 0 or offset + size > len(data):
        raise ValueError("font table offset out of range")
    return struct.unpack_from(fmt, data, offset)


def _tables(data: bytes) -> dict[bytes, tuple[int, int]]:
    if len(data) < 12 or _unpack(">L", data, 0)[0] not in SIGNATURES:
        raise ValueError("not a TrueType font")
    count = _unpack(">H", data, 4)[0]
    tables = {}
    for index in range(count):
        tag, _checksum, offset, length = _unpack(">4sLLL", data, 12 + 16 * index)
        if offset + length > len(data):
            raise ValueError("font table offset out of range")
        tables[tag] = (offset, length)
    return tables


def _cmap_subtable(data: bytes, start: int) -> int:
    """Offset of the preferred Unicode subtable: (3, 10), then platform 0, then (3, 1)."""
    count = _unpack(">H", data, start + 2)[0]
    found = {}
    for index in range(count):
        platform, encoding, offset = _unpack(">HHL", data, start + 4 + 8 * index)
        found[(platform, encoding)] = start + offset
    for key in ((3, 10), (0, 4), (0, 6), (0, 3), (0, 2), (0, 1), (0, 0), (3, 1)):
        if key not in found:
            continue
        # Platform 0 may hold either format; the 4 and 12 forms are the ones read.
        if _unpack(">H", data, found[key])[0] in (4, 12):
            return found[key]
    raise ValueError("no usable Unicode cmap subtable")


def _glyphs_format_12(data: bytes, offset: int):
    groups = _unpack(">L", data, offset + 12)[0]
    for index in range(groups):
        first, last, glyph = _unpack(">LLL", data, offset + 16 + 12 * index)
        for code in range(first, last + 1):
            yield code, glyph + code - first


def _glyphs_format_4(data: bytes, offset: int):
    segments = _unpack(">H", data, offset + 6)[0] // 2
    ends = offset + 14
    starts = ends + 2 * segments + 2
    deltas = starts + 2 * segments
    ranges = deltas + 2 * segments
    for index in range(segments):
        last = _unpack(">H", data, ends + 2 * index)[0]
        first = _unpack(">H", data, starts + 2 * index)[0]
        delta = _unpack(">h", data, deltas + 2 * index)[0]
        range_offset = _unpack(">H", data, ranges + 2 * index)[0]
        for code in range(first, last + 1):
            if range_offset == 0:
                glyph = (code + delta) % 65536
            else:
                position = ranges + 2 * index + range_offset + 2 * (code - first)
                glyph = _unpack(">H", data, position)[0]
                if glyph:
                    glyph = (glyph + delta) % 65536
            yield code, glyph


def _units_per_em(data: bytes, tables) -> int:
    if b"head" not in tables:
        raise ValueError("missing head table")
    units = _unpack(">H", data, tables[b"head"][0] + 18)[0]
    if units == 0:
        raise ValueError("units per em is zero")
    return units


def _read(data: bytes) -> tuple[int, dict[str, float]]:
    tables = _tables(data)
    for tag in (b"head", b"hhea", b"maxp", b"hmtx", b"cmap"):
        if tag not in tables:
            raise ValueError(f"missing {tag.decode()} table")
    units = _units_per_em(data, tables)
    long_count = _unpack(">H", data, tables[b"hhea"][0] + 34)[0]
    glyph_count = _unpack(">H", data, tables[b"maxp"][0] + 4)[0]
    if long_count == 0:
        raise ValueError("no horizontal metrics")
    hmtx = tables[b"hmtx"][0]

    def advance_units(glyph: int) -> int:
        return _unpack(">H", data, hmtx + 4 * min(glyph, long_count - 1))[0]

    subtable = _cmap_subtable(data, tables[b"cmap"][0])
    pairs = _glyphs_format_12(data, subtable) if _unpack(">H", data, subtable)[0] == 12 \
        else _glyphs_format_4(data, subtable)
    advances = {}
    for code, glyph in pairs:
        if code < 0x20 or 0x7F <= code <= 0x9F or code > 0x10FFFF or 0xD800 <= code <= 0xDFFF:
            continue
        if glyph == 0 or glyph >= glyph_count:
            continue
        advances[chr(code)] = round(advance_units(glyph) / units, 4)
    return units, advances


def advances(ttf: bytes) -> dict[str, float]:
    """Character -> advance in ems, for the font's Unicode cmap from U+0020 up (not U+007F-U+009F)."""
    return _read(ttf)[1]


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: font_metrics.py <font.ttf> <out.json>", file=sys.stderr)
        return 2
    path = pathlib.Path(argv[1])
    data = path.read_bytes()
    units, table = _read(data)
    document = {"family": "DejaVu Sans", "source": path.name, "sha256": hashlib.sha256(data).hexdigest(),
                "units_per_em": units, "advances": table}
    pathlib.Path(argv[2]).write_text(json.dumps(document, sort_keys=True, ensure_ascii=False, indent=0) + "\n",
                                     encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
