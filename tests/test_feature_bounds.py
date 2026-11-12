# SPDX-License-Identifier: GPL-3.0-only
"""Every accumulator of the feature scanners and package surveys stays bounded on untrusted input.

Each crafted package is written as a stream into the ZIP (never built whole in memory) and read by the streaming
reader in a fresh process, which reports the outcome, the peak traced allocation and its resident-memory growth.
An input beyond a bound is either rejected with LIMIT or imported with a saturated ("at least") count."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

from datahorizon import ods_features, xlsx_features, xlsx_stream
from datahorizon.conversion_report import ConversionReport
from datahorizon.ods import MIME
from datahorizon.engine import EngineError
from datahorizon.interchange import xlsx_preview
from datahorizon.ods import ods_preview

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
import ods_builder  # noqa: E402
import xlsx_builder  # noqa: E402

MAIN = xlsx_builder.TRANSITIONAL["main"]
REL = xlsx_builder.TRANSITIONAL["rel"]
PKG = xlsx_builder.TRANSITIONAL["pkg"]
TRACED_LIMIT = 64*1024*1024
RSS_LIMIT = 192*1024*1024
SECONDS = 20

# Runs one streaming reader on argv[2] (format argv[1]) and prints its outcome and memory as JSON.
PROBE = r"""
import json, sys, time, tracemalloc
try:
    import resource
except ImportError:
    resource = None
from datahorizon import ods_stream, xlsx_stream
from datahorizon.engine import EngineError

def rss():
    if resource is None: return None
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return value if sys.platform == "darwin" else value*1024

class Strings:
    def put(self, index, text): pass
    def get(self, index): return ""

class Sink:
    strings = Strings()
    def sheet(self, name): pass
    def cells(self, batch): pass

sink = Sink()
reader = xlsx_stream.read_workbook if sys.argv[1] == "xlsx" else ods_stream.read_workbook
before = rss()
started = time.monotonic()
tracemalloc.start()
try:
    report = reader(sys.argv[2], {"mode": "formulas"}, sink, lambda completed, phase: None)
    outcome = {"entries": report.entries()}
except EngineError as error:
    outcome = {"error": error.body}
