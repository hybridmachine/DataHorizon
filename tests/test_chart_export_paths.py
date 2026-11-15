# SPDX-License-Identifier: GPL-3.0-only
"""XLSX and ODS exports write native charts: the inline (bounded) whole-workbook and selection routes and the streamed
whole-workbook export job, through the service, read back with the project's own import previews."""
import base64
import io
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid
import zipfile

from datahorizon import service as service_module
from datahorizon.client import LocalService
from datahorizon.engine import EngineError
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


class ChartBooks(unittest.TestCase):
    """A local service for building workbooks with charts."""
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

    def left_out_book(self, name):
        """data_book with three charts no file can hold: a table chart, a query chart and one whose sheet was deleted."""
        book = self.data_book(name)
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
        return book


class InlineChartExportTests(ChartBooks):
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
        book = self.left_out_book("left-out")
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
            self.assertIn("'Sheet1'!$A$2:$A$13", archive.read("xl/charts/chart1.xml").decode())

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

    def test_charts_spread_over_many_sheets_read_back_through_the_bounded_import(self):
        # One chart on each of 25 sheets: the XLSX package holds a drawing, two relationship parts and a chart part
        # per sheet (130 entries), which the bounded import must accept as it accepts the same workbook without charts.
        book = self.open_book("spread")
        for number in range(25):
            name = "Data" if number == 0 else f"S{number}"
            if number:
                book.apply("sheet_add", name=name)
            book.apply("edit", name, cells=[{"row": 1, "col": 1, "source": str(number)}])
            book.create_chart({"spec_version": 1, "type": "column",
                               "series": [{"name": {"text": "n"}, "values": {"source": {"kind": "range", "sheet": name, "ref": "A1"}}}]},
                              sheet=name, name=f"Chart {number}")
        for fmt in FORMATS:
            with self.subTest(fmt):
                result = book.export_spreadsheet(fmt, scope="workbook")
                self.assertIn("Charts: 25 written.", result["report"])
                preview = read_back(fmt, result)
                self.assertEqual(len(preview["workbook_sheets"]), 25)
                self.assertEqual(sorted(c["name"] for c in preview["workbook_charts"]), sorted(f"Chart {n}" for n in range(25)))

    def size_book(self, name, cells, length=65536):
        """``cells`` texts of ``length`` ampersands in column A, and what 64 charts of 64 series (about 1.8 MiB) need."""
        book = self.open_book(name)
        for row in range(1, cells + 1):
            book.apply("edit", "Data", cells=[{"row": row, "col": 1, "source": "&" * length}])
        series = [{"name": {"source": {"kind": "range", "sheet": "Data", "ref": "A1"}},
                   "values": {"source": {"kind": "range", "sheet": "Data", "ref": f"A{1 + n}:A{9 + n}"}}} for n in range(64)]
        return book, {"spec_version": 1, "type": "line", "series": series}

    def assert_leading_charts_kept(self, fmt, result, cells, limit):
        left = skipped(result)
        written = 64 - len(left)
        self.assertTrue(0 < written < 64, written)
        self.assertEqual(left, [("size_limit", f"Chart {number:02}", "Data") for number in range(written + 1, 65)])
        self.assertEqual(chart_lines(result), [f"Charts: {written} written, {64 - written} left out; each is listed below with its reason."])
        with zipfile.ZipFile(io.BytesIO(base64.b64decode(result["file_base64"]))) as archive:
            self.assertLessEqual(sum(item.file_size for item in archive.infolist()), limit)
        preview = read_back(fmt, result)
        self.assertEqual(len(preview["workbook_sheets"][0]["cells"]), cells)
        self.assertEqual([c["name"] for c in preview["workbook_charts"]], [f"Chart {number:02}" for number in range(1, written + 1)])

    def test_ods_charts_that_do_not_fit_the_size_limit_are_left_out_and_the_cells_exported(self):
        # The cells alone come within 0.4 MiB of the 16 MiB expanded limit (an ampersand is written as five bytes and
        # ODS writes a text twice): the export keeps the leading charts that fit and names every other one.
        book, spec = self.size_book("size-ods", 25)
        self.assertEqual(chart_lines(book.export_spreadsheet("ods", scope="workbook")), [])
        for number in range(1, 65):
            book.create_chart(spec, sheet="Data", name=f"Chart {number:02}", anchor={"row": number, "col": 3})
        self.assert_leading_charts_kept("ods", book.export_spreadsheet("ods", scope="workbook"), 25, 16 * 1024 * 1024)

    def test_xlsx_charts_that_do_not_fit_the_size_limit_are_left_out_and_the_cells_exported(self):
        # A read snapshot cannot hold cells that fill 16 MiB of XLSX, so the limit is lowered to 0.4 MiB over what the
        # cells take; the selection route is held to the same rule.
        book, spec = self.size_book("size-xlsx", 4, 32767)
        plain = book.export_spreadsheet("xlsx", scope="workbook")
        with zipfile.ZipFile(io.BytesIO(base64.b64decode(plain["file_base64"]))) as archive:
            limit = sum(item.file_size for item in archive.infolist()) + 400_000
        for number in range(1, 65):
            book.create_chart(spec, sheet="Data", name=f"Chart {number:02}", anchor={"row": number, "col": 3})
        book.close()
        # The limit is patched in this process, so the export runs in an in-process service.
        server = self.enterContext(Service(staging_root=Path(self.temp.name) / "staging-size"))
        base = "/v1/workbooks/" + server.dispatch("POST", "/v1/open", {"path": str(Path(self.temp.name) / "size-xlsx.dhwb")})["id"]
        export = lambda scope: server.dispatch("POST", f"{base}/export/xlsx", scope)
        with patch("datahorizon.interchange.MAX_EXPANDED", limit):
            self.assert_leading_charts_kept("xlsx", export({"scope": "workbook"}), 4, limit)
            selection = export({"sheet": "Data", "r1": 1, "c1": 1, "r2": 80, "c2": 3})
            self.assertEqual({detail for detail, _, _ in skipped(selection)}, {"size_limit"})
            self.assertEqual(len(read_back("xlsx", selection)["workbook_sheets"][0]["cells"]), 4)
        with patch("datahorizon.interchange.MAX_EXPANDED", 1000), self.assertRaises(EngineError) as raised:
            export({"scope": "workbook"})
        self.assertEqual(raised.exception.body["code"], "LIMIT")

    def test_many_table_sourced_charts_do_not_slow_the_export(self):
        # 60 tables of 20 columns and 64 charts of 64 table-sourced series, all within the engine's limits: collecting
        # the charts must not look the table catalog up once per source (that took over 2 s for a 20-cell selection).
        book = self.open_book("table-sources")
        fill_data(book)
        for number in range(60):
            book.create_table(f"T{number}", [dict(name=f"C{column}", type="integer") for column in range(20)])
        book.insert_records("T0", [{f"C{column}": row for column in range(20)} for row in range(5)])
        spec = {"spec_version": 1, "type": "line",
                "series": [{"name": {"text": f"s{n}"}, "values": {"source": {"kind": "table", "table": "T0", "column": f"C{n % 20}"}}}
                           for n in range(64)]}
        for number in range(64):
            book.create_chart(spec, sheet="Data", name=f"T {number}", anchor={"row": number + 1, "col": 5})
        for fmt in FORMATS:
            with self.subTest(fmt, scope="selection"):
                started = time.monotonic()
                result = book.export_spreadsheet(fmt, sheet="Data", r1=1, c1=1, r2=10, c2=2)
                self.assertLess(time.monotonic() - started, 1.0)
                self.assertEqual(chart_lines(result), ["Charts: 0 written, 64 left out; each is listed below with its reason."])
                self.assertEqual(len(read_back(fmt, result)["workbook_sheets"][0]["cells"]), 20)
            with self.subTest(fmt, scope="workbook"):
                started = time.monotonic()
                result = book.export_spreadsheet(fmt, scope="workbook")
                self.assertLess(time.monotonic() - started, 1.0)
                self.assertEqual(chart_lines(result), ["Charts: 0 written, 64 left out; each is listed below with its reason."])
                self.assertEqual({detail for detail, _, _ in skipped(result)}, {"table_source"})
                self.assertEqual(len(read_back(fmt, result)["workbook_sheets"][0]["cells"]), 39)

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

    def test_a_chart_packaging_failure_does_not_fail_the_export(self):
        self.data_book("packaging").close()
        server = self.enterContext(Service(staging_root=Path(self.temp.name) / "staging-packaging"))
        base = "/v1/workbooks/" + server.dispatch("POST", "/v1/open", {"path": str(Path(self.temp.name) / "packaging.dhwb")})["id"]
        for fmt in FORMATS:
            for scope in ({"scope": "workbook"}, {"sheet": "Data", "r1": 1, "c1": 1, "r2": 20, "c2": 8}):
                with self.subTest(fmt, **scope), patch(PACKAGERS[fmt], side_effect=ValueError("x")):
                    result = server.dispatch("POST", f"{base}/export/{fmt}", scope)
                    assert_packaging_failure(self, fmt, result, "Data" if "scope" in scope else "Sheet1")


