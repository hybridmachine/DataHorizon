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


if __name__ == "__main__":
    unittest.main()
