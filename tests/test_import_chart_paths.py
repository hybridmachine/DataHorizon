# SPDX-License-Identifier: GPL-3.0-only
"""Whole-workbook imports carry charts: the staged (streaming) and inline (bounded) paths, through the service."""
import base64
from contextlib import closing
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
import uuid
import zipfile
from unittest.mock import patch

from chart_fixtures import (chart_xml, ods_chart_xml, ods_sample_like, ods_with_charts, one_cell, plot_xml, sample_like,
                            xlsx_with_charts)
from ods_builder import styles_document
from datahorizon import import_charts, import_stage, ods_stream, xlsx_stream
from datahorizon.xlsx_charts import MAX_FRAMES
from datahorizon.cli import report_text
from datahorizon.client import LocalService
from datahorizon.conversion_report import ConversionReport
from datahorizon.engine import EngineError
from datahorizon.import_stage import ImportStage, open_readonly
from datahorizon.interchange import workbook_report, xlsx_preview
from datahorizon.ods import ods_preview
from datahorizon.service import Service, openapi

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SAMPLE_SUMMARY = [{"name": "Chart 1", "sheet": "Column Chart", "type": "column"},
                  {"name": "Chart 1", "sheet": "Stacked Column", "type": "column"}]
SALES = [400586, 353095, 804585, 410434, 99699, 25539, 25156]
CHARTSHEET = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/chartsheet"
MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def b64(data):
    return base64.b64encode(data).decode()


def data_rows(count=8):
    return "".join(f'<row r="{row}"><c r="A{row}" t="inlineStr"><is><t>Item {row}</t></is></c><c r="B{row}"><v>{row}</v></c></row>'
                   for row in range(1, count + 1))


def column_chart(sheet="Data", pad=0):
    """A supported column chart; ``pad`` adds that many bytes of ignored extension text to its part."""
    extras = f'<c:extLst><c:ext uri="pad">{"x" * pad}</c:ext></c:extLst>' if pad else ""
    return chart_xml("bar", "col", "clustered", [{"cat": f"{sheet}!$A$2:$A$8", "val": f"{sheet}!$B$2:$B$8"}], extras=extras)


def ods_data_rows(count=8):
    return "".join(f'<table:table-row><table:table-cell office:value-type="string"><text:p>Item {row}</text:p></table:table-cell>'
                   f'<table:table-cell office:value-type="float" office:value="{row}"><text:p>{row}</text:p></table:table-cell></table:table-row>'
                   for row in range(1, count + 1))


def ods_column_chart(sheet="Data", pad=0):
    """A supported column chart object; ``pad`` adds that many bytes of ignored text to its content.xml."""
    return ods_chart_xml("bar", "col", "clustered", [{"cat": f"{sheet}!$A$2:$A$8", "val": f"{sheet}!$B$2:$B$8"}],
                         chart_style=f"<pad>{'x' * pad}</pad>" if pad else "")


def many_charts(count):
    """One worksheet, Data, with ``count`` supported column charts named Chart 1 to Chart ``count``."""
    return xlsx_with_charts({"Data": data_rows()}, [{"sheet": "Data", "name": f"Chart {n}", "xml": column_chart()}
                                                    for n in range(1, count + 1)])


def replace_part(data, name, payload):
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as source, zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as target:
        for item in source.namelist():
            target.writestr(item, payload if item == name else source.read(item))
    return output.getvalue()


def entries(summary, category):
    return [entry for entry in summary["report_entries"] if entry["category"] == category]


