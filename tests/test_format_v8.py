# SPDX-License-Identifier: GPL-3.0-only
"""Native format 8: charts stored in the dh_objects catalog."""

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import uuid

from datahorizon import charts
from datahorizon.client import LocalService
from datahorizon.engine import EngineError, Workbook
from datahorizon.workbook_code import fingerprint


def format_version(book):
    return book.db.execute("SELECT format_version FROM dh_meta").fetchone()[0]


def mutate(book, **request):
    return book.mutate(dict(operation_id=str(uuid.uuid4()), expected_state=book.state(), **request))


def chart_spec():
    return {"spec_version": 1, "type": "column",
            "series": [{"name": {"text": "Sales"}, "values": {"source": {"kind": "range", "sheet": "Sheet1", "ref": "B2:B4"}}}]}


def raw_payload(path):
    with closing(sqlite3.connect(path)) as db:
        return db.execute("SELECT payload FROM dh_objects WHERE namespace='chart'").fetchone()[0]


class FormatV8Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "v8.dhwb"

    def new_book(self):
        book = Workbook(self.path, create=True)
        self.addCleanup(book.close)
        return book

    def reopen(self):
        book = Workbook(self.path)
        self.addCleanup(book.close)
        return book

    def raw(self, statement, *args):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute(statement, args)
            db.commit()

    def book_with_chart(self):
        book = self.new_book()
        receipt = mutate(book, action="chart_create", sheet="Sheet1", spec=chart_spec())
        book.close()
        return receipt["chart_id"]

    def test_first_chart_write_bumps_to_v8_and_reopens(self):
        book = self.new_book()
        self.assertEqual(format_version(book), 1)
        chart_id = mutate(book, action="chart_create", sheet="Sheet1", spec=chart_spec())["chart_id"]
        self.assertEqual(format_version(book), 8)
        book.close()
        reopened = self.reopen()
        self.assertEqual(format_version(reopened), 8)
        self.assertEqual(reopened.info()["format_version"], 8)
        listed = charts.read(reopened, {"expected_state": reopened.state()})["charts"]
        self.assertEqual([item["id"] for item in listed], [chart_id])

    def test_undo_of_the_first_chart_keeps_v8(self):
        book = self.new_book()
        mutate(book, action="chart_create", sheet="Sheet1", spec=chart_spec())
        mutate(book, action="undo")
        self.assertEqual(format_version(book), 8)
        self.assertEqual(charts.read(book, {"expected_state": book.state()})["charts"], [])

    def test_v7_file_with_a_chart_row_is_invalid_format(self):
        book = self.new_book()
        mutate(book, action="module_save", fingerprint=fingerprint(book.db), name="m", source="def f(ctx):\n    return 1\n", macros=[])
        self.assertEqual(format_version(book), 5)
        sheet = book.info()["sheets"][0]["id"]
        payload = charts.normalise_spec(book, chart_spec())
        book.close()
        stored = {"name": "Chart 1", "anchor": {"row": 1, "col": 1}, "offset": {"x": 0, "y": 0},
                  "size": {"width": 480, "height": 300}, "z": 0, "spec_version": 1, "spec": payload}
        self.raw("UPDATE dh_meta SET format_version=7")
        self.raw("INSERT INTO dh_objects VALUES('chart',?,?,?)", str(uuid.uuid4()), sheet, json.dumps(stored))
        with self.assertRaises(EngineError) as raised:
            Workbook(self.path)
        self.assertEqual(raised.exception.body["code"], "INVALID_FORMAT")
        self.raw("UPDATE dh_meta SET format_version=8")
        reopened = self.reopen()
        self.assertEqual(len(charts.read(reopened, {"expected_state": reopened.state()})["charts"]), 1)

    def test_v9_is_rejected_on_open(self):
        self.new_book().close()
        self.raw("UPDATE dh_meta SET format_version=9")
        with self.assertRaises(EngineError) as raised:
            Workbook(self.path)
        self.assertEqual(raised.exception.body["code"], "FORMAT_VERSION")

    def test_capabilities_advertise_v8(self):
        with LocalService() as client:
            capabilities = client.request("/v1/capabilities")
            self.assertEqual((capabilities["format_version"], capabilities["supported_format_versions"]),
                             (8, [1, 2, 3, 4, 5, 6, 7, 8]))
            self.assertIs(capabilities["charts"], True)
            self.assertEqual((capabilities["max_charts"], capabilities["chart_spec_version"]), (64, 1))
            self.assertTrue({"chart_create", "chart_update", "chart_delete"} <= set(capabilities["operations"]))
            route = client.request("/openapi.json")["paths"]["/v1/workbooks/{workbook}/charts"]["post"]
            self.assertEqual(route["requestBody"]["content"]["application/json"]["schema"]["$ref"], "#/components/schemas/ChartsReadRequest")
            with client.open(self.path, create=True) as book:
                chart_id = book.apply("chart_create", spec=chart_spec())["chart_id"]
                book.refresh()
                listed = client.request(book.base + "/charts", {"expected_state": book.state})
                self.assertEqual([item["id"] for item in listed["charts"]], [chart_id])
                self.assertEqual(book.refresh()["format_version"], 8)

    def test_direct_edit_to_an_unreadable_spec_opens_and_reports(self):
        self.book_with_chart()
        self.raw("UPDATE dh_objects SET payload=json_set(payload,'$.spec.type','radar') WHERE namespace='chart'")
        book = self.reopen()
        listed = charts.read(book, {"expected_state": book.state()})["charts"]
        self.assertEqual(listed[0]["issue"], "INVALID_SPEC")
        self.assertEqual(json.loads(raw_payload(self.path))["spec"]["type"], "radar")

    def test_direct_edit_to_a_bad_envelope_is_invalid_format(self):
        self.book_with_chart()
        self.raw("UPDATE dh_objects SET payload=json_set(payload,'$.size.width',10) WHERE namespace='chart'")
        with self.assertRaises(EngineError) as raised:
            Workbook(self.path)
        self.assertEqual(raised.exception.body["code"], "INVALID_FORMAT")


if __name__ == "__main__":
    unittest.main()
