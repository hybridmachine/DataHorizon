# SPDX-License-Identifier: GPL-3.0-only
"""Write a deterministic large XLSX workbook for capacity measurement, with the standard library only.

The package is streamed: each worksheet is written to the archive row by row and no sheet is built in memory.
Long shared strings are spooled to a temporary file while the sheets are written and copied into
xl/sharedStrings.xml afterwards; the short-text vocabulary is the only text held.

Content mix (per stored cell, from one seeded random sequence; column A of every row is a number that the
formulas refer to, so numbers come out slightly above their weight):
  about 60% numbers (integers and decimals),
        25% short text (one of 2,000 vocabulary entries, shared like Excel shares repeated text),
         5% long text (200-2,000 characters, unique; --long-text-share),
         5% dates (serials with a yyyy-mm-dd number format),
         5% formulas (--formula-share) with cached values; about one in five uses SQRT, which DataHorizon lacks.
Styles: 330 cell formats (fonts, solid fills, borders and number formats), all distinct when mapped, under the
1,024 distinct-style export limit. Cells are spread over --sheets sheets of 20 columns.
"""
import argparse
import json
import math
import os
from pathlib import Path
import random
import tempfile
import zipfile

MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
RELS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE_RELS = "http://schemas.openxmlformats.org/package/2006/relationships"
COLUMNS = 20
VOCABULARY = 2000
FLUSH = 65536
SEED = 20261111
SYLLABLES = ("ka", "lo", "mi", "ne", "ru", "ta", "vo", "si", "pe", "da", "zu", "fa", "go", "hi", "jo", "be")
COLORS = ("FF000000", "FF1F4E79", "FF833C0B", "FF375623", "FF7030A0", "FFC00000")
FILLS = ("FFFFF2CC", "FFDDEBF7", "FFE2EFDA", "FFFCE4D6", "FFEDEDED", "FFD9E1F2", "FFFFE699", "FFC6E0B4")
NUMBER_FORMATS = ("0", "2", "4", "165")  # General, 0.00, #,##0.00 and the custom 0.000
DATE_FORMAT = "164"
STYLE_COUNT, DATE_STYLE_COUNT = 300, 30


def column_name(col):
    name = ""
    while col:
        col, rest = divmod(col-1, 26)
        name = chr(65+rest)+name
    return name


def escape(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def vocabulary(rng):
    words = set()
    while len(words) < VOCABULARY:
        words.add("".join(rng.choice(SYLLABLES) for _ in range(rng.randint(2, 4)))+" "+str(rng.randint(1, 999)))
    return sorted(words)


def long_text(rng, words):
    target, parts, size = rng.randint(200, 2000), [], 0
    while size < target:
        word = rng.choice(words)
        parts.append(word); size += len(word)+1
    return " ".join(parts)[:target]


def styles(rng):
    """The stylesheet XML and the xf indexes for ordinary cells and for dates."""
    fonts = ['<font><sz val="11"/><name val="Calibri"/></font>']
    for bold in (False, True):
        for italic in (False, True):
            for color in COLORS:
                fonts.append("<font>"+("<b/>" if bold else "")+("<i/>" if italic else "")
                             +f'<sz val="11"/><color rgb="{color}"/><name val="Calibri"/></font>')
    fills = ['<fill><patternFill patternType="none"/></fill>', '<fill><patternFill patternType="gray125"/></fill>']
    fills += [f'<fill><patternFill patternType="solid"><fgColor rgb="{rgb}"/></patternFill></fill>' for rgb in FILLS]
    borders = ["<border><left/><right/><top/><bottom/><diagonal/></border>",
               "<border>"+"".join(f'<{side} style="thin"><color rgb="FF000000"/></{side}>' for side in ("left", "right", "top", "bottom"))
               +"<diagonal/></border>"]
    fill_ids = [0]+list(range(2, len(fills)))
    combos = [(font, fill, fmt, border) for font in range(1, len(fonts)) for fill in fill_ids
              for fmt in NUMBER_FORMATS for border in (0, 1)]
    rng.shuffle(combos)
    dates = [(font, fill, DATE_FORMAT, 0) for font in range(1, len(fonts)) for fill in fill_ids]
    rng.shuffle(dates)
    chosen = combos[:STYLE_COUNT]+dates[:DATE_STYLE_COUNT]
    xfs = ['<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>']
    for font, fill, fmt, border in chosen:
        xfs.append(f'<xf numFmtId="{fmt}" fontId="{font}" fillId="{fill}" borderId="{border}" xfId="0"'
                   ' applyNumberFormat="1" applyFont="1" applyFill="1" applyBorder="1"/>')
    xml = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<styleSheet xmlns="{MAIN}">'
           '<numFmts count="2"><numFmt numFmtId="164" formatCode="yyyy-mm-dd"/><numFmt numFmtId="165" formatCode="0.000"/></numFmts>'
           f'<fonts count="{len(fonts)}">{"".join(fonts)}</fonts><fills count="{len(fills)}">{"".join(fills)}</fills>'
           f'<borders count="{len(borders)}">{"".join(borders)}</borders>'
           '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
           f'<cellXfs count="{len(xfs)}">{"".join(xfs)}</cellXfs>'
           '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>')
    return xml, list(range(1, STYLE_COUNT+1)), list(range(STYLE_COUNT+1, STYLE_COUNT+DATE_STYLE_COUNT+1))


