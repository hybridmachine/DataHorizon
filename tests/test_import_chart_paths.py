# SPDX-License-Identifier: GPL-3.0-only
"""Whole-workbook imports carry charts: the staged (streaming) and inline (bounded) paths, through the service."""
import base64
from contextlib import closing
import io
import json
from pathlib import Path
import random
import sqlite3
import tempfile
import time
import unittest
import uuid
import sys
import zipfile
import zlib
from unittest.mock import patch

from chart_fixtures import (chart_xml, two_cell, ods_chart_xml, ods_sample_like, ods_title, ods_with_charts, one_cell, plot_xml,
                            rich_title, sample_like, xlsx_with_charts)
from ods_builder import styles_document
from datahorizon import import_charts, import_stage, ods_stream, xlsx_stream
from datahorizon.import_charts import MAX_FRAMES
from datahorizon.cli import report_text
from datahorizon import charts as charts_module
from datahorizon.client import APIError, LocalService
from datahorizon.conversion_report import ConversionReport
from datahorizon.engine import EngineError, Workbook
from datahorizon.workbook_code import activation, fingerprint
from datahorizon.import_stage import ImportStage, open_readonly
from datahorizon.interchange import workbook_report, xlsx_preview
from datahorizon.ods import ods_preview
from datahorizon.service import Service, openapi

FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(FIXTURES))
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

    def test_chart_sheet_anywhere_binds_charts_to_their_worksheets(self):
        sheet_xml = '<sheet name="Chart Sheet" sheetId="10" r:id="rIdK1"/>'
        last = xlsx_with_charts({"Data": data_rows(), "Other": data_rows()},
                                [{"sheet": "Data", "name": "OnData", "xml": column_chart("Data")},
                                 {"sheet": "Other", "name": "OnOther", "xml": column_chart("Other")},
                                 {"sheet": "Other", "name": "Cross", "xml": column_chart("Data")}],
                                extra_sheets=sheet_xml,
                                workbook_rels=[f'<Relationship Id="rIdK1" Type="{CHARTSHEET}" Target="chartsheets/sheet1.xml"/>'],
                                extra_parts={"xl/chartsheets/sheet1.xml": f'<chartsheet xmlns="{MAIN}"/>'})
        with zipfile.ZipFile(io.BytesIO(last)) as archive:
            workbook = archive.read("xl/workbook.xml").decode().replace(sheet_xml, "")
        layouts = {"first": replace_part(last, "xl/workbook.xml", workbook.replace("<sheets>", "<sheets>" + sheet_xml).encode()),
                   "middle": replace_part(last, "xl/workbook.xml", workbook.replace('<sheet name="Other"', sheet_xml + '<sheet name="Other"').encode()),
                   "last": last}
        expected = [("OnData", "Data", "Data"), ("Cross", "Other", "Data"), ("OnOther", "Other", "Other")]
        for layout, data in layouts.items():
            with self.subTest(layout):
                order = {"first": ["Chart Sheet", "Data", "Other"], "middle": ["Data", "Chart Sheet", "Other"],
                         "last": ["Data", "Other", "Chart Sheet"]}[layout]
                # Selection: each worksheet's own charts, rebased to sheet 0 and shifted with the paste at C3 (row 3, col 2).
                for index, name in enumerate(order, 1):
                    request = {"file_base64": b64(data), "scope": "selection", "sheet_index": index, "row": 3, "col": 2}
                    if name == "Chart Sheet":
                        with self.assertRaises(EngineError) as raised:
                            xlsx_preview(request)
                        self.assertEqual(raised.exception.body["code"], "INVALID_ARGUMENT")
                        continue
                    preview = xlsx_preview(request)
                    self.assertEqual(preview["sheet"], name)
                    own = [item for item in expected if item[1] == name and item[2] == name]
                    self.assertEqual([(record["name"], record["sheet"], record["anchor"], record["spec"]["series"][0]["values"]["source"])
                                      for record in preview["charts"]],
                                     [(item[0], 0, {"row": 3, "col": 4}, {"kind": "range", "sheet": 0, "ref": "C4:C10"}) for item in own])
                    crossing = [(entry["detail"], entry["samples"]) for entry in entries(preview, "chart.skipped")]
                    self.assertEqual(crossing, [("source", [{"sheet": "Other", "cell": "Cross"}])] if name == "Other" else [])
                # Whole workbook, inline and staged: positions among the worksheets, then the stored sheets.
                worksheets = ["Data", "Other"]
                inline = xlsx_preview({"file_base64": b64(data), "scope": "workbook"})
                self.assertEqual([sheet["name"] for sheet in inline["workbook_sheets"]], worksheets)
                self.assertEqual(sorted((record["name"], worksheets[record["sheet"]], worksheets[record["spec"]["series"][0]["values"]["source"]["sheet"]])
                                        for record in inline["workbook_charts"]), sorted(expected))
                summary = self.prepare(data)
                self.assertEqual(sorted((item["name"], item["sheet"]) for item in summary["charts"]), sorted(item[:2] for item in expected))
                base = self.open(f"chartsheet-{layout}")
                self.assertEqual(self.apply(summary, base=base)["imported"]["charts"], 3)
                names = self.sheet_ids(base)
                self.assertEqual(sorted((item["name"], names[item["sheet_id"]], names[item["spec"]["series"][0]["values"]["source"]["sheet_id"]])
                                        for item in self.charts(base)), sorted(expected))
                with LocalService() as client, client.open(self.temp / f"chartsheet-edit-{layout}.dhwb", create=True) as book:
                    index = order.index("Other") + 1
                    book.import_prepared(xlsx_preview({"file_base64": b64(data), "scope": "selection", "sheet_index": index}), "Sheet1")
                    book.refresh()
                    (chart,) = book.charts()
                    self.assertEqual(chart["name"], "OnOther")
                    self.assertEqual(book.resolve_chart(chart["id"])["resolved"]["series"][0]["values"], list(range(2, 9)))

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

    def test_long_chart_text_never_fails_an_import(self):
        long = "N" * 300_000
        named = [{"cat": "Data!$A$2:$A$8", "val": "Data!$B$2:$B$8", "name_text": long}]
        cases = {
            "series name": (xlsx_with_charts({"Data": data_rows()}, [{"sheet": "Data", "xml": chart_xml("bar", "col", "clustered", named)}]),
                            "xlsx", "series_name_cut"),
            "axis title": (xlsx_with_charts({"Data": data_rows()}, [{"sheet": "Data", "xml": chart_xml(
                "bar", "col", "clustered", [{**named[0], "name_text": "S"}], category_axis=rich_title(long))}]),
                           "xlsx", "title_cut"),
            "ods axis title": (ods_with_charts({"Data": ods_data_rows()}, [{"sheet": "Data", "xml": ods_chart_xml(
                "bar", "col", "clustered", [{"cat": "Data!$A$2:$A$8", "val": "Data!$B$2:$B$8"}], category_axis=ods_title(long))}]),
                               "ods", "title_cut")}
        for case, (data, fmt, detail) in cases.items():
            preview = xlsx_preview if fmt == "xlsx" else ods_preview
            with self.subTest(case, path="staged"):
                summary = self.prepare(data, fmt)
                self.assertEqual(len(summary["charts"]), 1)
                self.assertIn(detail, [entry["detail"] for entry in entries(summary, "chart.converted")])
                base = self.open(f"staged-{fmt}-{case}")
                self.assertEqual(self.apply(summary, base=base)["imported"], {"sheets": 1, "cells": 16, "charts": 1})
                (chart,) = self.charts(base)
                spec = chart["spec"]
                texts = [spec["series"][0]["name"].get("text"), spec["axes"]["category"]["title"]]
                self.assertIn("N" * 255, texts)
            with LocalService() as client:
                with self.subTest(case, path="inline"), client.open(self.temp / f"inline-{fmt}-{case}.dhwb", create=True) as book:
                    workbook = preview({"file_base64": b64(data), "scope": "workbook"})
                    self.assertEqual(book.import_prepared(workbook)["imported"], {"sheets": 1, "cells": 16, "charts": 1})
                with self.subTest(case, path="edit"), client.open(self.temp / f"edit-{fmt}-{case}.dhwb", create=True) as book:
                    selection = preview({"file_base64": b64(data), "scope": "selection"})
                    self.assertIn(detail, [entry["detail"] for entry in entries(selection, "chart.converted")])
                    receipt = book.import_prepared(selection, "Sheet1")
                    self.assertEqual((receipt["changed_cells"], receipt["changed_charts"]), (16, 1))

    def test_many_long_series_names_stay_inside_the_preview_limits(self):
        series = [{"cat": "Data!$A$2:$A$8", "val": "Data!$B$2:$B$8", "name_text": "S" * 1200} for _ in range(60)]
        many = [{"sheet": "Data", "name": f"C{n}", "xml": chart_xml("bar", "col", "clustered", series)} for n in range(64)]
        snow = [{"cat": "Data!$A$2:$A$8", "val": "Data!$B$2:$B$8", "name_text": "☃" * 300} for _ in range(60)]
        wide = [{"sheet": "Data", "name": "Wide", "xml": chart_xml("bar", "col", "clustered", snow)}]
        for case, charts, detail in (("total", many, "workbook_limit"), ("one record", wide, "limit")):
            data = xlsx_with_charts({"Data": data_rows()}, charts)
            with self.subTest(case):
                summary = self.prepare(data)
                workbook = xlsx_preview({"file_base64": b64(data), "scope": "workbook"})
                selection = xlsx_preview({"file_base64": b64(data), "scope": "selection"})
                self.assertLessEqual(sum(import_charts.record_size(item) for item in workbook["workbook_charts"]),
                                     import_charts.RECORDS_BYTES)
                self.assertLess(len(summary["charts"]), len(charts))
                self.assertEqual(len(summary["charts"]), len(workbook["charts"]))
                self.assertEqual(len(selection["charts"]), len(workbook["charts"]))
                for found in (summary, workbook, selection):
                    self.assertIn(detail, [entry["detail"] for entry in entries(found, "chart.skipped")])
                base = self.open(f"many-{case}")
                self.assertEqual(self.apply(summary, base=base)["imported"]["cells"], 16)
                with LocalService() as client:
                    with client.open(self.temp / f"many-inline-{case}.dhwb", create=True) as book:
                        self.assertEqual(book.import_prepared(workbook)["imported"]["charts"], len(workbook["charts"]))
                    with client.open(self.temp / f"many-edit-{case}.dhwb", create=True) as book:
                        self.assertEqual(book.import_prepared(selection, "Sheet1")["changed_charts"], len(selection["charts"]))

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


