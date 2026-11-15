# SPDX-License-Identifier: GPL-3.0-only
"""XLSX and ODS exports write native charts: the inline (bounded) whole-workbook and selection routes, through the
service, read back with the project's own import previews."""
import base64
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from datahorizon.client import LocalService
from datahorizon.interchange import xlsx_preview
from datahorizon.ods import ods_preview
from datahorizon.service import Service

FORMATS = ("xlsx", "ods")
PREVIEWS = {"xlsx": xlsx_preview, "ods": ods_preview}


def fill_data(book, sheet="Data"):
    """Month, Sales and Cost in A1:C13 of ``sheet``."""
    cells = [{"row": 1, "col": 1, "source": "Month"}, {"row": 1, "col": 2, "source": "Sales"}, {"row": 1, "col": 3, "source": "Cost"}]
    for row in range(2, 14):
        cells += [{"row": row, "col": 1, "source": f"M{row - 1}"}, {"row": row, "col": 2, "source": str(row * 10)},
                  {"row": row, "col": 3, "source": str(row * 4)}]
    book.apply("edit", sheet, cells=cells)


def read_back(fmt, result):
    """The whole-workbook import preview of an export's file."""
    return PREVIEWS[fmt]({"file_base64": result["file_base64"], "scope": "workbook"})


def imported_charts(fmt, result):
    return read_back(fmt, result)["workbook_charts"]


def cells_of(fmt, result):
    return [(sheet["name"], sheet["cells"]) for sheet in read_back(fmt, result)["workbook_sheets"]]


def skipped(result):
    """``(detail, chart name, sheet)`` of every chart left out, from the report entries' samples."""
    return sorted((entry["detail"], sample["cell"], sample["sheet"]) for entry in result["report_entries"]
                  if entry["category"] == "chart.skipped" for sample in entry["samples"])


def chart_lines(result):
    return [line for line in result["report"] if line.startswith("Charts")]


def ref(source):
    return (source["sheet"], source["ref"])


class InlineChartExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.service = LocalService()
        cls.client = cls.service.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.service.__exit__(None, None, None)
        cls.temp.cleanup()

    def open_book(self, name):
        book = self.client.open(Path(self.temp.name) / f"{name}.dhwb", create=True)
        self.addCleanup(book.close)
        book.apply("sheet_rename", sheet="Sheet1", name="Data")
        return book

    def data_book(self, name):
        """Data with A1:C13, a clustered column chart (Chart 1, at E2, an empty cell) and a line chart (Chart 2, at B13,
        a cell with a value)."""
        book = self.open_book(name)
        fill_data(book)
        column = book.suggest_chart("Data", r1=1, c1=1, r2=13, c2=3, type="column")["spec"]
        line = book.suggest_chart("Data", r1=1, c1=1, r2=13, c2=3, type="line")["spec"]
        book.create_chart(column, sheet="Data", name="Chart 1", anchor={"row": 2, "col": 5}, offset={"x": 7, "y": 9})
        book.create_chart(line, sheet="Data", name="Chart 2", anchor={"row": 13, "col": 2})
        return book

    def test_whole_workbook_export_writes_range_charts(self):
        book = self.data_book("whole")
        for fmt in FORMATS:
            with self.subTest(fmt):
                result = book.export_spreadsheet(fmt, scope="workbook")
                self.assertIn("Charts: 2 written.", result["report"])
                self.assertFalse(any("native-only" in line and "Charts" in line for line in result["report"]))
                self.assertEqual(skipped(result), [])
                preview = read_back(fmt, result)
                charts = preview["workbook_charts"]
                self.assertEqual([c["name"] for c in charts], ["Chart 1", "Chart 2"])
                self.assertEqual([(c["spec"]["type"], c["spec"]["grouping"]) for c in charts][0], ("column", "clustered"))
                self.assertEqual(charts[1]["spec"]["type"], "line")
                self.assertEqual((charts[0]["anchor"], charts[0]["offset"]), ({"row": 2, "col": 5}, {"x": 7, "y": 9}))
                self.assertEqual(ref(charts[0]["spec"]["categories"]["source"]), (0, "A2:A13"))
                self.assertEqual([ref(item["values"]["source"]) for item in charts[0]["spec"]["series"]], [(0, "B2:B13"), (0, "C2:C13")])
                self.assertNotIn("anchor_absolute", [e["detail"] for e in preview["report_entries"]])

    def test_left_out_charts_are_named(self):
        book = self.data_book("left-out")
        book.create_table("Sales", [dict(name="Month", type="text"), dict(name="Units", type="integer")])
        book.insert_records("Sales", [dict(Month=f"M{n}", Units=n * 10) for n in range(1, 7)])
        book.create_chart(book.suggest_chart(table="Sales", type="bar")["spec"], sheet="Data", name="By table")
        book.save_query(book.preview_query("Totals", "SELECT Month, Units * 2 AS Doubled FROM Sales ORDER BY Month"))
        book.create_chart({"spec_version": 1, "type": "column",
                           "categories": {"source": {"kind": "query", "query": "Totals", "column": "Month"}},
                           "series": [{"name": {"text": "Doubled"},
                                       "values": {"source": {"kind": "query", "query": "Totals", "column": "Doubled"}}}]},
                          sheet="Data", name="By query")
        book.apply("sheet_add", name="Scratch")
        fill_data(book, "Scratch")
        book.create_chart(book.suggest_chart("Scratch", r1=1, c1=1, r2=13, c2=2)["spec"], sheet="Data", name="Broken")
        book.apply("sheet_delete", sheet="Scratch")
        written = {}
        for fmt in FORMATS:
            with self.subTest(fmt):
                result = book.export_spreadsheet(fmt, scope="workbook")
                self.assertIn("Charts: 2 written, 3 left out; each is listed below with its reason.", result["report"])
                self.assertEqual(skipped(result), [("broken_source", "Broken", "Data"), ("query_source", "By query", "Data"),
                                                   ("table_source", "By table", "Data")])
                self.assertEqual([c["name"] for c in imported_charts(fmt, result)], ["Chart 1", "Chart 2"])
                written[fmt] = cells_of(fmt, result)
        for chart in book.charts():
            book.delete_chart(chart["id"])
        for fmt in FORMATS:
            with self.subTest(fmt, charts=False):
                result = book.export_spreadsheet(fmt, scope="workbook")
                self.assertEqual(chart_lines(result), [])
                self.assertEqual(written[fmt], cells_of(fmt, result))

    def test_values_mode_still_writes_charts(self):
        book = self.data_book("values")
        book.apply("edit", "Data", cells=[{"row": 14, "col": 2, "source": "=SUM(B2:B13)"}])
        for fmt in FORMATS:
            with self.subTest(fmt):
                result = book.export_spreadsheet(fmt, scope="workbook", mode="values")
                self.assertIn("Charts: 2 written.", result["report"])
                self.assertEqual([c["name"] for c in imported_charts(fmt, result)], ["Chart 1", "Chart 2"])
                total = [cell for name, cells in cells_of(fmt, result) for cell in cells if (cell["row"], cell["col"]) == (14, 2)]
                self.assertEqual([cell["kind"] for cell in total], ["number"])

    def test_chart_on_an_empty_sheet(self):
        book = self.open_book("empty-sheet")
        fill_data(book)
        book.apply("sheet_add", name="Summary")
        spec = book.suggest_chart("Data", r1=1, c1=1, r2=13, c2=2)["spec"]
        book.create_chart(spec, sheet="Summary", name="Overview", anchor={"row": 4, "col": 3}, offset={"x": 5, "y": 6})
        for fmt in FORMATS:
            with self.subTest(fmt):
                result = book.export_spreadsheet(fmt, scope="workbook")
                self.assertIn("Charts: 1 written.", result["report"])
                preview = read_back(fmt, result)
                self.assertEqual([sheet["name"] for sheet in preview["workbook_sheets"]], ["Data", "Summary"])
                self.assertEqual(preview["workbook_sheets"][1]["cells"], [])
                [chart] = preview["workbook_charts"]
                self.assertEqual((chart["sheet"], chart["name"], chart["anchor"], chart["offset"]),
                                 (1, "Overview", {"row": 4, "col": 3}, {"x": 5, "y": 6}))
                self.assertEqual(ref(chart["spec"]["series"][0]["values"]["source"]), (0, "B2:B13"))
                self.assertNotIn("anchor_absolute", [e["detail"] for e in preview["report_entries"]])

    def test_selection_inside(self):
        book = self.data_book("selection")
        for fmt in FORMATS:
            with self.subTest(fmt):
                result = book.export_spreadsheet(fmt, sheet="Data", r1=1, c1=1, r2=20, c2=8)
                self.assertIn("Charts: 2 written.", result["report"])
                preview = read_back(fmt, result)
                self.assertEqual([sheet["name"] for sheet in preview["workbook_sheets"]], ["Sheet1"])
                charts = preview["workbook_charts"]
                self.assertEqual([(c["name"], c["anchor"]) for c in charts], [("Chart 1", {"row": 2, "col": 5}), ("Chart 2", {"row": 13, "col": 2})])
                self.assertEqual(ref(charts[0]["spec"]["categories"]["source"]), (0, "A2:A13"))
                self.assertEqual(ref(charts[0]["spec"]["series"][1]["values"]["source"]), (0, "C2:C13"))
        with zipfile.ZipFile(io.BytesIO(base64.b64decode(book.export_spreadsheet("xlsx", sheet="Data", r1=1, c1=1, r2=20, c2=8)["file_base64"]))) as archive:
            self.assertIn("Sheet1!$A$2:$A$13", archive.read("xl/charts/chart1.xml").decode())

    def test_selection_cutting_a_source(self):
        book = self.open_book("cut")
        fill_data(book)
        spec = book.suggest_chart("Data", r1=1, c1=1, r2=13, c2=3)["spec"]
        book.create_chart(spec, sheet="Data", name="Wide", anchor={"row": 2, "col": 2})
        book.apply("sheet_add", name="Other")
        book.create_chart(spec, sheet="Other", name="Elsewhere")
        for fmt in FORMATS:
            with self.subTest(fmt):
                result = book.export_spreadsheet(fmt, sheet="Data", r1=1, c1=1, r2=13, c2=2)
                self.assertEqual(skipped(result), [("source", "Wide", "Sheet1")])
                self.assertEqual(chart_lines(result), ["Charts: 0 written, 1 left out; each is listed below with its reason."])
                self.assertEqual(imported_charts(fmt, result), [])
                self.assertEqual(len(read_back(fmt, result)["workbook_sheets"][0]["cells"]), 26)

    def test_selection_at_the_cell_limit_with_large_charts(self):
        book = self.open_book("limit")
        # 10,000 cells sized so that the cells alone come within 0.25 MiB of the read's 4 MiB limit; the stored charts
        # described take more than 1 MiB, so the read would fail if they counted.
        text = "x" * 100
        cells = [{"row": row, "col": col, "source": f"{row}" if col == 1 else text} for row in range(1, 1001) for col in range(1, 11)]
        for start in range(0, len(cells), 2500):
            book.apply("edit", "Data", cells=cells[start:start + 2500])
        series = [{"name": {"source": {"kind": "range", "sheet": "Data", "ref": f"{chr(65 + n % 10)}1"}},
                   "values": {"source": {"kind": "range", "sheet": "Data", "ref": f"A{2 + n}:A{150 + n}"}}} for n in range(64)]
        spec = {"spec_version": 1, "type": "line", "title": {"text": "T" * 255},
                "categories": {"source": {"kind": "range", "sheet": "Data", "ref": "A2:A150"}}, "series": series}
        for number in range(1, 65):
            book.create_chart(spec, sheet="Data", name=f"Chart {number}", anchor={"row": number, "col": 2})
        for fmt in FORMATS:
            with self.subTest(fmt):
                result = book.export_spreadsheet(fmt, sheet="Data", r1=1, c1=1, r2=1000, c2=10, mode="values")
                self.assertIn("Charts: 64 written.", result["report"])
                preview = read_back(fmt, result)
                self.assertEqual(len(preview["workbook_sheets"][0]["cells"]), 10_000)
                self.assertEqual(len(preview["workbook_charts"]), 64)
                self.assertEqual({len(c["spec"]["series"]) for c in preview["workbook_charts"]}, {64})

    def test_workbook_without_charts_reports_nothing_about_charts(self):
        book = self.open_book("plain")
        fill_data(book)
        for fmt in FORMATS:
            with self.subTest(fmt):
                for scope in (dict(scope="workbook"), dict(sheet="Data", r1=1, c1=1, r2=13, c2=3)):
                    result = book.export_spreadsheet(fmt, **scope)
                    self.assertEqual([line for line in result["report"] if "Chart" in line], [])
                    self.assertEqual([e for e in result["report_entries"] if e["category"].startswith("chart.")], [])
                    with zipfile.ZipFile(io.BytesIO(base64.b64decode(result["file_base64"]))) as archive:
                        self.assertEqual([name for name in archive.namelist() if "chart" in name.lower() or "drawing" in name or "Object" in name], [])

    def test_a_chart_writer_failure_does_not_fail_the_export(self):
        # The writer is patched in this process, so the export runs in an in-process service.
        self.data_book("failure").close()
        server = self.enterContext(Service(staging_root=Path(self.temp.name) / "staging"))
        base = "/v1/workbooks/" + server.dispatch("POST", "/v1/open", {"path": str(Path(self.temp.name) / "failure.dhwb")})["id"]
        writers = {"xlsx": "datahorizon.xlsx_charts.chart_part", "ods": "datahorizon.ods_charts.chart_document"}
        for fmt in FORMATS:
            with self.subTest(fmt), patch(writers[fmt], side_effect=ValueError("x")):
                result = server.dispatch("POST", f"{base}/export/{fmt}", {"scope": "workbook"})
                self.assertEqual([e["detail"] for e in result["report_entries"] if e["category"] == "chart.skipped"], ["unwritable"])
                self.assertEqual(skipped(result), [("unwritable", "Chart 1", "Data"), ("unwritable", "Chart 2", "Data")])
                self.assertIn("Charts: 0 written, 2 left out; each is listed below with its reason.", result["report"])
                self.assertEqual(imported_charts(fmt, result), [])
                self.assertEqual(len(read_back(fmt, result)["workbook_sheets"][0]["cells"]), 39)


if __name__ == "__main__":
    unittest.main()