class ImportChartPathTests(unittest.TestCase):
    def setUp(self):
        self.temp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.server = self.enterContext(Service(staging_root=self.temp / "staging"))
        self.base = self.open("book")

    # Service helpers.

    def open(self, name):
        info = self.server.dispatch("POST", "/v1/open", {"path": str(self.temp / f"{name}.dhwb"), "create": True})
        return f"/v1/workbooks/{info['id']}"

    def info(self, base=None):
        return self.server.dispatch("GET", base or self.base, {})

    def operate(self, action, base=None, **args):
        base = base or self.base
        return self.server.dispatch("POST", base + "/operations", {"action": action, "operation_id": str(uuid.uuid4()),
                                                                   "expected_state": self.info(base)["state"], **args})

    def wait(self, path):
        deadline = time.monotonic() + 120
        view = self.server.dispatch("GET", path, {})
        while view["status"] == "running":
            self.assertLess(time.monotonic(), deadline, "job did not finish")
            time.sleep(0.01)
            view = self.server.dispatch("GET", path, {})
        self.assertEqual(view["status"], "completed", view)
        return view["result"]

    def prepare(self, data, fmt="xlsx", **request):
        path = self.temp / f"source-{uuid.uuid4().hex}.{fmt}"
        path.write_bytes(data)
        job = uuid.uuid4().hex
        self.server.dispatch("POST", f"/v1/import/{fmt}/jobs", {"job_id": job, "scope": "workbook", "source_path": str(path), **request})
        return self.wait("/v1/jobs/" + job)

    def apply(self, summary, target="replace_empty", base=None):
        return self.operate("workbook_import", base, stage_id=summary["stage_id"], stage_digest=summary["stage_digest"], target=target)

    def charts(self, base=None):
        """The catalog in sheet order, then name and id order (the catalog itself is not ordered by sheet)."""
        base = base or self.base
        order = {sheet["id"]: number for number, sheet in enumerate(self.info(base)["sheets"])}
        found = self.server.dispatch("POST", base + "/charts", {"expected_state": self.info(base)["state"]})["charts"]
        return sorted(found, key=lambda item: (order[item["sheet_id"]], item["name"], item["id"]))

    def sheet_ids(self, base=None):
        return {sheet["id"]: sheet["name"] for sheet in self.info(base)["sheets"]}

    def resolve(self, chart_id, base=None):
        base = base or self.base
        job = uuid.uuid4().hex
        self.server.dispatch("POST", base + "/charts/jobs", {"job_id": job, "expected_state": self.info(base)["state"],
                                                             "chart_id": chart_id, "kind": "resolve"})
        return self.wait(f"{base}/charts/jobs/{job}")["resolved"]

    def engine(self, base=None):
        return self.server.workbooks[(base or self.base).rsplit("/", 1)[1]]

    def format_version(self, base=None):
        engine = self.engine(base)
        with engine.lock:
            return engine.db.execute("SELECT format_version FROM dh_meta").fetchone()[0]

    def stored_cells(self, base=None):
        engine = self.engine(base)
        with engine.lock:
            return engine.db.execute("SELECT sheets.name, row, col, source, kind, style FROM cells JOIN sheets ON sheets.id = cells.sheet_id "
                                     "ORDER BY sheets.name, row, col").fetchall()

    def check_sample(self, base=None):
        found = self.charts(base)
        names = self.sheet_ids(base)
        self.assertEqual([(names[item["sheet_id"]], item["name"]) for item in found],
                         [("Column Chart", "Chart 1"), ("Stacked Column", "Chart 1")])
        first = next(item for item in found if names[item["sheet_id"]] == "Column Chart")
        resolved = self.resolve(first["id"], base)
        self.assertEqual(resolved["series"][0]["values"], SALES)
        self.assertEqual(resolved["categories"][0], "Sports")
        self.assertEqual(self.format_version(base), 8)
        return found

    # Tests.

    def test_staged_xlsx_sample(self):
        summary = self.prepare(sample_like())
        self.assertEqual(summary["charts"], SAMPLE_SUMMARY)
        receipt = self.apply(summary)
        self.assertEqual(receipt["imported"]["charts"], 2)
        self.assertNotIn("charts_left_out", receipt)
        self.check_sample()

    def test_staged_ods_sample(self):
        summary = self.prepare(ods_sample_like(), "ods")
        self.assertEqual(summary["charts"], SAMPLE_SUMMARY)
        receipt = self.apply(summary)
        self.assertEqual(receipt["imported"]["charts"], 2)
        self.check_sample()

    def test_inline_workbook_import(self):
        previews = {"xlsx": xlsx_preview({"file_base64": b64(sample_like()), "scope": "workbook"}),
                    "ods": ods_preview({"file_base64": b64(ods_sample_like()), "scope": "workbook"})}
        for preview in previews.values():
            self.assertEqual(preview["charts"], SAMPLE_SUMMARY)
            self.assertEqual([(item["sheet"], item["name"]) for item in preview["workbook_charts"]], [(0, "Chart 1"), (1, "Chart 1")])
        with LocalService() as client:
            for fmt, preview in previews.items():
                with self.subTest(fmt), client.open(self.temp / f"inline-{fmt}.dhwb", create=True) as book:
                    receipt = book.import_prepared(preview)
                    self.assertEqual(receipt["imported"], {"sheets": 2, "cells": preview["stored_cells"], "charts": 2})
                    book.refresh()
                    names = {sheet["id"]: sheet["name"] for sheet in book.info["sheets"]}
                    self.assertEqual(sorted((names[item["sheet_id"]], item["name"]) for item in book.charts()),
                                     [("Column Chart", "Chart 1"), ("Stacked Column", "Chart 1")])

    def test_inline_charts_need_sheets(self):
        summary = self.prepare(sample_like())
        preview = xlsx_preview({"file_base64": b64(sample_like()), "scope": "workbook"})
        with self.assertRaises(EngineError) as raised:
            self.operate("workbook_import", stage_id=summary["stage_id"], stage_digest=summary["stage_digest"],
                         charts=preview["workbook_charts"])
        self.assertEqual(raised.exception.body["code"], "INVALID_ARGUMENT")
        with self.assertRaises(EngineError) as raised:
            self.operate("workbook_import", sheets=preview["workbook_sheets"], charts=[{"sheet": 2}])
        self.assertEqual(raised.exception.body["code"], "INVALID_ARGUMENT")
        self.assertEqual(self.charts(), [])

    def test_add_sheets_binds_to_new_sheets(self):
        self.operate("edit", sheet="Sheet1", cells=[{"row": 1, "col": 1, "source": "a"}, {"row": 1, "col": 2, "source": "1"},
                                                    {"row": 2, "col": 1, "source": "b"}, {"row": 2, "col": 2, "source": "2"}])
        spec = {"spec_version": 1, "type": "line", "categories": {"source": {"kind": "range", "sheet": "Sheet1", "ref": "A1:A2"}},
                "series": [{"name": {"text": "Mine"}, "values": {"source": {"kind": "range", "sheet": "Sheet1", "ref": "B1:B2"}}}]}
        own = self.operate("chart_create", sheet="Sheet1", spec=spec)["chart_id"]
        before = self.charts()
        receipt = self.apply(self.prepare(sample_like()), "add_sheets")
        self.assertEqual(receipt["imported"]["charts"], 2)
        after = self.charts()
        self.assertEqual([item for item in after if item["id"] == own], before)
        names = self.sheet_ids()
        imported = [item for item in after if item["id"] != own]
        self.assertEqual([names[item["sheet_id"]] for item in imported], ["Column Chart", "Stacked Column"])
        for item in imported:
            sources = [item["spec"]["categories"]["source"]] + [part["source"] for series in item["spec"]["series"]
                                                                for part in (series["name"], series["values"]) if "source" in part]
            self.assertTrue(sources)
            self.assertEqual({source["sheet_id"] for source in sources}, {item["sheet_id"]})
        self.assertEqual(self.resolve(imported[0]["id"])["series"][0]["values"], SALES)

    def test_workbook_limit_under_add_sheets(self):
        self.assertEqual(self.apply(self.prepare(many_charts(63)))["imported"]["charts"], 63)
        receipt = self.apply(self.prepare(sample_like()), "add_sheets")
        self.assertEqual(receipt["imported"]["charts"], 1)
        self.assertEqual(receipt["charts_left_out"], ["Chart 1"])
        names = self.sheet_ids()
        self.assertEqual(len(self.charts()), 64)
        self.assertEqual([names[item["sheet_id"]] for item in self.charts()].count("Column Chart"), 1)

    def test_digest_covers_charts(self):
        record = xlsx_preview({"file_base64": b64(sample_like()), "scope": "workbook"})["workbook_charts"][0]
        digests = []
        for name in ("Chart 1", "Other"):
            path = self.temp / f"stage-{name}.db"
            path.touch()
            stage = ImportStage(path)
            try:
                stage.sheet("Column Chart")
                stage.cells([{"row": 1, "col": 1, "source": "7", "kind": "number", "style": {}}])
                stage.charts([{**record, "name": name}])
                digests.append(stage.finish(ConversionReport(), {})["stage_digest"])
            finally:
                stage.close()
        self.assertNotEqual(*digests)
        with open_readonly(self.temp / "stage-Other.db") as reader:
            self.assertEqual(reader.chart_records()[0]["name"], "Other")
            self.assertEqual(reader.digest(), digests[1])

        summary = self.prepare(sample_like())
        with closing(sqlite3.connect(self.server.staging.path("stage", summary["stage_id"]))) as db, db:
            payload = json.loads(db.execute("SELECT payload FROM charts WHERE position = 0").fetchone()[0])
            db.execute("UPDATE charts SET payload = ? WHERE position = 0", (json.dumps({**payload, "name": "Altered"}),))
        with self.assertRaises(EngineError) as raised:
            self.apply(summary)
        self.assertEqual((raised.exception.body["code"], raised.exception.status), ("STALE_PLAN", 409))
        self.assertEqual(self.charts(), [])

    def test_stage_of_the_earlier_version_is_stale(self):
        summary = self.prepare(sample_like())
        path = self.server.staging.path("stage", summary["stage_id"])
        with closing(sqlite3.connect(path)) as db, db:
            meta = json.loads(db.execute("SELECT payload FROM meta").fetchone()[0])
            db.execute("UPDATE meta SET payload = ?", (json.dumps({**meta, "version": 1}),))
        self.assertEqual(import_stage.VERSION, 2)
        with self.assertRaises(EngineError) as raised:
            self.apply(summary)
        self.assertEqual(raised.exception.body["code"], "STALE_PLAN")
        self.assertIn("prepare the workbook again", raised.exception.body["message"])

    def test_values_mode(self):
        summary = self.prepare(sample_like(), mode="values")
        self.assertEqual(summary["charts"], SAMPLE_SUMMARY)
        self.assertEqual(self.apply(summary)["imported"]["charts"], 2)
        self.check_sample()

    def test_chart_on_skipped_chart_sheet_and_unsupported(self):
        radar = chart_xml(plot_area=plot_xml("radar", series=[{"cat": "Data!$A$2:$A$8", "val": "Data!$B$2:$B$8"}]))
        data = xlsx_with_charts({"Data": data_rows()}, [{"sheet": "Data", "name": "Radar", "xml": radar}],
                                extra_sheets='<sheet name="Chart Sheet" sheetId="10" r:id="rIdK1"/>',
                                workbook_rels=[f'<Relationship Id="rIdK1" Type="{CHARTSHEET}" Target="chartsheets/sheet1.xml"/>'],
                                extra_parts={"xl/chartsheets/sheet1.xml": f'<chartsheet xmlns="{MAIN}"/>'})
        summary = self.prepare(data)
        self.assertEqual(summary["charts"], [])
        self.assertEqual([(entry["detail"], entry["samples"]) for entry in entries(summary, "chart.skipped")],
                         [("type", [{"sheet": "Data", "cell": "Radar"}])])
        self.assertEqual({entry["detail"] for entry in entries(summary, "workbook.feature")} >= {"charts", "chart_sheets"}, True)
        receipt = self.apply(summary)
        self.assertEqual(receipt["imported"], {"sheets": 1, "cells": 16, "charts": 0})
        self.assertEqual(self.charts(), [])

    def test_malformed_chart_does_not_change_cells(self):
        good = sample_like()
        with zipfile.ZipFile(io.BytesIO(good)) as archive:
            part = archive.read("xl/charts/chart1.xml")
        bad = replace_part(good, "xl/charts/chart1.xml", part[:len(part) // 2])
        results = []
        for name, data in (("good", good), ("bad", bad)):
            summary = self.prepare(data)
            base = self.open(name)
            self.apply(summary, base=base)
            results.append((summary, self.stored_cells(base), len(self.charts(base))))
        (good_summary, good_cells, good_charts), (bad_summary, bad_cells, bad_charts) = results
        self.assertEqual(good_summary["stored_cells"], bad_summary["stored_cells"])
        self.assertEqual(good_summary["sheets"], bad_summary["sheets"])
        self.assertEqual(good_cells, bad_cells)
        self.assertEqual((good_charts, bad_charts), (2, 1))
        self.assertEqual([(entry["detail"], entry["samples"]) for entry in entries(bad_summary, "chart.skipped")],
                         [("unreadable", [{"sheet": "Column Chart", "cell": "Chart 1"}])])

    def test_import_clears_undo_as_before(self):
        self.operate("edit", sheet="Sheet1", cells=[{"row": 1, "col": 1, "source": "1"}])
        self.assertIsNotNone(self.info()["undo"])
        receipt = self.apply(self.prepare(sample_like()), "add_sheets")
        self.assertEqual(receipt["imported"]["charts"], 2)
        info = self.info()
        self.assertIsNone(info["undo"])
        self.assertIsNone(info["redo"])

    def test_more_than_64_charts_in_one_file(self):
        data = many_charts(66)
        staged = self.prepare(data)
        bounded = xlsx_preview({"file_base64": b64(data), "scope": "workbook"})
        self.assertEqual(len(bounded["workbook_charts"]), 64)
        for summary in (staged, bounded):
            self.assertEqual([item["name"] for item in summary["charts"]], [f"Chart {n}" for n in range(1, 65)])
            limit = [entry for entry in entries(summary, "chart.skipped") if entry["detail"] == "workbook_limit"]
            self.assertEqual([(entry["severity"], entry["count"], entry["samples"]) for entry in limit],
                             [("omitted", 2, [{"sheet": "Data", "cell": "Chart 65"}, {"sheet": "Data", "cell": "Chart 66"}])])
        self.assertEqual(self.apply(staged)["imported"]["charts"], 64)

    def test_ods_frames_past_the_frame_cap_are_reported_once(self):
        count = MAX_FRAMES + 2
        data = ods_with_charts({"Data": ods_data_rows()}, [{"sheet": "Data", "name": f"Chart {n}", "xml": ods_column_chart()}
                                                           for n in range(1, count + 1)])
        summary = self.prepare(data, "ods")
        self.assertEqual(len(summary["charts"]), 64)
        skipped = {entry["detail"]: entry for entry in entries(summary, "chart.skipped")}
        self.assertEqual((skipped["unreadable"]["count"], skipped["unreadable"]["samples"]), (2, [{"sheet": "Data", "cell": "drawing"}]))
        self.assertEqual(skipped["workbook_limit"]["count"], MAX_FRAMES - 64)
        self.assertEqual(skipped["workbook_limit"]["samples"][0], {"sheet": "Data", "cell": "Chart 65"})

    def test_declared_cell_sizes_mark_sizes_approximate(self):
        cols = '<cols><col min="1" max="3" width="20" customWidth="1"/></cols>'
        xlsx = {"declared": xlsx_with_charts({"Data": data_rows()}, [
                    {"sheet": "Data", "name": "Two", "xml": column_chart()},
                    {"sheet": "Data", "name": "One", "xml": column_chart(), "anchor": one_cell()}], sheet_before={"Data": cols}),
                "default": many_charts(1)}
        styles = styles_document('<style:style style:name="co1" style:family="table-column">'
                                 '<style:table-column-properties style:column-width="1in"/></style:style>')
        chart = [{"sheet": "Data", "name": "Shape", "xml": ods_column_chart(), "x": "1in", "y": "0.5in"}]
        ods = {"declared": ods_with_charts({"Data": '<table:table-column table:style-name="co1"/>' + ods_data_rows()}, chart, styles_xml=styles),
               "default": ods_with_charts({"Data": ods_data_rows()}, chart)}
        for fmt, cases in (("xlsx", xlsx), ("ods", ods)):
            preview = xlsx_preview if fmt == "xlsx" else ods_preview
            for case, data in cases.items():
                with self.subTest(fmt=fmt, case=case):
                    for summary in (self.prepare(data, fmt), preview({"file_base64": b64(data), "scope": "workbook"})):
                        found = [(entry["samples"]) for entry in entries(summary, "chart.converted") if entry["detail"] == "size_approximate"]
                        expected = {("xlsx", "declared"): [[{"sheet": "Data", "cell": "Two"}]],
                                    ("ods", "declared"): [[{"sheet": "Data", "cell": "Shape"}]]}.get((fmt, case), [])
                        self.assertEqual(found, expected)

    def test_summary_without_charts(self):
        for fmt in ("xlsx", "ods"):
            with self.subTest(fmt):
                data = (FIXTURES / f"fidelity.{fmt}").read_bytes()
                bounded = (xlsx_preview if fmt == "xlsx" else ods_preview)({"file_base64": b64(data), "scope": "workbook"})
                for summary in (self.prepare(data, fmt), bounded):
                    self.assertEqual(summary["charts"], [])
                    self.assertEqual([entry for entry in summary["report_entries"] if entry["category"].startswith("chart.")], [])
                    self.assertEqual(summary["scope_lines"][:3], workbook_report())
                    report = ConversionReport()
                    self.assertEqual(summary["report"], summary["scope_lines"] + [report._line(entry) for entry in summary["report_entries"]])
                self.assertEqual(bounded["workbook_charts"], [])

    def test_cli_summary_lists_charts(self):
        summary = self.prepare(sample_like())
        text = report_text(summary)
        self.assertIn("Charts:", text)
        self.assertIn("  Chart 1 on Column Chart (column)", text)
        self.assertIn("Total: 56 cells in 2 sheets, 2 charts", text)

    def test_openapi(self):
        spec = openapi()
        schemas = spec["components"]["schemas"]
        operation = schemas["WorkbookImportOperation"]
        self.assertIn("charts", operation["properties"])
        self.assertEqual(operation["properties"]["charts"]["maxItems"], 64)
        staged = next(variant for variant in operation["oneOf"] if variant["title"] == "Prepared stage")
        self.assertIn({"required": ["charts"]}, staged["not"]["anyOf"])
        for name in ("StagedImportSummary", "SpreadsheetImportPreview"):
            self.assertIn("charts", schemas[name]["properties"])
        self.assertIn("charts", schemas["StagedImportSummary"]["required"])
        self.assertIn("workbook_charts", schemas["SpreadsheetImportPreview"]["properties"])
        self.assertIn("charts", schemas["Receipt"]["properties"]["imported"]["required"])
        self.assertIn("charts_left_out", schemas["Receipt"]["properties"])
        try:
            from openapi_schema_validator import OAS30Validator
            from openapi_spec_validator import validate as validate_spec
        except ImportError:
            return  # The optional validators run in tests.test_job_openapi when installed.
        validate_spec(spec)
        record = xlsx_preview({"file_base64": b64(sample_like()), "scope": "workbook"})
        OAS30Validator({**schemas["SpreadsheetImportPreview"], "components": spec["components"]}).validate(record)

    def test_bounded_and_streaming_skip_the_same_charts_past_the_allowance(self):
        sheets = ("One", "Two", "Three")
        xlsx = xlsx_with_charts({name: data_rows() for name in sheets},
                                [{"sheet": name, "name": f"{name} {n}", "xml": column_chart(name, pad=60_000)} for name in sheets for n in (1, 2)])
        ods = ods_with_charts({name: ods_data_rows() for name in sheets},
                              [{"sheet": name, "name": f"{name} {n}", "xml": ods_column_chart(name, pad=60_000)} for name in sheets for n in (1, 2)])
        with patch.object(import_charts, "CHART_EXPANDED", 300_000):
            for fmt, data in (("xlsx", xlsx), ("ods", ods)):
                with self.subTest(fmt):
                    bounded = (xlsx_preview if fmt == "xlsx" else ods_preview)({"file_base64": b64(data), "scope": "workbook"})
                    staged = self.prepare(data, fmt)
                    self.assertEqual(bounded["charts"], staged["charts"])
                    self.assertEqual(entries(bounded, "chart.skipped"), entries(staged, "chart.skipped"))
                    self.assertTrue(0 < len(staged["charts"]) < 6, staged["charts"])
                    unread = sum(entry["count"] for entry in entries(staged, "chart.skipped") if entry["detail"] == "unreadable")
                    self.assertEqual(len(staged["charts"]) + unread, 6)

    def test_bounded_ods_past_the_workbook_and_frame_limits(self):
        for count in (66, MAX_FRAMES + 2):
            with self.subTest(count=count):
                frames = [{"sheet": "Data", "name": "Chart 1", "xml": ods_column_chart()}]
                frames += [{"sheet": "Data", "name": f"Chart {n}", "missing": True, "target": "./Object 1"} for n in range(2, count + 1)]
                preview = ods_preview({"file_base64": b64(ods_with_charts({"Data": ods_data_rows()}, frames)), "scope": "workbook"})
                self.assertEqual([item["name"] for item in preview["charts"]], [f"Chart {n}" for n in range(1, 65)])
                skipped = {entry["detail"]: entry for entry in entries(preview, "chart.skipped")}
                read = min(count, MAX_FRAMES)
                self.assertEqual(skipped["workbook_limit"]["count"], read - 64)
                self.assertEqual(skipped["workbook_limit"]["samples"][0], {"sheet": "Data", "cell": "Chart 65"})
                if count > MAX_FRAMES:
                    self.assertEqual((skipped["unreadable"]["count"], skipped["unreadable"]["samples"]),
                                     (count - MAX_FRAMES, [{"sheet": "Data", "cell": "drawing"}]))
                else:
                    self.assertNotIn("unreadable", skipped)

    def test_default_declared_sizes_are_not_approximate(self):
        defaults = '<sheetFormatPr defaultRowHeight="15"/><cols><col min="1" max="3" width="9.140625"/></cols>'
        tall = data_rows().replace('<row r="1">', '<row r="1" ht="30" customHeight="1">')
        cases = {"default widths": (data_rows(), defaults, []), "custom row height": (tall, "", [[{"sheet": "Data", "cell": "Chart 1"}]])}
        for case, (rows, before, expected) in cases.items():
            data = xlsx_with_charts({"Data": rows}, [{"sheet": "Data", "name": "Chart 1", "xml": column_chart()}], sheet_before={"Data": before})
            for summary in (self.prepare(data), xlsx_preview({"file_base64": b64(data), "scope": "workbook"})):
                with self.subTest(case):
                    self.assertEqual(len(summary["charts"]), 1)
                    self.assertEqual([entry["samples"] for entry in entries(summary, "chart.converted") if entry["detail"] == "size_approximate"],
                                     expected)


class RecordingSink:
    """Counts each sheet's cells and keeps the chart records the reader hands over (once)."""
    def __init__(self):
        self.counts, self.records, self.current, self.table = {}, None, None, {}
        self.strings = self

    def put(self, index, text): self.table[index] = text

    def get(self, index): return self.table[index]

    def sheet(self, name): self.counts[name], self.current = 0, name

    def cells(self, batch): self.counts[self.current] += len(batch)

    def charts(self, records):
        assert self.records is None, "charts is called once"
        self.records = records


class ChartAllowanceTests(unittest.TestCase):
    """Chart, drawing and relationship parts are read against their own allowance, never the cells' package budget."""

    def setUp(self):
        self.temp = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def stream(self, data, fmt="xlsx"):
        path = self.temp / f"book-{uuid.uuid4().hex}.{fmt}"
        path.write_bytes(data)
        sink = RecordingSink()
        reader = xlsx_stream.read_workbook if fmt == "xlsx" else ods_stream.read_workbook
        return sink, reader(path, {}, sink, lambda *_: None)

    def test_a_chart_part_drawn_many_times_never_fails_later_cells(self):
        frames = [{"sheet": "Data", "name": "Chart 1", "xml": column_chart(pad=1_200_000)}]
        frames += [{"sheet": "Data", "name": f"Chart {n}", "missing": True, "target": "../charts/chart1.xml"} for n in range(2, 251)]
        rows = "".join(f'<row r="{row}"><c r="A{row}"><v>{row}</v></c></row>' for row in range(1, 30_001))
        sink, report = self.stream(xlsx_with_charts({"Data": data_rows(), "Rows": rows}, frames))
        self.assertEqual(sink.counts, {"Data": 16, "Rows": 30_000})
        self.assertEqual(len(sink.records), 64)
        self.assertEqual([entry["detail"] for entry in report.entries() if entry["category"] == "chart.skipped"], ["workbook_limit"])

    def test_charts_past_the_allowance_are_reported_and_cells_import(self):
        count = 20
        xlsx = xlsx_with_charts({"Data": data_rows()}, [{"sheet": "Data", "name": f"Chart {n}", "xml": column_chart(pad=1_000_000)}
                                                        for n in range(1, count + 1)])
        ods = ods_with_charts({"Data": ods_data_rows()}, [{"sheet": "Data", "name": f"Chart {n}", "xml": ods_column_chart(pad=1_000_000)}
                                                          for n in range(1, count + 1)])
        for fmt, data in (("xlsx", xlsx), ("ods", ods)):
            with self.subTest(fmt):
                sink, report = self.stream(data, fmt)
                self.assertEqual(sink.counts, {"Data": 16})
                made = len(sink.records)
                self.assertTrue(import_charts.CHART_EXPANDED // 1_100_000 <= made < count, made)
                unread = [entry for entry in report.entries() if entry["category"] == "chart.skipped"]
                self.assertEqual([(entry["detail"], entry["count"]) for entry in unread], [("unreadable", count - made)])
                self.assertEqual(unread[0]["samples"][0], {"sheet": "Data", "cell": f"Chart {made + 1}"})

    def test_charts_never_charge_the_package_budget(self):
        def variants(data, chart_parts, pad):
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                parts = {name: archive.read(name) for name in archive.namelist()}
            removed, enlarged = io.BytesIO(), io.BytesIO()
            with zipfile.ZipFile(removed, "w", zipfile.ZIP_DEFLATED) as without, zipfile.ZipFile(enlarged, "w", zipfile.ZIP_DEFLATED) as larger:
                for name, payload in parts.items():
                    if name not in chart_parts:
                        without.writestr(name, payload)
                    larger.writestr(name, pad(payload) if name in chart_parts else payload)
            return [data, removed.getvalue(), enlarged.getvalue()]

        xlsx_pad = lambda payload: payload.replace(b"<c:plotArea>", b"<c:plotArea>" + b" " * 2_000_000)
        ods_pad = lambda payload: payload.replace(b"<office:body>", b"<office:body>" + b" " * 2_000_000)
        cases = {"xlsx": variants(sample_like(), {"xl/charts/chart1.xml", "xl/charts/chart2.xml"}, xlsx_pad),
                 "ods": variants(ods_sample_like(), {"Object 1/content.xml", "Object 2/content.xml"}, ods_pad)}
        for fmt, datas in cases.items():
            with self.subTest(fmt):
                used = []
                for data in datas:
                    budgets = []

                    class Recording(xlsx_stream.ByteBudget):
                        def __init__(self, limit):
                            super().__init__(limit)
                            budgets.append(self)
                    with patch.object(xlsx_stream, "ByteBudget", Recording):
                        sink, _ = self.stream(data, fmt)
                    used.append([budget.used for budget in budgets])
                    self.assertEqual(sum(sink.counts.values()), 56)
                self.assertEqual(used[0], used[1])
                self.assertEqual(used[0], used[2])
                self.assertTrue(all(value > 0 for value in used[0]))


if __name__ == "__main__":
    unittest.main()