PKG = "http://schemas.openxmlformats.org/package/2006/relationships"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def shared_drawing(sheets=1, frames=256, drawing_rels=1, series=1, distinct=1, pad=0):
    """Every sheet's relationships hold ``drawing_rels`` drawing relationships, all to one drawing of ``frames`` one-cell
    frames, which cycle over ``distinct`` chart parts of ``series`` series each, padded with ``pad`` empty elements."""
    from xlsx_builder import workbook_bytes
    names = {f"S{n}": data_rows() for n in range(1, sheets + 1)}
    extras = "<c:extLst>" + "<c:ext/>" * pad + "</c:extLst>" if pad else ""
    items = [{"cat": "S1!$A$2:$A$8", "val": "S1!$B$2:$B$8"} for _ in range(series)]
    parts = {f"xl/charts/chart{k}.xml": chart_xml("bar", "col", "clustered", items, extras=extras) for k in range(1, distinct + 1)}
    from chart_fixtures import A, C, XDR, frame_xml
    anchors = "".join(one_cell().replace("{frame}", frame_xml(f"rId{n % distinct + 1}", f"F{n}")) for n in range(frames))
    parts["xl/drawings/drawing1.xml"] = f'<xdr:wsDr xmlns:xdr="{XDR}" xmlns:a="{A}" xmlns:r="{REL}" xmlns:c="{C}">{anchors}</xdr:wsDr>'
    parts["xl/drawings/_rels/drawing1.xml.rels"] = f'<Relationships xmlns="{PKG}">' + "".join(
        f'<Relationship Id="rId{k}" Type="{REL}/chart" Target="../charts/chart{k}.xml"/>' for k in range(1, distinct + 1)) + "</Relationships>"
    for number in range(1, sheets + 1):
        parts[f"xl/worksheets/_rels/sheet{number}.xml.rels"] = f'<Relationships xmlns="{PKG}">' + "".join(
            f'<Relationship Id="rId{k}" Type="{REL}/drawing" Target="../drawings/drawing1.xml"/>' for k in range(1, drawing_rels + 1)) + "</Relationships>"
    return workbook_bytes(names, extra_parts=parts, sheet_after={name: '<drawing r:id="rId1"/>' for name in names})


def shared_object(sheets=1, frames=256, distinct=1):
    """ODS: ``frames`` frames on each sheet, cycling over ``distinct`` chart objects."""
    charts = [{"sheet": f"S{s}", "name": f"F{s}-{n}", "xml": ods_column_chart("S1"), "missing": n >= distinct or s > 1,
               "target": f"./Object {n % distinct + 1}"} for s in range(1, sheets + 1) for n in range(frames)]
    return ods_with_charts({f"S{s}": ods_data_rows() for s in range(1, sheets + 1)}, charts)


