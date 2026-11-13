# SPDX-License-Identifier: GPL-3.0-only
"""Chart sources and anchors follow row, column, sheet and Cut-and-paste changes."""
import pathlib
import tempfile
import unittest
import uuid

from datahorizon import charts
from datahorizon.engine import Workbook
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


if __name__ == "__main__":
    unittest.main()
