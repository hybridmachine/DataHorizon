# SPDX-License-Identifier: GPL-3.0-only
"""Chart sources and anchors follow row, column, sheet and Cut-and-paste changes."""
import pathlib
import tempfile
import unittest
import uuid

from datahorizon import chart_resolve, charts
from datahorizon.engine import EngineError, Workbook
from test_charts import spec


class ChartStructure(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = pathlib.Path(self.tmp.name) / "structure.dhwb"
        self.book = Workbook(self.path, create=True)
        self.addCleanup(lambda: self.book.close())
        self.chart = self.apply("chart_create", sheet="Sheet1", spec=spec(), anchor={"row": 3, "col": 4})["chart_id"]

    def apply(self, action, **args):
        return self.book.mutate(dict(action=action, operation_id=str(uuid.uuid4()), expected_state=self.book.state(), **args))

    def read_chart(self, chart_id=None):
        return charts.read(self.book, dict(expected_state=self.book.state(), chart_id=chart_id or self.chart))["charts"][0]

    def values(self, chart_id=None):
        return self.read_chart(chart_id)["spec"]["series"][0]["values"]["source"]

    def test_rows_inserted_above_shift_sources_and_anchor(self):
        receipt = self.apply("rows_insert", sheet="Sheet1", start=1, count=2)
        chart = self.read_chart()
        self.assertEqual(chart["spec"]["series"][0]["values"]["source"]["ref"], "B4:B15")
        self.assertEqual(chart["spec"]["categories"]["source"]["ref"], "A4:A15")
        self.assertEqual(chart["anchor"], {"row": 5, "col": 4})
        self.assertNotIn("broken_charts", receipt)

    def test_rows_inserted_inside_grow_the_source(self):
        self.apply("rows_insert", sheet="Sheet1", start=5, count=1)
        self.assertEqual(self.values()["ref"], "B2:B14")
        self.assertEqual(self.read_chart()["anchor"], {"row": 3, "col": 4})

    def test_rows_deleted_inside_shrink_the_source(self):
        self.apply("rows_delete", sheet="Sheet1", start=5, count=3)
        self.assertEqual(self.values()["ref"], "B2:B10")

    def test_deleting_every_source_row_breaks_the_source(self):
        receipt = self.apply("rows_delete", sheet="Sheet1", start=2, count=12)
        self.assertEqual(receipt["broken_charts"], [self.chart])
        source = self.read_chart()["spec"]["series"][0]["values"]["source"]
        self.assertEqual((source["broken"], source["ref"]), (True, None))
        self.assertEqual(self.read_chart()["anchor"], {"row": 2, "col": 4})
        # Already broken: a further edit neither moves nor reports it again.
        again = self.apply("rows_insert", sheet="Sheet1", start=1, count=1)
        self.assertNotIn("broken_charts", again)

    def test_anchor_inside_a_deleted_span_moves_to_start(self):
        self.apply("rows_delete", sheet="Sheet1", start=2, count=5)
        self.assertEqual(self.read_chart()["anchor"], {"row": 2, "col": 4})

    def test_columns_follow_the_same_rules(self):
        self.apply("columns_insert", sheet="Sheet1", start=1, count=2)
        chart = self.read_chart()
        self.assertEqual(chart["spec"]["series"][0]["values"]["source"]["ref"], "D2:D13")
        self.assertEqual(chart["anchor"], {"row": 3, "col": 6})
        receipt = self.apply("columns_delete", sheet="Sheet1", start=4, count=1)
        self.assertEqual(receipt["broken_charts"], [self.chart])
        self.assertEqual(self.read_chart()["spec"]["categories"]["source"]["ref"], "C2:C13")

    def test_edit_on_another_sheet_leaves_the_chart_alone(self):
        self.apply("sheet_add", name="Other")
        before = self.read_chart()
        receipt = self.apply("rows_insert", sheet="Other", start=1, count=2)
        self.assertEqual(self.read_chart(), before)
        self.assertNotIn("broken_charts", receipt)

    def test_deleting_the_data_sheet_breaks_sources_on_other_sheets(self):
        self.apply("sheet_add", name="Sheet2")
        other = self.apply("chart_create", sheet="Sheet2", spec=spec())["chart_id"]
        receipt = self.apply("sheet_delete", sheet="Sheet2")
        self.assertNotIn("broken_charts", receipt)  # its own chart went with it
        self.assertEqual(self.chart in [item["id"] for item in charts.read(
            self.book, dict(expected_state=self.book.state()))["charts"]], True)
        self.assertNotIn(other, [item["id"] for item in charts.read(self.book, dict(expected_state=self.book.state()))["charts"]])
        self.apply("sheet_add", name="Sheet3")
        mine = self.apply("chart_create", sheet="Sheet3", spec=spec(), name="Over")["chart_id"]
        self.apply("chart_update", chart_id=mine, spec=spec(series=[{
            "name": {"text": "s"}, "values": {"source": {"kind": "range", "sheet": "Sheet1", "ref": "B2:B13"}}, "x": None}]))
        receipt = self.apply("sheet_delete", sheet="Sheet1")
        self.assertEqual(receipt["broken_charts"], [mine])
        stored = charts.find(self.book, mine)[1]["spec"]["series"][0]["values"]["source"]
        self.assertEqual(stored, {"kind": "range", "sheet_id": None, "row1": None, "col1": None, "row2": None, "col2": None})
        self.assertEqual(self.values(mine)["broken"], True)

    def test_deleting_the_owning_sheet_deletes_its_charts(self):
        self.apply("sheet_add", name="Sheet2")
        self.apply("sheet_delete", sheet="Sheet1")
        count = self.book.db.execute("SELECT count(*) FROM dh_objects WHERE namespace='chart'").fetchone()[0]
        self.assertEqual(count, 0)

    def test_cut_moves_a_wholly_contained_source_and_undo_restores_it(self):
        self.apply("move", sheet="Sheet1", r1=1, c1=2, r2=14, c2=3, row=1, col=6,
                   cut_state=self.book.state())
        chart = self.read_chart()
        self.assertEqual(chart["spec"]["series"][0]["values"]["source"]["ref"], "F2:F13")
        self.assertEqual(chart["spec"]["categories"]["source"]["ref"], "A2:A13")  # column A is outside the cut
        self.assertEqual(chart["anchor"], {"row": 3, "col": 4})
        self.apply("undo")
        self.assertEqual(self.values()["ref"], "B2:B13")

    def test_cut_leaves_a_partly_overlapping_source(self):
        self.apply("move", sheet="Sheet1", r1=1, c1=2, r2=5, c2=2, row=1, col=6,
                   cut_state=self.book.state())
        self.assertEqual(self.values()["ref"], "B2:B13")

    def test_duplicate_sheet_does_not_copy_charts(self):
        self.apply("sheet_duplicate", sheet="Sheet1", name="Copy")
        self.assertEqual(len(charts.read(self.book, dict(expected_state=self.book.state()))["charts"]), 1)

    def test_broken_source_survives_reopen(self):
        self.apply("rows_delete", sheet="Sheet1", start=2, count=12)
        self.book.close()
        self.book = Workbook(self.path)
        source = self.values()
        self.assertEqual((source["broken"], source["ref"]), (True, None))

    def test_unreadable_specification_is_left_untouched_but_anchor_moves(self):
        payload = charts.find(self.book, self.chart)[1]
        payload["spec"] = {"spec_version": 1, "type": "bogus", "series": [{"values": {"source": {"row1": 2}}}]}
        self.book.db.execute("UPDATE dh_objects SET payload=json(?) WHERE namespace='chart'", (__import__("json").dumps(payload),))
        self.apply("rows_insert", sheet="Sheet1", start=1, count=2)
        after = charts.find(self.book, self.chart)[1]
        self.assertEqual(after["spec"], payload["spec"])
        self.assertEqual(after["anchor"], {"row": 5, "col": 4})


def ref_source(ref):
    return {"kind": "range", "sheet": "Sheet1", "ref": ref}


class ChartOverLimit(unittest.TestCase):
    """A structural edit can push a stored chart past a size limit; it stays readable and maintained."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = pathlib.Path(self.tmp.name) / "limits.dhwb"
        self.book = Workbook(self.path, create=True)
        self.addCleanup(lambda: self.book.close())

    def apply(self, action, **args):
        return self.book.mutate(dict(action=action, operation_id=str(uuid.uuid4()), expected_state=self.book.state(), **args))

    def refused(self, action, **args):
        with self.assertRaises(EngineError) as caught:
            self.apply(action, **args)
        return caught.exception

    def read_chart(self, chart_id):
        return charts.read(self.book, dict(expected_state=self.book.state(), chart_id=chart_id))["charts"][0]

    def resolve(self, chart_id):
        return chart_resolve.resolve(self.book, charts.find(self.book, chart_id)[1], self.book.state())

    def resolve_refused(self, chart_id):
        with self.assertRaises(EngineError) as caught:
            self.resolve(chart_id)
        return caught.exception

    def pie(self):
        cells = []
        for row in range(1, 1001):
            cells += [{"row": row, "col": 1, "source": f"P{row}"}, {"row": row, "col": 2, "source": str(row)}]
        self.apply("edit", sheet="Sheet1", cells=cells)
        pie = {"spec_version": 1, "type": "pie", "categories": {"source": ref_source("A1:A1000")},
               "series": [{"name": {"text": "Share"}, "values": {"source": ref_source("B1:B1000")}}]}
        return self.apply("chart_create", sheet="Sheet1", spec=pie)["chart_id"]

    def test_an_insert_that_pushes_a_pie_past_its_point_limit(self):
        chart_id = self.pie()
        before = self.read_chart(chart_id)
        receipt = self.apply("rows_insert", sheet="Sheet1", start=10, count=2)
        chart = self.read_chart(chart_id)
        self.assertIsNone(chart["issue"])
        self.assertEqual((before["over_limit"], chart["over_limit"]), (None, "LIMIT_PIE_POINTS"))
        self.assertEqual(receipt["charts_over_limit"], [chart_id])
        self.assertNotIn("broken_charts", receipt)
        self.assertEqual(chart["spec"]["categories"]["source"]["ref"], "A1:A1002")
        error = self.resolve_refused(chart_id)
        self.assertEqual((error.body["code"], error.status), ("LIMIT_PIE_POINTS", 400))
        self.assertIn("now holds 1002 cells", error.body["message"])
        # Still a managed chart: placement edits are accepted, and a patch re-validates the limits.
        self.apply("chart_update", chart_id=chart_id, name="Shares")
        self.assertEqual(code_of(self.refused("chart_update", chart_id=chart_id, spec_patch={"title": {"text": "T"}})),
                         "LIMIT_PIE_POINTS")
        # An edit that keeps it over the limit does not list it again.
        again = self.apply("rows_insert", sheet="Sheet1", start=1, count=1)
        self.assertNotIn("charts_over_limit", again)
        self.assertEqual(self.read_chart(chart_id)["spec"]["series"][0]["values"]["source"]["ref"], "B2:B1003")
        # A later delete still moves its sources; once it fits again it resolves.
        self.apply("rows_delete", sheet="Sheet1", start=1, count=5)
        chart = self.read_chart(chart_id)
        self.assertEqual(chart["spec"]["categories"]["source"]["ref"], "A1:A998")
        self.assertEqual(chart["spec"]["series"][0]["values"]["source"]["ref"], "B1:B998")
        self.assertIsNone(chart["over_limit"])
        self.assertEqual(len(self.resolve(chart_id)["resolved"]["series"][0]["values"]), 998)
        self.apply("chart_update", chart_id=chart_id, spec_patch={"title": {"text": "T"}})
        self.assertEqual(self.read_chart(chart_id)["spec"]["title"], {"text": "T"})

    def test_an_insert_that_pushes_a_series_past_the_point_limit_and_reopen(self):
        line = {"spec_version": 1, "type": "line", "series": [{"name": {"text": "S"}, "values": {"source": ref_source("B1:B10000")}}]}
        chart_id = self.apply("chart_create", sheet="Sheet1", spec=line)["chart_id"]
        receipt = self.apply("rows_insert", sheet="Sheet1", start=5000, count=1)
        self.assertIsNone(self.read_chart(chart_id)["issue"])
        self.assertEqual(receipt["charts_over_limit"], [chart_id])
        self.book.close()
        self.book = Workbook(self.path)
        chart = self.read_chart(chart_id)
        self.assertEqual((chart["issue"], chart["over_limit"]), (None, "LIMIT_POINTS"))
        self.assertEqual(chart["spec"]["series"][0]["values"]["source"]["ref"], "B1:B10001")
        error = self.resolve_refused(chart_id)
        self.assertEqual((error.body["code"], error.status), ("LIMIT_POINTS", 400))
        self.assertIn("now holds 10001 cells", error.body["message"])
        self.apply("chart_update", chart_id=chart_id, spec={**line, "title": {"text": "Fits"}})
        self.assertIsNone(self.read_chart(chart_id)["over_limit"])


def code_of(error):
    return error.body["code"]


if __name__ == "__main__":
    unittest.main()