class ChartWorkBoundTests(unittest.TestCase):
    """The work of reading charts is bounded per import, whatever the package repeats: counted, not timed."""

    def setUp(self):
        self.temp = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def readers(self, data, fmt="xlsx", path="streaming", checkpoint=lambda *_: None):
        """The ChartParts the import used and the report, read by the streaming reader or the bounded preview."""
        made = []

        class Recording(import_charts.ChartParts):
            def __init__(self, *args, **options):
                super().__init__(*args, **options)
                made.append(self)
        with patch.object(import_charts, "ChartParts", Recording):
            if path == "streaming":
                file = self.temp / f"book-{uuid.uuid4().hex}.{fmt}"
                file.write_bytes(data)
                sink = RecordingSink()
                report = (xlsx_stream.read_workbook if fmt == "xlsx" else ods_stream.read_workbook)(file, {}, sink, checkpoint)
                records, entries_found = sink.records, report.entries()
            else:
                preview = (xlsx_preview if fmt == "xlsx" else ods_preview)({"file_base64": b64(data), "scope": path}, checkpoint)
                records = preview["workbook_charts"] if path == "workbook" else preview["charts"]
                entries_found = preview["report_entries"]
        (parts,) = made
        skipped = {entry["detail"]: entry["count"] for entry in entries_found if entry["category"] == "chart.skipped"}
        return parts, records, skipped

    def check(self, data, fmt, expected, paths=("streaming", "workbook", "selection")):
        for path in paths:
            with self.subTest(fmt=fmt, path=path):
                parts, records, skipped = self.readers(data, fmt, path)
                found = {"parses": parts.parses, "walks": parts.walks, "drafts": parts.drafts, "listings": parts.listings,
                         "records": len(records), "skipped": skipped}
                want = dict(expected)
                if path == "selection":
                    want.update(want.pop("selection", {}))
                want.pop("selection", None)
                self.assertEqual(found, want)

    def test_many_frames_of_one_chart_part(self):
        # Sheet relationships, drawing, drawing relationships and the chart: each read once.
        self.check(shared_drawing(frames=300, pad=20_000), "xlsx",
                   {"parses": 4, "walks": 1, "drafts": 64, "listings": 1, "records": 64,
                    "skipped": {"workbook_limit": 256 - 64, "unreadable": 300 - 256}})

    def test_sheets_sharing_a_drawing(self):
        # The frame cap is a total: the first sheet reads 256 frames, the others none.
        self.check(shared_drawing(sheets=4, pad=20_000), "xlsx",
                   {"parses": 7, "walks": 1, "drafts": 64, "listings": 1, "records": 64,
                    "skipped": {"workbook_limit": 256 - 64, "unreadable": 3 * 256},
                    "selection": {"parses": 4, "skipped": {"workbook_limit": 256 - 64}}})

    def test_a_sheet_with_many_drawing_relationships(self):
        # Only the drawing the worksheet's <drawing r:id> names is read; the 65-series chart is skipped as limit.
        self.check(shared_drawing(drawing_rels=200, series=65), "xlsx",
                   {"parses": 4, "walks": 1, "drafts": 256, "listings": 1, "records": 0, "skipped": {"limit": 256}})
        self.check(shared_drawing(drawing_rels=8, pad=20_000), "xlsx",
                   {"parses": 4, "walks": 1, "drafts": 64, "listings": 1, "records": 64, "skipped": {"workbook_limit": 192}})

    def test_no_chart_part_is_read_once_no_chart_could_be_imported(self):
        # The chart parts cycle: the first 64 are read; the next 36 are never read; the rest are repeats.
        self.check(shared_drawing(distinct=100), "xlsx",
                   {"parses": 3 + 64, "walks": 64, "drafts": 64, "listings": 1, "records": 64,
                    "skipped": {"workbook_limit": 256 - 64}})

    def test_ods_objects(self):
        self.check(shared_object(frames=300), "ods",
                   {"parses": 1, "walks": 1, "drafts": 64, "listings": 1, "records": 64,
                    "skipped": {"workbook_limit": 256 - 64, "unreadable": 300 - 256}})
        self.check(shared_object(sheets=3, frames=100), "ods",
                   {"parses": 1, "walks": 1, "drafts": 64, "listings": 1, "records": 64,
                    "skipped": {"workbook_limit": 256 - 64, "unreadable": 300 - 256},
                    "selection": {"skipped": {"workbook_limit": 100 - 64}}}, paths=("streaming", "workbook", "selection"))
        self.check(shared_object(frames=256, distinct=100), "ods",
                   {"parses": 64, "walks": 64, "drafts": 64, "listings": 1, "records": 64,
                    "skipped": {"workbook_limit": 256 - 64}})

    def test_no_parsed_tree_is_kept(self):
        import xml.etree.ElementTree as ET

        def trees(value):
            if isinstance(value, ET.Element):
                return 1
            if isinstance(value, dict):
                return sum(trees(item) for item in value.values())
            if isinstance(value, (list, tuple)):
                return sum(trees(item) for item in value)
            return 0
        for fmt, data in (("xlsx", shared_drawing(distinct=10, pad=20_000)), ("ods", shared_object(distinct=10))):
            for path in ("streaming", "workbook"):
                with self.subTest(fmt=fmt, path=path):
                    parts, records, _ = self.readers(data, fmt, path)
                    self.assertEqual(len(records), 64)
                    self.assertEqual(parts.walks, 10)
                    self.assertEqual(trees(vars(parts)), 0)

    def test_charts_are_read_between_checkpoints(self):
        for fmt, data in (("xlsx", shared_drawing(distinct=10)), ("ods", shared_object(distinct=10))):
            with self.subTest(fmt):
                phases = []
                self.readers(data, fmt, checkpoint=lambda completed, phase: phases.append(phase))
                self.assertGreaterEqual(phases.count("Reading charts"), 256)

                def cancel(completed, phase):
                    if phase == "Reading charts":
                        raise EngineError("CANCELLED", "Preparation cancelled; no workbook changes were made")
                with self.assertRaises(EngineError) as raised:
                    self.readers(data, fmt, checkpoint=cancel)
                self.assertEqual(raised.exception.body["code"], "CANCELLED")


