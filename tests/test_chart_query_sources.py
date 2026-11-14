# SPDX-License-Identifier: GPL-3.0-only
"""Tests for saved-query chart sources in specifications."""

import copy
import pathlib
import tempfile
import unittest
import uuid

from datahorizon import charts
from datahorizon.engine import EngineError, Workbook
from datahorizon.query_views import preview

from test_charts import code, payload, spec


def query_source(name, column):
    return {"kind": "query", "query": name, "column": column}


def source_spec(values, **extra):
    return {"spec_version": 1, "type": "column", "series": [{"name": {"text": "s"}, "values": {"source": values}}],
            **extra}


class Spec(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.book = Workbook(pathlib.Path(self.tmp.name) / "q.dhwb", create=True)
        self.addCleanup(self.book.close)
        self.apply("table_create", name="Sales", columns=[
            dict(name="Month", type="text"), dict(name="Units", type="integer")])
        self.apply("table_insert", table="Sales", rows=[dict(Month=f"M{n}", Units=n) for n in range(1, 13)])
        self.query_id = self.save("Top", "SELECT Month, Units FROM Sales ORDER BY Units DESC")
        self.dupe_id = self.save("Dupe", "SELECT Units AS x, Units AS x FROM Sales")

    def apply(self, action, **args):
        return self.book.mutate(dict(action=action, operation_id=str(uuid.uuid4()),
                                     expected_state=self.book.state(), **args))

    def save(self, name, sql):
        review = preview(self.book, dict(name=name, sql=sql, expected_state=self.book.state()))
        return self.apply("query_save", **review["source_request"], review_digest=review["review_digest"])["query"]["id"]

    def refused(self, value):
        with self.assertRaises(EngineError) as caught:
            charts.normalise_spec(self.book, value)
        return caught.exception

    def test_normalise_stores_query_identity_and_column_name(self):
        out = charts.normalise_spec(self.book, source_spec(query_source("top", "Units")))
        self.assertEqual(out["series"][0]["values"]["source"],
                         {"kind": "query", "query_id": self.query_id, "column": "Units"})
        out = charts.normalise_spec(self.book, source_spec(query_source(self.query_id, "Month")))
        self.assertEqual(out["series"][0]["values"]["source"]["query_id"], self.query_id)
        self.assertIsNone(charts.validate_stored(payload(out)))

    def test_refusals(self):
        error = self.refused(source_spec(query_source("Nope", "Units")))
        self.assertEqual((code(error), error.status), ("QUERY_NOT_FOUND", 404))
        self.assertEqual(code(self.refused(source_spec(query_source("Top", "Nope")))), "COLUMN_NOT_FOUND")
        self.assertEqual(code(self.refused(source_spec(query_source("Dupe", "x")))), "COLUMN_NOT_FOUND")
        self.assertEqual(code(self.refused(source_spec(dict(query_source("Top", "Units"), extra=1)))), "INVALID_SPEC")
        named = source_spec(query_source("Top", "Units"))
        named["series"][0]["name"] = {"source": query_source("Top", "Month")}
        self.assertEqual(code(self.refused(named)), "INVALID_SPEC")

    def test_stored_validation_and_is_broken(self):
        out = charts.normalise_spec(self.book, source_spec(query_source("Top", "Units")))
        source = out["series"][0]["values"]["source"]
        self.assertEqual(charts.source_kind(source), "query")
        self.assertFalse(charts.is_broken(source))
        source["query_id"] = None
        self.assertIsNone(charts.validate_stored(payload(out)))
        self.assertTrue(charts.is_broken(source))
        for bad in ({"kind": "query", "query_id": 3, "column": "Units"},
                    {"kind": "query", "query_id": None, "column": ""},
                    {"kind": "query", "query_id": None, "column": None},
                    {"kind": "query", "query_id": None},
                    {"kind": "query", "query_id": None, "column": "Units", "extra": 1}):
            out["series"][0]["values"]["source"] = bad
            self.assertEqual(charts.validate_stored(payload(out)), "INVALID_SPEC", bad)

    def test_describe_marks_a_missing_column_broken(self):
        out = charts.normalise_spec(self.book, source_spec(query_source("Top", "Units")))

        def described(change=None):
            changed = copy.deepcopy(out)
            if change:
                changed["series"][0]["values"]["source"].update(change)
            return charts.describe(self.book.db, "s", "c", payload(changed))["spec"]["series"][0]["values"]["source"]

        self.assertEqual((described()["query"], described()["broken"]), ("Top", False))
        self.assertTrue(described({"column": "Gone"})["broken"])
        self.assertTrue(described({"query_id": str(uuid.uuid4())})["broken"])
        self.assertEqual((described({"query_id": None})["query"], described({"query_id": None})["broken"]), (None, True))
        self.assertTrue(described({"query_id": self.dupe_id, "column": "x"})["broken"])
        self.apply("query_rename", query=self.query_id, name="Renamed")
        self.assertEqual(described()["query"], "Renamed")

    def test_spec_patch_keeps_a_query_source(self):
        chart_id = self.apply("chart_create", spec=source_spec(query_source("Top", "Units")))["chart_id"]
        self.apply("query_rename", query=self.query_id, name="Renamed")
        self.apply("chart_update", chart_id=chart_id, spec_patch={"title": {"text": "T"}})
        stored = charts.find(self.book, chart_id)[1]["spec"]
        self.assertEqual(stored["series"][0]["values"]["source"],
                         {"kind": "query", "query_id": self.query_id, "column": "Units"})

    def test_query_sources_add_nothing_to_size_checks(self):
        out = charts.normalise_spec(self.book, source_spec(query_source("Top", "Units")))
        self.assertIsNone(charts.limit_error(out))

    def test_mixed_kinds_in_one_chart(self):
        scatter = {"spec_version": 1, "type": "scatter", "series": [{
            "name": {"text": "s"},
            "values": {"source": {"kind": "table", "table": "Sales", "column": "Units"}},
            "x": {"source": query_source("Top", "Units")}}]}
        out = charts.normalise_spec(self.book, scatter)
        self.assertEqual([out["series"][0][key]["source"]["kind"] for key in ("values", "x")], ["table", "query"])
        column = spec(categories={"source": {"kind": "range", "sheet": "Sheet1", "ref": "A2:A13"}})
        column["series"] = [{"name": {"text": "s"},
                             "values": {"source": {"kind": "table", "table": "Sales", "column": "Units"}}}]
        out = charts.normalise_spec(self.book, column)
        self.assertEqual((out["categories"]["source"]["kind"], out["series"][0]["values"]["source"]["kind"]),
                         ("range", "table"))
        self.assertIsNone(charts.validate_stored(payload(out)))
        chart_id = self.apply("chart_create", spec=column)["chart_id"]
        self.apply("chart_update", chart_id=chart_id, spec_patch={"title": {"text": "T"}})
        self.assertEqual(charts.find(self.book, chart_id)[1]["spec"]["categories"]["source"]["kind"], "range")


    # Following the saved query, and mixed sources.

    def make_chart(self):
        self.chart = self.apply("chart_create", spec=source_spec(query_source("Top", "Units")))["chart_id"]
        return self.chart

    def source(self, chart_id=None):
        return charts.find(self.book, chart_id or self.chart)[1]["spec"]["series"][0]["values"]["source"]

    def test_deleting_the_saved_query_breaks_its_sources(self):
        self.make_chart()
        receipt = self.apply("query_delete", query=self.query_id)
        self.assertEqual(receipt["broken_charts"], [self.chart])
        self.assertEqual(self.source(), {"kind": "query", "query_id": None, "column": "Units"})
        described = charts.read(self.book, dict(expected_state=self.book.state()))["charts"][0]
        self.assertTrue(described["spec"]["series"][0]["values"]["source"]["broken"])

    def test_deleting_another_saved_query_breaks_nothing(self):
        self.make_chart()
        receipt = self.apply("query_delete", query=self.dupe_id)
        self.assertNotIn("broken_charts", receipt)
        self.assertEqual(self.source()["query_id"], self.query_id)

    def test_renaming_the_saved_query_breaks_nothing(self):
        self.make_chart()
        receipt = self.apply("query_rename", query=self.query_id, name="Renamed")
        self.assertNotIn("broken_charts", receipt)
        self.assertEqual(self.source()["query_id"], self.query_id)

    def test_saving_a_query_touches_no_chart(self):
        self.make_chart()
        before = charts.find(self.book, self.chart)[1]
        review = preview(self.book, dict(name="Third", sql="SELECT Units FROM Sales", expected_state=self.book.state()))
        receipt = self.apply("query_save", **review["source_request"], review_digest=review["review_digest"])
        self.assertNotIn("broken_charts", receipt)
        self.assertEqual(charts.find(self.book, self.chart)[1], before)

    def test_a_refused_table_drop_leaves_the_chart_alone(self):
        chart_id = self.apply("chart_create", spec=source_spec(
            {"kind": "table", "table": "Sales", "column": "Units"}))["chart_id"]
        before = charts.find(self.book, chart_id)[1]
        with self.assertRaises(EngineError) as caught:
            self.apply("table_drop", table="Sales", allow_data_loss=True)
        self.assertEqual(code(caught), "DEPENDENCY_BLOCKED")
        self.assertEqual(charts.find(self.book, chart_id)[1], before)

    def test_a_broken_saved_query_source_survives_reopen_and_blocks_spec_patch(self):
        self.make_chart()
        self.apply("query_delete", query=self.query_id)
        path = self.book.path
        self.book.close()
        self.book = Workbook(path)
        self.assertIsNone(self.source()["query_id"])
        with self.assertRaises(EngineError) as caught:
            self.apply("chart_update", chart_id=self.chart, spec_patch={"title": {"text": "T"}})
        self.assertEqual((code(caught), caught.exception.status), ("SOURCE_BROKEN", 409))

    def test_charts_without_the_query_are_not_rewritten(self):
        self.make_chart()
        other = self.apply("chart_create", spec=spec())["chart_id"]
        before = charts.find(self.book, other)[1]
        receipt = self.apply("query_delete", query=self.query_id)
        self.assertEqual(receipt["changed_charts"], 1)
        self.assertEqual(charts.find(self.book, other)[1], before)

    def test_a_series_name_may_not_be_a_table_or_query_source(self):
        out = charts.normalise_spec(self.book, source_spec(query_source("Top", "Units")))
        for named in (query_source("Top", "Month"), {"kind": "table", "table": "Sales", "column": "Month"}):
            stored = copy.deepcopy(out)
            stored["series"][0]["name"] = {"source": named}
            self.assertEqual(charts.validate_stored(payload(stored)), "INVALID_SPEC")
        stored = copy.deepcopy(out)
        stored["series"][0]["name"] = {"source": {"kind": "query", "query_id": self.query_id, "column": "Month"}}
        self.assertEqual(charts.validate_stored(payload(stored)), "INVALID_SPEC")
        stored["series"][0]["name"] = {"source": {"kind": "table", "table_id": "t", "column_id": "c"}}
        self.assertEqual(charts.validate_stored(payload(stored)), "INVALID_SPEC")

    def test_a_chart_with_a_broken_query_source_refuses_spec_patch(self):
        chart_id = self.make_chart()
        sheet_id, stored = charts.find(self.book, chart_id)
        stored["spec"]["series"][0]["values"]["source"]["query_id"] = None
        self.book.db.execute("BEGIN")
        charts.store(self.book, sheet_id, chart_id, stored)
        self.book.db.execute("COMMIT")
        with self.assertRaises(EngineError) as caught:
            self.apply("chart_update", chart_id=chart_id, spec_patch={"title": {"text": "T"}})
        self.assertEqual((code(caught), caught.exception.status), ("SOURCE_BROKEN", 409))

    def mixed(self, sheet):
        column = spec(categories={"source": {"kind": "range", "sheet": sheet, "ref": "A2:A13"}})
        column["series"] = [
            {"name": {"text": "t"}, "values": {"source": {"kind": "table", "table": "Sales", "column": "Units"}}},
            {"name": {"text": "q"}, "values": {"source": query_source("Top", "Units")}}]
        return self.apply("chart_create", spec=column)["chart_id"]

    def parts(self, chart_id):
        stored = charts.find(self.book, chart_id)[1]["spec"]
        return (stored["categories"]["source"], stored["series"][0]["values"]["source"],
                stored["series"][1]["values"]["source"])

    def test_a_mixed_chart_follows_only_its_range_source(self):
        chart_id = self.mixed("Sheet1")
        _, table_source, query_source_ = self.parts(chart_id)
        self.apply("rows_insert", sheet="Sheet1", start=1, count=2)
        moved, table_after, query_after = self.parts(chart_id)
        self.assertEqual((moved["row1"], moved["row2"]), (4, 15))
        self.assertEqual((table_after, query_after), (table_source, query_source_))
        self.apply("move", sheet="Sheet1", r1=1, c1=1, r2=15, c2=1, row=1, col=6, cut_state=self.book.state())
        moved, table_after, query_after = self.parts(chart_id)
        self.assertEqual((moved["col1"], moved["col2"]), (6, 6))
        self.assertEqual((table_after, query_after), (table_source, query_source_))

    def test_deleting_the_range_sheet_breaks_only_the_range_source(self):
        self.apply("sheet_add", name="Sheet2")
        chart_id = self.mixed("Sheet2")
        _, table_source, query_source_ = self.parts(chart_id)
        receipt = self.apply("sheet_delete", sheet="Sheet2")
        self.assertEqual(receipt["broken_charts"], [chart_id])
        broken, table_after, query_after = self.parts(chart_id)
        self.assertTrue(charts.is_broken(broken))
        self.assertEqual((table_after, query_after), (table_source, query_source_))


if __name__ == "__main__":
    unittest.main()