PACKAGERS = {"xlsx": "datahorizon.xlsx_charts.package", "ods": "datahorizon.ods_charts.package"}


def assert_packaging_failure(case, fmt, result, sheet):
    """Packaging the built charts failed: the export holds every cell and no chart part, and reports each chart."""
    case.assertEqual(skipped(result), [("unwritable", "Chart 1", sheet), ("unwritable", "Chart 2", sheet)])
    case.assertEqual(chart_lines(result), ["Charts: 0 written, 2 left out; each is listed below with its reason."])
    case.assertEqual([e for e in result["report_entries"] if e["category"] == "chart.converted"], [])
    case.assertEqual(imported_charts(fmt, result), [])
    case.assertEqual(len(read_back(fmt, result)["workbook_sheets"][0]["cells"]), 39)
    with zipfile.ZipFile(io.BytesIO(base64.b64decode(result["file_base64"]))) as archive:
        case.assertEqual([name for name in archive.namelist() if "chart" in name.lower() or "drawing" in name or "Object" in name], [])


def chart_parts(fmt, data):
    """``{part name: bytes}`` of the parts that carry an export's charts, with the content types or manifest."""
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        if fmt == "xlsx":
            wanted = [name for name in archive.namelist()
                      if name.startswith(("xl/charts/", "xl/drawings/", "xl/worksheets/_rels/")) or name == "[Content_Types].xml"]
        else:
            wanted = [name for name in archive.namelist() if name.startswith("Object ") or name == "META-INF/manifest.xml"]
        return {name: archive.read(name) for name in wanted}