def number(rng):
    return str(rng.randint(1, 100_000)) if rng.random() < .4 else repr(round(rng.uniform(1, 1000), rng.randint(1, 4)))


def formula(rng, row, anchor):
    """(formula text, cell type attribute, cached value, uses an unsupported function)."""
    a = float(anchor)
    if rng.random() < .2: return f"SQRT(A{row})", "", repr(math.sqrt(a)), True
    pick = rng.randrange(4)
    if pick == 0: return f"A{row}*2", "", repr(a*2), False
    if pick == 1: return f"ROUND(A{row}/3,2)", "", repr(round(a/3, 2)), False
    if pick == 2: return f'IF(A{row}&gt;500,"high","low")', ' t="str"', "high" if a > 500 else "low", False
    return f"SUM(A{row},{row})", "", repr(a+row), False


def split(cells, sheets):
    base, extra = divmod(cells, sheets)
    return [base+(index < extra) for index in range(sheets)]


def generate(out, cells, sheets=3, formula_share=.05, strings="shared", seed=SEED, long_text_share=.05):
    """Write the workbook to `out`; return a description of what was written."""
    if (cells < 1 or sheets < 1 or sheets > 255 or not 0 <= formula_share <= .5 or not 0 <= long_text_share <= .3
            or strings not in ("shared", "inline")):
        raise ValueError("Positive cells, 1-255 sheets, a formula share of 0-0.5, a long-text share of 0-0.3 "
                         "and shared or inline strings are required")
    rng = random.Random(seed)
    words = vocabulary(rng)
    stylesheet, plain, dated = styles(rng)
    weights = (("formula", formula_share), ("date", .05), ("long", long_text_share), ("short", .25))
    counts = dict.fromkeys(("number", "short", "long", "date", "formula", "unsupported_formula"), 0)
    references = long_index = 0
    out = Path(out)
    with tempfile.TemporaryFile(dir=out.parent) as spool, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        def text_cell(ref, style, text, shared_index):
            nonlocal references
            references += 1
            if strings == "inline":
                return f'<c r="{ref}"{style} t="inlineStr"><is><t>{escape(text)}</t></is></c>'
            return f'<c r="{ref}"{style} t="s"><v>{shared_index}</v></c>'

        for sheet, total in enumerate(split(cells, sheets), 1):
            with archive.open(f"xl/worksheets/sheet{sheet}.xml", "w") as part:
                pending, size = [f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<worksheet xmlns="{MAIN}"><sheetData>'], 0
                written, row = 0, 0
                while written < total:
                    row += 1
                    pending.append(f'<row r="{row}">')
                    anchor = None
                    for col in range(1, min(COLUMNS, total-written)+1):
                        ref = column_name(col)+str(row)
                        style = f' s="{rng.choice(plain)}"' if rng.random() < .7 else ""
                        draw, kind = rng.random(), "number"
                        if col > 1:
                            for name, weight in weights:
                                if draw < weight: kind = name; break
                                draw -= weight
                        if kind == "number":
                            value = number(rng)
                            if col == 1: anchor = value
                            cell = f'<c r="{ref}"{style}><v>{value}</v></c>'
                        elif kind == "short":
                            index = rng.randrange(VOCABULARY)
                            cell = text_cell(ref, style, words[index], index)
                        elif kind == "long":
                            text = long_text(rng, words)
                            cell = text_cell(ref, style, text, VOCABULARY+long_index)
                            if strings == "shared": spool.write(f"<si><t>{escape(text)}</t></si>".encode())
                            long_index += 1
                        elif kind == "date":
                            cell = f'<c r="{ref}" s="{rng.choice(dated)}"><v>{rng.randint(36526, 47483)}</v></c>'
                        else:
                            source, typed, cached, unsupported = formula(rng, row, anchor)
                            counts["unsupported_formula"] += unsupported
                            cell = f'<c r="{ref}"{style}{typed}><f>{source}</f><v>{cached}</v></c>'
                        counts[kind] += 1
                        pending.append(cell); size += len(cell)
                        written += 1
                    pending.append("</row>")
                    if size >= FLUSH:
                        part.write("".join(pending).encode()); pending, size = [], 0
                pending.append("</sheetData></worksheet>")
                part.write("".join(pending).encode())
        names = [f"Data{index}" for index in range(1, sheets+1)]
        if strings == "shared":
            with archive.open("xl/sharedStrings.xml", "w") as part:
                part.write((f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<sst xmlns="{MAIN}" '
                            f'count="{references}" uniqueCount="{VOCABULARY+long_index}">').encode())
                part.write("".join(f"<si><t>{escape(word)}</t></si>" for word in words).encode())
                spool.seek(0)
                while chunk := spool.read(1024*1024): part.write(chunk)
                part.write(b"</sst>")
        archive.writestr("xl/styles.xml", stylesheet)
        sheet_list = "".join(f'<sheet name="{name}" sheetId="{index}" r:id="rId{index}"/>' for index, name in enumerate(names, 1))
        archive.writestr("xl/workbook.xml", f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<workbook xmlns="{MAIN}" '
                         f'xmlns:r="{RELS}"><sheets>{sheet_list}</sheets></workbook>')
        relations = [(f"rId{index}", "worksheet", f"worksheets/sheet{index}.xml") for index in range(1, sheets+1)]
        relations.append((f"rId{sheets+1}", "styles", "styles.xml"))
        if strings == "shared": relations.append((f"rId{sheets+2}", "sharedStrings", "sharedStrings.xml"))
        archive.writestr("xl/_rels/workbook.xml.rels", f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Relationships xmlns="{PACKAGE_RELS}">'
                         +"".join(f'<Relationship Id="{ident}" Type="{RELS}/{kind}" Target="{target}"/>' for ident, kind, target in relations)
                         +"</Relationships>")
        archive.writestr("_rels/.rels", f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Relationships xmlns="{PACKAGE_RELS}">'
                         f'<Relationship Id="rId1" Type="{RELS}/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        content = "application/vnd.openxmlformats-officedocument.spreadsheetml"
        overrides = [("/xl/workbook.xml", content+".sheet.main+xml"), ("/xl/styles.xml", content+".styles+xml")]
        overrides += [(f"/xl/worksheets/sheet{index}.xml", content+".worksheet+xml") for index in range(1, sheets+1)]
        if strings == "shared": overrides.append(("/xl/sharedStrings.xml", content+".sharedStrings+xml"))
        archive.writestr("[Content_Types].xml", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                         '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                         '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                         '<Default Extension="xml" ContentType="application/xml"/>'
                         +"".join(f'<Override PartName="{name}" ContentType="{kind}"/>' for name, kind in overrides)+"</Types>")
    with zipfile.ZipFile(out) as archive:
        expanded = sum(item.file_size for item in archive.infolist())
    return {"cells": cells, "sheets": sheets, "columns": COLUMNS, "strings": strings, "seed": seed, "formula_share": formula_share,
            "long_text_share": long_text_share,
            "kinds": counts, "cell_formats": STYLE_COUNT+DATE_STYLE_COUNT+1, "package_bytes": out.stat().st_size,
            "expanded_bytes": expanded}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cells", type=int, required=True)
    parser.add_argument("--sheets", type=int, default=3)
    parser.add_argument("--formula-share", type=float, default=.05)
    parser.add_argument("--long-text-share", type=float, default=.05)
    parser.add_argument("--strings", choices=("shared", "inline"), default="shared")
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    try: print(json.dumps(generate(args.out, args.cells, args.sheets, args.formula_share, args.strings, args.seed, args.long_text_share), indent=2))
    except ValueError as error: parser.error(str(error))


if __name__ == "__main__":
    main()