def corrupted(data, name):
    """``data`` with part ``name`` padded by a long comment of random text, then four bytes flipped three quarters into
    its compressed stream: reading the part fails with a zlib, CRC or end-of-stream error, never with clean XML."""
    choose = random.Random(7).choice
    junk = "".join(choose("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(400_000))
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        text = archive.read(name).decode()
    close = text.rindex("</")
    padded = replace_part(data, name, (text[:close] + f"<!--{junk}-->" + text[close:]).encode())
    with zipfile.ZipFile(io.BytesIO(padded)) as archive:
        info = archive.getinfo(name)
        start = info.header_offset + 30 + len(info.filename.encode()) + len(info.extra)
        first = start + info.compress_size * 3 // 4
    # Most flips only break the CRC (BadZipFile, which interchange.read_xml already reports); the first position, in a
    # fixed order, whose flip breaks the deflate stream itself gives the raw zlib.error a reader has to contain.
    for position in range(first, start + info.compress_size - 4, 97):
        raw = bytearray(padded)
        for offset in range(4):
            raw[position + offset] ^= 0xFF
        with zipfile.ZipFile(io.BytesIO(bytes(raw))) as archive:
            try:
                archive.read(name)
            except zlib.error:
                return bytes(raw)
            except (zipfile.BadZipFile, EOFError):
                continue
    raise AssertionError(f"no flip in {name} breaks its deflate stream")


class CorruptChartPartTests(unittest.TestCase):
    """A chart, drawing or relationship part whose compressed bytes are corrupt is reported, and the cells import."""

    def setUp(self):
        self.temp = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def stream(self, data, fmt):
        path = self.temp / f"book-{uuid.uuid4().hex}.{fmt}"
        path.write_bytes(data)
        sink = RecordingSink()
        report = (xlsx_stream.read_workbook if fmt == "xlsx" else ods_stream.read_workbook)(path, {}, sink, lambda *_: None)
        return sum(sink.counts.values()), len(sink.records), report.entries()

    def test_corrupt_chart_parts(self):
        xlsx = xlsx_with_charts({"Data": data_rows()}, [{"sheet": "Data", "name": "Bar", "xml": column_chart()}])
        ods = ods_with_charts({"Data": ods_data_rows()}, [{"sheet": "Data", "name": "Bar", "xml": ods_column_chart()}])
        cases = (("xlsx", xlsx, "xl/drawings/drawing1.xml", "drawing"), ("xlsx", xlsx, "xl/drawings/_rels/drawing1.xml.rels", "drawing"),
                 ("xlsx", xlsx, "xl/charts/chart1.xml", "Bar"), ("ods", ods, "Object 1/content.xml", "Bar"))
        for fmt, source, name, label in cases:
            data = corrupted(source, name)
            preview = xlsx_preview if fmt == "xlsx" else ods_preview
            found = {"streaming": self.stream(data, fmt)}
            for scope in ("workbook", "selection"):
                result = preview({"file_base64": b64(data), "scope": scope})
                found[scope] = (result["stored_cells"], len(result["workbook_charts" if scope == "workbook" else "charts"]),
                                result["report_entries"])
            for path, (cells, charts, report) in found.items():
                with self.subTest(part=name, path=path):
                    self.assertEqual((cells, charts), (16, 0))
                    self.assertEqual([(entry["detail"], entry["samples"]) for entry in report if entry["category"] == "chart.skipped"],
                                     [("unreadable", [{"sheet": "Data", "cell": label}])])


STACKED = "Stacked Column"


class SelectionChartTests(unittest.TestCase):
    """The one-sheet import: a bounded selection preview, then a plain edit that carries its charts."""
    def setUp(self):
        self.temp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.formats = {"xlsx": (sample_like(), xlsx_preview), "ods": (ods_sample_like(), ods_preview)}

    def preview(self, fmt="xlsx", data=None, **request):
        source, function = self.formats[fmt]
        return function({"file_base64": b64(data or source), "scope": "selection", **request})

    def book(self, name="book"):
        client = self.enterContext(LocalService())
        return self.enterContext(client.open(self.temp / f"{name}.dhwb", create=True))

    def resolved(self, book, chart):
        book.refresh()
        return book.resolve_chart(chart["id"])["resolved"]

    def test_selection_preview_and_edit(self):
        preview = self.preview(sheet_index=2, row=1, col=1)
        self.assertEqual(len(preview["charts"]), 1)
        self.assertEqual((preview["charts"][0]["sheet"], preview["charts"][0]["name"]), (0, "Chart 1"))
        book = self.book()
        receipt = book.import_prepared(preview, "Sheet1")
        self.assertEqual(receipt["changed_charts"], 1)
        self.assertNotIn("charts_left_out", receipt)
        book.refresh()
        (chart,) = book.charts()
        self.assertEqual(book.info["sheets"][0]["id"], chart["sheet_id"])
        resolved = self.resolved(book, chart)
        self.assertEqual(resolved["series"][0]["values"], [2, 3, 4, 5, 6, 7, 8])
        self.assertEqual(resolved["categories"][0], "Division 2")

    def test_selection_offset_shifts_chart(self):
        preview = self.preview(sheet_index=2, row=5, col=3)
        (record,) = preview["charts"]
        self.assertEqual(record["anchor"], {"row": 5, "col": 5})
        spec = record["spec"]
        self.assertEqual(spec["categories"]["source"]["ref"], "C6:C12")
        self.assertEqual(spec["series"][0]["values"]["source"]["ref"], "D6:D12")
        self.assertEqual({source["sheet"] for source in (spec["categories"]["source"], spec["series"][0]["values"]["source"])}, {0})
        book = self.book()
        book.import_prepared(preview, "Sheet1")
        book.refresh()
        resolved = self.resolved(book, book.charts()[0])
        self.assertEqual(resolved["series"][0]["values"], [2, 3, 4, 5, 6, 7, 8])
        self.assertEqual(resolved["categories"][0], "Division 2")

    def test_selection_chart_on_other_sheet_is_skipped(self):
        data = xlsx_with_charts({"First": data_rows(), "Second": data_rows()},
                                [{"sheet": "First", "name": "Elsewhere", "xml": column_chart("Second")}])
        preview = self.preview(data=data, sheet_index=1)
        self.assertEqual(preview["charts"], [])
        self.assertEqual([(entry["detail"], entry["samples"]) for entry in entries(preview, "chart.skipped")],
                         [("source", [{"sheet": "First", "cell": "Elsewhere"}])])

    def test_selection_off_grid(self):
        far = xlsx_with_charts({"Data": data_rows()}, [{"sheet": "Data", "name": "Far", "xml": column_chart(),
                                                        "anchor": two_cell((16379, 0, 0, 0), (16383, 0, 15, 0))}])
        plain = xlsx_with_charts({"Data": data_rows()}, [])
        with_chart = self.preview(data=far, row=1, col=10)
        without = self.preview(data=plain, row=1, col=10)
        self.assertEqual(with_chart["charts"], [])
        self.assertEqual([(entry["detail"], entry["samples"]) for entry in entries(with_chart, "chart.skipped")],
                         [("off_grid", [{"sheet": "Data", "cell": "Far"}])])
        for key in ("cells", "can_import", "stored_cells"):
            self.assertEqual(with_chart[key], without[key], key)

    def test_charts_never_change_the_selection_cells(self):
        for fmt in ("xlsx", "ods"):
            source, function = self.formats[fmt]
            with self.subTest(fmt):
                full = self.preview(fmt, sheet_index=2, row=3, col=2)
                self.assertEqual(len(full["charts"]), 1)
                if fmt == "xlsx":
                    with zipfile.ZipFile(io.BytesIO(source)) as archive:
                        part = archive.read("xl/charts/chart2.xml")
                    broken = replace_part(source, "xl/charts/chart2.xml", part[:len(part) // 2])
                else:
                    broken = source.replace(b"chart:plot-area", b"chart:plot-ared")
                bad = self.preview(fmt, broken, sheet_index=2, row=3, col=2)
                for key in ("cells", "can_import", "stored_cells"):
                    self.assertEqual(full[key], bad[key], key)

    def test_edit_with_charts_is_one_undo_step(self):
        book = self.book()
        record = self.preview(sheet_index=2)["charts"]
        cells = [{"row": 1, "col": 1, "source": "1"}]
        receipt = book.apply("edit", "Sheet1", cells=cells, charts=record, label="Import spreadsheet")
        self.assertEqual((receipt["changed_cells"], receipt["changed_charts"]), (1, 1))
        book.refresh()
        (chart,) = book.charts()
        book.apply("undo")
        book.refresh()
        self.assertEqual(book.charts(), [])
        book.apply("redo")
        book.refresh()
        self.assertEqual([item["id"] for item in book.charts()], [chart["id"]])

    def test_edit_charts_validation(self):
        book = self.book()
        record = self.preview(sheet_index=2)["charts"][0]
        cells = [{"row": 1, "col": 1, "source": "1"}]
        for bad in ([{**record, "sheet": 1}], record, "x", [3]):
            with self.subTest(bad=bad), self.assertRaises(APIError) as caught:
                book.apply("edit", "Sheet1", cells=cells, charts=bad)
            self.assertEqual(caught.exception.body["code"], "INVALID_ARGUMENT")
        book.refresh()
        self.assertEqual(book.charts(), [])

    def test_ods_selection(self):
        preview = self.preview("ods", sheet_index=2, row=5, col=3)
        (record,) = preview["charts"]
        self.assertEqual(record["anchor"], {"row": 5, "col": 5})
        self.assertEqual(record["spec"]["categories"]["source"]["ref"], "C6:C12")
        book = self.book("ods")
        self.assertEqual(book.import_prepared(preview, "Sheet1")["changed_charts"], 1)
        book.refresh()
        self.assertEqual(self.resolved(book, book.charts()[0])["series"][0]["values"], [2, 3, 4, 5, 6, 7, 8])

    def test_openapi_describes_selection_charts(self):
        spec = openapi()
        schemas = spec["components"]["schemas"]
        self.assertEqual(schemas["EditOperation"]["properties"]["charts"]["maxItems"], 64)
        self.assertIn("charts", schemas["SpreadsheetImportPreview"]["properties"])
        self.assertIn("edit", schemas["Receipt"]["properties"]["charts_left_out"]["description"])
        try:
            from openapi_schema_validator import OAS30Validator
        except ImportError:
            return
        for fmt in ("xlsx", "ods"):
            OAS30Validator({**schemas["SpreadsheetImportPreview"], "components": spec["components"]}).validate(self.preview(fmt, sheet_index=2))

    def test_edit_at_the_workbook_chart_limit(self):
        book = self.book()
        record = self.preview(sheet_index=2)["charts"][0]
        receipt = book.apply("edit", "Sheet1", cells=[{"row": 1, "col": 1, "source": "1"}],
                             charts=[dict(record, name=f"C{n}") for n in range(63)])
        self.assertEqual(receipt["changed_charts"], 63)
        receipt = book.apply("edit", "Sheet1", cells=[{"row": 1, "col": 1, "source": "2"}],
                             charts=[dict(record, name=name) for name in ("X", "Y", "Z")])
        self.assertEqual((receipt["changed_cells"], receipt["changed_charts"], receipt["charts_left_out"]), (1, 1, ["Y", "Z"]))
        book.refresh()
        self.assertEqual(len(book.charts()), 64)
        self.assertEqual(book.read(r1=1, c1=1, r2=1, c2=1)["cells"][0]["source"], "2")
        book.apply("undo")
        book.refresh()
        self.assertEqual(len(book.charts()), 63)
        self.assertEqual(book.read(r1=1, c1=1, r2=1, c2=1)["cells"][0]["source"], "1")
        self.assertNotIn("X", [item["name"] for item in book.charts()])

    def test_edit_without_charts_is_unchanged(self):
        book = self.book()
        preview = self.preview(data=xlsx_with_charts({"Data": data_rows()}, []))
        receipt = book.import_prepared(preview, "Sheet1")
        self.assertEqual(receipt["changed_charts"], 0)
        self.assertNotIn("charts_left_out", receipt)
        self.assertEqual(preview["charts"], [])


class SelectionChartLimitTests(unittest.TestCase):
    """What a one-sheet preview may not let charts do: use up the chart limit with charts it then drops, or
    take the room the cells need."""
    def test_charts_the_selection_drops_do_not_count_toward_the_limit(self):
        elsewhere = [{"sheet": "First", "name": f"Elsewhere {n}", "xml": column_chart("Second")} for n in range(1, 65)]
        eligible = [{"sheet": "First", "name": "Eligible", "xml": column_chart("First")}]
        xlsx = xlsx_with_charts({"First": data_rows(), "Second": data_rows()}, elsewhere + eligible)
        shared = [{"sheet": "First", "name": "Elsewhere 1", "xml": ods_column_chart("Second")}]
        shared += [{"sheet": "First", "name": f"Elsewhere {n}", "missing": True, "target": "./Object 1"} for n in range(2, 65)]
        ods = ods_with_charts({"First": ods_data_rows(), "Second": ods_data_rows()},
                              shared + [{"sheet": "First", "name": "Eligible", "xml": ods_column_chart("First")}])
        for fmt, data, function in (("xlsx", xlsx, xlsx_preview), ("ods", ods, ods_preview)):
            with self.subTest(fmt):
                preview = function({"file_base64": b64(data), "scope": "selection", "sheet_index": 1})
                self.assertEqual([item["name"] for item in preview["charts"]], ["Eligible"])
                skipped = {entry["detail"]: entry["count"] for entry in entries(preview, "chart.skipped")}
                self.assertEqual(skipped, {"source": 64})

    def test_charts_never_take_the_room_the_cells_need(self):
        text = "x" * 300
        rows = "".join(f'<row r="{row}">' + "".join(f'<c r="{column}{row}" t="inlineStr"><is><t>{text}</t></is></c>'
                                                    for column in "ABCDEFGH") + "</row>" for row in range(1, 1201))
        series = [{"cat": "Data!$A$2:$A$8", "val": "Data!$B$2:$B$8", "name_text": "S" * 250} for _ in range(60)]
        charts = [{"sheet": "Data", "name": f"Chart {n}", "xml": chart_xml("bar", "col", "clustered", series)} for n in range(1, 33)]
        plain = xlsx_preview({"file_base64": b64(xlsx_with_charts({"Data": rows}, [])), "scope": "selection"})
        self.assertGreater(len(json.dumps(plain)), 3 * 1024 * 1024)
        preview = xlsx_preview({"file_base64": b64(xlsx_with_charts({"Data": rows}, charts)), "scope": "selection"})
        self.assertLessEqual(len(json.dumps(preview, separators=(",", ":")).encode()), 4 * 1024 * 1024)
        for key in ("cells", "can_import", "stored_cells"):
            self.assertEqual(preview[key], plain[key], key)
        self.assertTrue(preview["can_import"])
        kept = [item["name"] for item in preview["charts"]]
        self.assertEqual(kept, [f"Chart {n}" for n in range(1, len(kept) + 1)])
        self.assertLess(len(kept), 32)
        skipped = {entry["detail"]: entry["count"] for entry in entries(preview, "chart.skipped")}
        self.assertEqual(skipped, {"workbook_limit": 32 - len(kept)})
        with LocalService() as client, tempfile.TemporaryDirectory() as folder:
            with client.open(Path(folder) / "room.dhwb", create=True) as book:
                receipt = book.import_prepared(preview, "Sheet1")
                self.assertEqual(receipt["changed_charts"], len(kept))


class ChartFixtureTests(unittest.TestCase):
    def test_committed_two_sheet_fixture_is_what_its_generator_writes(self):
        import generate_chart_workbook
        self.assertEqual((FIXTURES / "charts-two-sheets.xlsx").read_bytes(), generate_chart_workbook.build())
        self.assertEqual((FIXTURES / "charts-one-sheet.xlsx").read_bytes(), generate_chart_workbook.build(1))


class SelectionRollbackTests(unittest.TestCase):
    def test_failing_change_handler_rolls_back_charts(self):
        source = "def refuse(ctx, changes):\n    raise ValueError('binding was called')\n"
        with tempfile.TemporaryDirectory() as temp:
            book = Workbook(Path(temp) / "run.dhwb", create=True)
            self.addCleanup(book.close)

            def apply(action, **args):
                return book.mutate({"action": action, "operation_id": str(uuid.uuid4()), "expected_state": book.state(), **args})

            apply("sheet_rename", sheet="Sheet1", name="Data")
            apply("module_save", name="rules", source=source, macros=[], fingerprint=fingerprint(book.db))
            apply("binding_save", event="cell_change", scope={"kind": "range", "reference": "'Data'!A1:Z40"}, module="rules",
                  function="refuse", required=True, fingerprint=fingerprint(book.db))
            book.approved_code = frozenset([activation(book)["activation_key"]])
            records = xlsx_preview({"file_base64": b64(sample_like()), "sheet_index": 2})["charts"]
            self.assertEqual(len(records), 1)
            with self.assertRaises(EngineError) as caught:
                apply("edit", sheet="Data", cells=[{"row": 1, "col": 1, "source": "1"}], charts=records)
            self.assertEqual(caught.exception.body["code"], "BINDING_FAILED")
            self.assertEqual(caught.exception.body["outcome"], "rolled_back")
            self.assertEqual(charts_module.objects(book), [])
            self.assertIsNone(book.raw(book.sheet("Data"), 1, 1))


if __name__ == "__main__":
    unittest.main()