class StreamedChartExportTests(ChartBooks):
    """The streamed whole-workbook export job, run in an in-process service so that its writers can be patched."""

    def setUp(self):
        self.staging = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.server = self.enterContext(Service(staging_root=self.staging / "staging"))

    def base(self, name):
        """Open the workbook ``name`` (built, then closed, in the local service) in the in-process service."""
        info = self.server.dispatch("POST", "/v1/open", {"path": str(Path(self.temp.name) / f"{name}.dhwb")})
        return f"/v1/workbooks/{info['id']}"

    def start(self, base, fmt):
        job = uuid.uuid4().hex
        self.server.dispatch("POST", f"{base}/export/{fmt}/jobs", {"job_id": job, "mode": "formulas"})
        return job

    def finish(self, job):
        """The job's terminal view, waited for."""
        deadline = time.monotonic() + 120
        view = self.server.dispatch("GET", "/v1/jobs/" + job, {})
        while view["status"] == "running":
            self.assertLess(time.monotonic(), deadline, "job did not finish")
            time.sleep(0.01)
            view = self.server.dispatch("GET", "/v1/jobs/" + job, {})
        return view

    def export_job(self, base, fmt):
        """The completed job's result, with its file as ``file_base64`` for the read-back helpers."""
        job = self.start(base, fmt)
        view = self.finish(job)
        self.assertEqual(view["status"], "completed", view)
        data = self.server.staging.path("export", job).read_bytes()
        return {**view["result"], "file_base64": base64.b64encode(data).decode()}

    def test_streamed_export_writes_charts(self):
        self.data_book("streamed").close()
        base = self.base("streamed")
        for fmt in FORMATS:
            with self.subTest(fmt):
                result = self.export_job(base, fmt)
                self.assertIn("Charts: 2 written.", result["report"])
                self.assertEqual(chart_lines(result), ["Charts: 2 written."])
                self.assertFalse(any("native-only" in line and "Charts" in line for line in result["report"]))
                self.assertEqual(skipped(result), [])
                charts = imported_charts(fmt, result)
                self.assertEqual([c["name"] for c in charts], ["Chart 1", "Chart 2"])
                self.assertEqual([(c["anchor"], c["offset"]) for c in charts],
                                 [({"row": 2, "col": 5}, {"x": 7, "y": 9}), ({"row": 13, "col": 2}, {"x": 0, "y": 0})])
                self.assertEqual(ref(charts[0]["spec"]["categories"]["source"]), (0, "A2:A13"))
                self.assertEqual([ref(item["values"]["source"]) for item in charts[0]["spec"]["series"]], [(0, "B2:B13"), (0, "C2:C13")])
                self.assertEqual(len(read_back(fmt, result)["workbook_sheets"][0]["cells"]), 39)

    def test_streamed_and_inline_exports_hold_the_same_charts(self):
        book = self.data_book("same")
        book.apply("sheet_add", name="Summary")
        book.apply("sheet_add", name="Tail")
        book.apply("edit", "Tail", cells=[{"row": 1, "col": 1, "source": "last"}])
        # A chart on a sheet with no cells, and one anchored past the last stored cell of its row and sheet.
        spec = book.suggest_chart("Data", r1=1, c1=1, r2=13, c2=2)["spec"]
        book.create_chart(spec, sheet="Summary", name="Overview", anchor={"row": 4, "col": 3}, offset={"x": 5, "y": 6})
        book.create_chart(spec, sheet="Data", name="Below", anchor={"row": 20, "col": 1})
        # One anchored before the first stored cell of its sheet (A1, first cell B1), one between two stored cells of a
        # row (B5, between A5 and C5).
        book.apply("sheet_add", name="Gaps")
        book.apply("edit", "Gaps", cells=[{"row": 1, "col": 2, "source": "b1"}, {"row": 5, "col": 1, "source": "a5"},
                                          {"row": 5, "col": 3, "source": "c5"}])
        book.create_chart(spec, sheet="Gaps", name="Before", anchor={"row": 1, "col": 1})
        book.create_chart(spec, sheet="Gaps", name="Between", anchor={"row": 5, "col": 2})
        book.close()
        base = self.base("same")
        for fmt in FORMATS:
            with self.subTest(fmt):
                streamed = self.export_job(base, fmt)
                inline = self.server.dispatch("POST", f"{base}/export/{fmt}", {"scope": "workbook"})
                self.assertEqual(chart_lines(streamed), ["Charts: 6 written."])
                self.assertEqual(chart_lines(streamed), chart_lines(inline))
                left, right = (chart_parts(fmt, base64.b64decode(r["file_base64"])) for r in (streamed, inline))
                self.assertGreater(len([name for name in right if "chart" in name.lower() or "Object" in name]), 3)
                self.assertEqual(sorted(left), sorted(right))
                for name in right:
                    self.assertEqual(left[name], right[name], name)
                if fmt == "xlsx":
                    # Without formulas the two XLSX packages agree byte for byte, worksheets and their drawing included.
                    with zipfile.ZipFile(io.BytesIO(base64.b64decode(streamed["file_base64"]))) as one, \
                            zipfile.ZipFile(io.BytesIO(base64.b64decode(inline["file_base64"]))) as other:
                        self.assertEqual(sorted(one.namelist()), sorted(other.namelist()))
                        for name in other.namelist():
                            self.assertEqual(one.read(name), other.read(name), name)
                self.assertEqual(imported_charts(fmt, streamed), imported_charts(fmt, inline))
                # No stored cell moves for a frame's anchor cell.
                self.assertEqual(cells_of(fmt, streamed), cells_of(fmt, inline))
                for result in (streamed, inline):
                    gaps = dict(cells_of(fmt, result))["Gaps"]
                    self.assertEqual(sorted((cell["row"], cell["col"], cell["source"]) for cell in gaps),
                                     [(1, 2, "b1"), (5, 1, "a5"), (5, 3, "c5")])
                    placed = {c["name"]: c["anchor"] for c in imported_charts(fmt, result)}
                    self.assertEqual((placed["Before"], placed["Between"]), ({"row": 1, "col": 1}, {"row": 5, "col": 2}))

    def test_sixty_four_charts_of_sixty_four_series(self):
        book = self.open_book("many")
        cells = [{"row": 1, "col": col, "source": f"S{col}"} for col in range(1, 11)]
        cells += [{"row": row, "col": 1, "source": str(row)} for row in range(2, 215)]
        book.apply("edit", "Data", cells=cells)
        series = [{"name": {"source": {"kind": "range", "sheet": "Data", "ref": f"{chr(65 + n % 10)}1"}},
                   "values": {"source": {"kind": "range", "sheet": "Data", "ref": f"A{2 + n}:A{150 + n}"}}} for n in range(64)]
        spec = {"spec_version": 1, "type": "line", "title": {"text": "T" * 255},
                "categories": {"source": {"kind": "range", "sheet": "Data", "ref": "A2:A150"}}, "series": series}
        for number in range(1, 65):
            book.create_chart(spec, sheet="Data", name=f"Chart {number}", anchor={"row": number, "col": 2})
        book.close()
        base = self.base("many")
        for fmt in FORMATS:
            with self.subTest(fmt):
                result = self.export_job(base, fmt)
                self.assertEqual(chart_lines(result), ["Charts: 64 written."])
                charts = imported_charts(fmt, result)
                self.assertEqual([c["name"] for c in charts], [f"Chart {n}" for n in range(1, 65)])
                self.assertEqual({len(c["spec"]["series"]) for c in charts}, {64})
                self.assertEqual(len(read_back(fmt, result)["workbook_sheets"][0]["cells"]), 223)

    def test_cancel_while_writing_charts_removes_the_file(self):
        self.data_book("cancel").close()
        base = self.base("cancel")
        writers = {"xlsx": "write_workbook", "ods": "ods_write_workbook"}
        for fmt in FORMATS:
            with self.subTest(fmt):
                reached, proceed, phases = threading.Event(), threading.Event(), []
                real = getattr(service_module, writers[fmt])

                def wrapper(path, sheets, report, checkpoint, **options):
                    def gate(completed, phase):
                        phases.append(phase)
                        if "chart" in phase.lower() and not reached.is_set():
                            reached.set()
                            proceed.wait(30)
                        checkpoint(completed, phase)
                    return real(path, sheets, report, gate, **options)

                with patch.object(service_module, writers[fmt], wrapper):
                    job = self.start(base, fmt)
                    try:
                        self.assertTrue(reached.wait(30), "the writer reached no chart checkpoint")
                        self.server.dispatch("POST", f"/v1/jobs/{job}/cancel", {})
                    finally:
                        proceed.set()
                    view = self.finish(job)
                self.assertEqual((view["status"], view["error"]["code"]), ("cancelled", "CANCELLED"), view)
                # The cancel took effect at the first chart's checkpoint.
                self.assertEqual(len([phase for phase in phases if "chart" in phase.lower()]), 1, phases)
                # Neither the export file nor its snapshot or scratch body is left in staging.
                self.assertEqual([p.name for p in self.server.staging.root.iterdir()], [])
                with self.assertRaises(EngineError) as raised:
                    self.server.staging.path("export", job)
                self.assertEqual(raised.exception.body["code"], "JOB_NOT_FOUND")

    def test_left_out_charts_are_named(self):
        self.left_out_book("left-out-streamed").close()
        base = self.base("left-out-streamed")
        for fmt in FORMATS:
            with self.subTest(fmt):
                result = self.export_job(base, fmt)
                self.assertEqual(chart_lines(result), ["Charts: 2 written, 3 left out; each is listed below with its reason."])
                self.assertEqual(skipped(result), [("broken_source", "Broken", "Data"), ("query_source", "By query", "Data"),
                                                   ("table_source", "By table", "Data")])
                self.assertEqual([c["name"] for c in imported_charts(fmt, result)], ["Chart 1", "Chart 2"])

    def test_a_chart_writer_failure_does_not_fail_the_export(self):
        self.data_book("failure-streamed").close()
        base = self.base("failure-streamed")
        writers = {"xlsx": "datahorizon.xlsx_charts.chart_part", "ods": "datahorizon.ods_charts.chart_document"}
        for fmt in FORMATS:
            with self.subTest(fmt), patch(writers[fmt], side_effect=ValueError("x")):
                result = self.export_job(base, fmt)
                self.assertEqual(skipped(result), [("unwritable", "Chart 1", "Data"), ("unwritable", "Chart 2", "Data")])
                self.assertEqual(chart_lines(result), ["Charts: 0 written, 2 left out; each is listed below with its reason."])
                self.assertEqual(imported_charts(fmt, result), [])
                self.assertEqual(len(read_back(fmt, result)["workbook_sheets"][0]["cells"]), 39)

    def test_a_chart_packaging_failure_does_not_fail_the_export(self):
        self.data_book("packaging-streamed").close()
        base = self.base("packaging-streamed")
        for fmt in FORMATS:
            with self.subTest(fmt), patch(PACKAGERS[fmt], side_effect=ValueError("x")):
                assert_packaging_failure(self, fmt, self.export_job(base, fmt), "Data")

    def test_workbook_without_charts_reports_nothing_about_charts(self):
        book = self.open_book("plain-streamed")
        fill_data(book)
        book.close()
        base = self.base("plain-streamed")
        for fmt in FORMATS:
            with self.subTest(fmt):
                result = self.export_job(base, fmt)
                self.assertEqual([line for line in result["report"] if "Chart" in line], [])
                self.assertEqual([e for e in result["report_entries"] if e["category"].startswith("chart.")], [])
                with zipfile.ZipFile(io.BytesIO(base64.b64decode(result["file_base64"]))) as archive:
                    self.assertEqual([name for name in archive.namelist() if "chart" in name.lower() or "drawing" in name or "Object" in name], [])


if __name__ == "__main__":
    unittest.main()