peak = tracemalloc.get_traced_memory()[1]
tracemalloc.stop()
after = rss()
outcome.update(peak=peak, seconds=time.monotonic()-started, rss_growth=None if before is None else after-before)
print(json.dumps(outcome))
"""


def probe(fmt, path):
    environment = {**os.environ, "PYTHONPATH": str(TESTS.parent/"service")}
    done = subprocess.run([sys.executable, "-c", PROBE, fmt, str(path)], capture_output=True, text=True, env=environment, timeout=120)
    if done.returncode: raise AssertionError(done.stderr)
    return json.loads(done.stdout)


def write_xlsx(path, worksheet, *, sheets_xml=None, workbook_rels="", sheet_rels=None, extra_parts=None):
    """An XLSX package at `path` whose first worksheet part is produced by worksheet(write), streamed into the ZIP."""
    workbook = (f'<workbook xmlns="{MAIN}" xmlns:r="{REL}"><sheets>'
                + (sheets_xml if sheets_xml is not None else '<sheet name="Sheet1" sheetId="1" r:id="rId1"/>')
                + "</sheets></workbook>")
    rels = (f'<Relationships xmlns="{PKG}"><Relationship Id="rId1" Type="{REL}/worksheet" Target="worksheets/sheet1.xml"/>'
            f'{workbook_rels}</Relationships>')
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", rels)
        with archive.open("xl/worksheets/sheet1.xml", "w") as part:
            pending = []
            def write(text):
                pending.append(text)
                if len(pending) >= 4096:
                    part.write("".join(pending).encode()); pending.clear()
            worksheet(write)
            part.write("".join(pending).encode())
        if sheet_rels is not None:
            with archive.open("xl/worksheets/_rels/sheet1.xml.rels", "w") as part:
                pending = []
                def write_rels(text):
                    pending.append(text)
                    if len(pending) >= 4096:
                        part.write("".join(pending).encode()); pending.clear()
                sheet_rels(write_rels)
                part.write("".join(pending).encode())
        for name, data in (extra_parts or {}).items(): archive.writestr(name, data)


def write_ods(path, body):
    """An ODS package at `path` whose content.xml is produced by body(write), streamed into the ZIP."""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", MIME, compress_type=zipfile.ZIP_STORED)
        with archive.open("content.xml", "w") as part:
            pending = []
            def write(text):
                pending.append(text)
                if len(pending) >= 4096:
                    part.write("".join(pending).encode()); pending.clear()
            write(f'<office:document-content {ods_builder.NAMESPACES}>')
            body(write)
            write('</office:document-content>')
            part.write("".join(pending).encode())


def repeat(write, text, count):
    block = text*1000
    for _ in range(count//1000): write(block)
    write(text*(count % 1000))


def worksheet(inner_before="", cells="", inner_after=""):
    """A worksheet writer: `inner_before` and `inner_after` are writers (or text) around <sheetData>."""
    def produce(write):
        write(f'<worksheet xmlns="{MAIN}" xmlns:r="{REL}" xmlns:x14="http://schemas.microsoft.com/office/spreadsheetml/2009/9/main">')
        (inner_before(write) if callable(inner_before) else write(inner_before))
        write("<sheetData>"); (cells(write) if callable(cells) else write(cells)); write("</sheetData>")
        (inner_after(write) if callable(inner_after) else write(inner_after))
        write("</worksheet>")
    return produce


class BoundedScannerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.temp = Path(temp.name)

    def measured(self, fmt, path):
        result = probe(fmt, path)
        self.assertLess(result["peak"], TRACED_LIMIT, result)
        if result["rss_growth"] is not None: self.assertLess(result["rss_growth"], RSS_LIMIT, result)
        self.assertLess(result["seconds"], SECONDS, result)
        return result

    def limited(self, fmt, path, text):
        result = self.measured(fmt, path)
        self.assertEqual(result.get("error", {}).get("code"), "LIMIT", {k: v for k, v in result.items() if k != "entries"})
        self.assertIn(text, result["error"]["message"])
        return result

    def entry(self, result, category, detail):
        self.assertNotIn("error", result)
        return next(e for e in result["entries"] if (e["category"], e["detail"]) == (category, detail))

    # --- XLSX worksheet ------------------------------------------------------------------------------------------
    def test_two_million_column_elements_are_rejected_without_holding_them(self):
        # The reproduced input at reduced scale: 2,000,000 <col> elements, inside every package limit.
        path = self.temp/"cols.xlsx"
        write_xlsx(path, worksheet(lambda write: (write("<cols>"), repeat(write, '<col min="1" max="1" width="1"/>', 2_000_000), write("</cols>"))))
        self.limited("xlsx", path, "16,384")

    def test_column_widths_are_aggregated_and_still_counted(self):
        # At the limit every element is kept as a count by width, and the departures from the default are reported.
        cols = "".join(f'<col min="{i}" max="{i}" width="{9 if i % 2 else 10+i/1000}"/>' for i in range(1, 16385))
        path = self.temp/"widths.xlsx"
        write_xlsx(path, worksheet(f"<cols>{cols}</cols>"))
        result = self.measured("xlsx", path)
        self.assertEqual(self.entry(result, "sheet.feature", "column_widths")["count"], 8192)

    def test_more_rows_than_the_grid_are_rejected(self):
        path = self.temp/"rows.xlsx"
        write_xlsx(path, worksheet(cells=lambda write: repeat(write, "<row/>", 1_048_577)))
        self.limited("xlsx", path, "1,048,576")

    def test_more_merged_ranges_hyperlinks_or_validations_than_cells_are_rejected(self):
        for name, container, item in (("merged", "mergeCells", '<mergeCell ref="A1:B2"/>'),
                                      ("links", "hyperlinks", '<hyperlink ref="A1" display="x"/>'),
                                      ("rules", "dataValidations", '<dataValidation sqref="A1"/>')):
            with self.subTest(name=name):
                path = self.temp/f"{name}.xlsx"
                write_xlsx(path, worksheet(inner_after=lambda write, c=container, i=item: (
                    write(f"<{c}>"), repeat(write, i, xlsx_stream.LARGE_CELLS+1), write(f"</{c}>"))))
                self.limited("xlsx", path, "1,000,000")

    def test_conditional_rule_ids_saturate_and_the_count_is_a_lower_bound(self):
        rules = xlsx_features.MAX_DISTINCT+10
        def after(write):
            write('<conditionalFormatting sqref="A1">')
            for index in range(rules):
                write(f'<cfRule type="dataBar" priority="{index+1}"><extLst><ext uri="u"><x14:id>{{{index:08X}-0000-0000-0000-000000000000}}</x14:id></ext></extLst></cfRule>')
            write("</conditionalFormatting><extLst>")
            # One <ext> per thousand twins keeps each record under the 1-MiB record bound.
            for start in range(0, rules, 1000):
                write('<ext uri="x"><x14:conditionalFormattings>')
                for index in range(start, min(start+1000, rules)):
                    write(f'<x14:conditionalFormatting><x14:cfRule type="dataBar" id="{{{index:08X}-0000-0000-0000-000000000000}}"/></x14:conditionalFormatting>')
                write("</x14:conditionalFormattings></ext>")
            write("</extLst>")
        path = self.temp/"ids.xlsx"
        write_xlsx(path, worksheet(inner_after=after))
        entry = self.entry(self.measured("xlsx", path), "sheet.feature", "conditional_formatting")
        # Every base rule is counted; the twins of untracked ids are not counted again, so the count is exact here.
        self.assertEqual(entry["count"], rules)
        self.assertIs(entry.get("at_least"), True)

    def test_a_relationship_part_with_many_distinct_types_is_read_in_bounded_memory(self):
        def rels(write):
            write(f'<Relationships xmlns="{PKG}">')
            for index in range(300_000):
                write(f'<Relationship Id="r{index}" Type="{REL}/kind{index}" Target="../part{index}.xml"/>')
            write("</Relationships>")
        path = self.temp/"rels.xlsx"
        write_xlsx(path, worksheet(cells='<row r="1"><c r="A1"><v>1</v></c></row>'), sheet_rels=rels)
        self.assertNotIn("error", self.measured("xlsx", path))

    def test_shared_formula_groups_are_bounded_per_sheet(self):
        count = xlsx_stream.SHARED_GROUPS+1
        def cells(write):
            for index in range(count):
                row = index+1
                write(f'<row r="{row}"><c r="A{row}"><f t="shared" si="{index}" ref="A{row}:B{row}">C{row}</f></c></row>')
        path = self.temp/"shared.xlsx"
        write_xlsx(path, worksheet(cells=cells))
        self.limited("xlsx", path, "shared formula")

    def test_more_workbook_sheets_than_package_parts_are_rejected(self):
        sheets = "".join(f'<sheet name="C{index}" sheetId="{index}" r:id="rChart"/>' for index in range(1, xlsx_stream.LARGE_PARTS+2))
        chart = f'<Relationship Id="rChart" Type="{REL}/chartsheet" Target="chartsheets/sheet1.xml"/>'
        path = self.temp/"sheets.xlsx"
        write_xlsx(path, worksheet(), sheets_xml='<sheet name="Sheet1" sheetId="1" r:id="rId1"/>'+sheets, workbook_rels=chart)
        self.limited("xlsx", path, "4,096")

    # --- ODS -----------------------------------------------------------------------------------------------------
    def test_half_a_million_distinct_validation_names_saturate(self):
        # The reproduced input at reduced scale: empty cells, each naming its own validation.
        def body(write):
            write('<office:body><office:spreadsheet><table:table table:name="S">')
            for start in range(0, 500_000, 1000):
                write("<table:table-row>"+"".join(f'<table:table-cell table:content-validation-name="v{index}"/>'
                                                   for index in range(start, start+1000))+"</table:table-row>")
            write("</table:table></office:spreadsheet></office:body>")
        path = self.temp/"validations.ods"
        write_ods(path, body)
        entry = self.entry(self.measured("ods", path), "sheet.feature", "data_validation")
        self.assertEqual(entry["count"], ods_features.MAX_DISTINCT)
        self.assertIs(entry.get("at_least"), True)

    def test_layout_styles_are_bounded(self):
        def body(write):
            write("<office:automatic-styles>")
            for index in range(70_000):
                write(f'<style:style style:name="c{index}" style:family="table-column"/>')
            write('</office:automatic-styles><office:body><office:spreadsheet><table:table table:name="S"/></office:spreadsheet></office:body>')
        path = self.temp/"layout.ods"
        write_ods(path, body)
        self.limited("ods", path, "65,536")

    def test_filtered_ranges_naming_many_sheets_are_bounded(self):
        def body(write):
            write('<office:body><office:spreadsheet><table:table table:name="S"/><table:database-ranges>')
            for index in range(200_000):
                write(f'<table:database-range table:name="d{index}" table:target-range-address="X{index}.A1:X{index}.B2" table:display-filter-buttons="true"/>')
            write("</table:database-ranges></office:spreadsheet></office:body>")
        path = self.temp/"filters.ods"
        write_ods(path, body)
        entry = self.entry(self.measured("ods", path), "sheet.feature", "auto_filter")
        self.assertEqual(entry["count"], 200_000)

    def test_table_names_are_bounded_by_the_sheet_limit_before_they_are_kept(self):
        def body(write):
            write("<office:body><office:spreadsheet>")
            for index in range(300_000): write(f'<table:table table:name="T{index}"/>')
            write("</office:spreadsheet></office:body>")
        path = self.temp/"tables.ods"
        write_ods(path, body)
        self.limited("ods", path, "255")

    def test_ods_merged_cells_beyond_the_cell_limit_are_rejected(self):
        def body(write):
            write('<office:body><office:spreadsheet><table:table table:name="S"><table:table-row>'
                  '<table:table-cell table:number-columns-spanned="2" table:number-columns-repeated="1000001"/>'
                  "</table:table-row></table:table></office:spreadsheet></office:body>")
        path = self.temp/"merged.ods"
        write_ods(path, body)
        self.limited("ods", path, "1,000,000")


class SaturationAgreementTests(unittest.TestCase):
    """The tree reader feeds the same scanners, so a saturated count reads the same from both readers."""

    def test_both_ods_readers_report_the_same_saturated_validation_count(self):
        import base64
        names = ods_features.MAX_DISTINCT+5
        rows = "".join("<table:table-row>"+"".join(f'<table:table-cell table:content-validation-name="v{index}"/>'
                                                   for index in range(start, min(start+1000, names)))+"</table:table-row>"
                       for start in range(0, names, 1000))
        data = ods_builder.workbook_bytes({"S": rows+'<table:table-row><table:table-cell office:value-type="float" office:value="1"/></table:table-row>'})
        tree = ods_preview({"file_base64": base64.b64encode(data).decode(), "scope": "workbook"})
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/"v.ods"; path.write_bytes(data)
            from datahorizon import ods_stream
            class Sink:
                class strings:
                    put = staticmethod(lambda index, text: None)
                def sheet(self, name): pass
                def cells(self, batch): pass
            streamed = ods_stream.read_workbook(path, {"scope": "workbook"}, Sink(), lambda completed, phase: None).entries()
        self.assertEqual(tree["report_entries"], streamed)
        entry = next(e for e in streamed if e["detail"] == "data_validation")
        self.assertEqual((entry["count"], entry["at_least"]), (ods_features.MAX_DISTINCT, True))
        self.assertIn("at least", next(line for line in tree["report"] if "data_validation" in line))

    def test_a_saturated_entry_merges_and_reads_as_a_lower_bound(self):
        first, second = ConversionReport(), ConversionReport()
        first.add("sheet.feature", "omitted", "data_validation", sheet="S", count=3, at_least=True)
        second.add("sheet.feature", "omitted", "data_validation", sheet="T", count=2)
        second.merge(first)
        (entry,) = second.entries()
        self.assertEqual((entry["count"], entry["at_least"]), (5, True))
        self.assertTrue(second.lines()[0].startswith("at least 5 items"))
        plain = ConversionReport(); plain.add("x", "omitted", "d")
        self.assertNotIn("at_least", plain.entries()[0])

    def test_feature_item_limit_is_the_large_cell_limit(self):
        self.assertEqual(xlsx_features.MAX_FEATURE_ITEMS, xlsx_stream.LARGE_CELLS)

    def test_tree_reader_rejects_too_many_column_elements_like_the_streaming_reader(self):
        import base64
        cols = '<col min="1" max="1" width="1"/>'*(xlsx_features.MAX_COLUMN_ELEMENTS+1)
        data = xlsx_builder.workbook_bytes({"S": ""}, sheet_before={"S": f"<cols>{cols}</cols>"})
        with self.assertRaises(EngineError) as raised:
            xlsx_preview({"file_base64": base64.b64encode(data).decode(), "scope": "workbook"})
        self.assertEqual(raised.exception.body["code"], "LIMIT")


if __name__ == "__main__":
    unittest.main()
