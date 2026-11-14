# SPDX-License-Identifier: GPL-3.0-only
"""Tests for table-column chart sources in specifications."""

import copy
import pathlib
import tempfile
import unittest
import uuid

from datahorizon import charts, relational
from datahorizon.engine import EngineError, Workbook
from datahorizon.table_schema import preview as schema_preview

from test_charts import code, payload, spec


def table(name, column):
    return {"kind": "table", "table": name, "column": column}


def source_spec(values, categories=None, type_="column"):
    result = {"spec_version": 1, "type": type_,
              "series": [{"name": {"text": "s"}, "values": {"source": values}}]}
    if categories is not None:
        result["categories"] = {"source": categories}
    return result


class Spec(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.book = Workbook(pathlib.Path(self.tmp.name) / "t.dhwb", create=True)
        self.addCleanup(self.book.close)
        self.apply("table_create", name="Sales", columns=[
            dict(name="Month", type="text"), dict(name="Units", type="integer"), dict(name="Cost", type="number"),
            dict(name="Paid", type="boolean"), dict(name="Day", type="date")])
        self.apply("table_insert", table="Sales", rows=[
            dict(Month=f"M{n}", Units=n, Cost=n * 1.5, Paid=n % 2 == 0, Day=f"2026-01-{n:02d}") for n in range(1, 13)])
        found = relational.table(self.book.db, "Sales")
        self.table_id = found["id"]
        self.column_id = {column["name"]: column["id"] for column in found["columns"]}

    def apply(self, action, **args):
        return self.book.mutate(dict(action=action, operation_id=str(uuid.uuid4()),
                                     expected_state=self.book.state(), **args))

    def refused(self, value):
        with self.assertRaises(EngineError) as caught:
            charts.normalise_spec(self.book, value)
        return caught.exception

    def stored(self, **kwargs):
        return {"kind": "table", "table_id": self.table_id, "column_id": self.column_id[kwargs["column"]]}

    def test_normalise_stores_table_and_column_identities(self):
        out = charts.normalise_spec(self.book, source_spec(table("Sales", "Units"), table("sales", "month")))
        self.assertEqual(out["series"][0]["values"]["source"],
                         {"kind": "table", "table_id": self.table_id, "column_id": self.column_id["Units"]})
        self.assertEqual(out["categories"]["source"], self.stored(column="Month"))

    def test_table_and_column_may_be_given_by_id(self):
        out = charts.normalise_spec(self.book, source_spec(table(self.table_id, self.column_id["Cost"])))
        self.assertEqual(out["series"][0]["values"]["source"], self.stored(column="Cost"))

    def test_refusals(self):
        error = self.refused(source_spec(table("Nope", "Units")))
        self.assertEqual((code(error), error.status), ("TABLE_NOT_FOUND", 404))
        self.assertEqual(code(self.refused(source_spec(table("Sales", "Nope")))), "COLUMN_NOT_FOUND")
        extra = dict(table("Sales", "Units"), ref="A1")
        self.assertEqual(code(self.refused(source_spec(extra))), "INVALID_SPEC")
        self.assertEqual(code(self.refused(source_spec({"kind": "table", "table": "Sales"}))), "INVALID_SPEC")
        self.assertEqual(code(self.refused(source_spec(table("Sales", 5)))), "INVALID_SPEC")
        self.assertEqual(code(self.refused(source_spec(table(5, "Units")))), "INVALID_SPEC")
        named = source_spec(table("Sales", "Units"))
        named["series"][0]["name"] = {"source": table("Sales", "Month")}
        self.assertEqual(code(self.refused(named)), "INVALID_SPEC")
        self.assertEqual(code(self.refused(source_spec({"kind": "pivot"}))), "INVALID_SPEC")

    def test_stored_form_round_trips_and_a_broken_source_is_valid(self):
        out = charts.normalise_spec(self.book, source_spec(table("Sales", "Units"), table("Sales", "Month")))
        self.assertIsNone(charts.validate_stored(payload(out)))
        out["series"][0]["values"]["source"] = {"kind": "table", "table_id": None, "column_id": None}
        self.assertIsNone(charts.validate_stored(payload(out)))
        self.assertTrue(charts.is_broken(out["series"][0]["values"]["source"]))
        self.assertFalse(charts.is_broken(out["categories"]["source"]))
        self.assertEqual(charts.source_kind(out["categories"]["source"]), "table")
        for bad in ({"kind": "table", "table_id": "t", "column_id": None},
                    {"kind": "table", "table_id": None, "column_id": 3},
                    {"kind": "table", "table_id": "t"},
                    {"kind": "table", "table_id": "t", "column_id": "c", "extra": 1}):
            out["series"][0]["values"]["source"] = bad
            self.assertEqual(charts.validate_stored(payload(out)), "INVALID_SPEC", bad)

    def test_describe_adds_current_names(self):
        out = charts.normalise_spec(self.book, source_spec(table("Sales", "Units")))
        before = charts.describe(self.book.db, "s", "c", payload(out))["spec"]["series"][0]["values"]["source"]
        self.assertEqual((before["table"], before["column"], before["broken"]), ("Sales", "Units", False))
        columns = [dict(column) for column in relational.table(self.book.db, "Sales")["columns"]]
        columns[1]["name"] = "Quantity"
        review = schema_preview(self.book, dict(table="Sales", name="Revenue", columns=columns,
                                                expected_state=self.book.state()))
        self.apply("table_schema", **review["source_request"], review_digest=review["review_digest"])
        after = charts.describe(self.book.db, "s", "c", payload(out))["spec"]["series"][0]["values"]["source"]
        self.assertEqual((after["table"], after["column"], after["broken"]), ("Revenue", "Quantity", False))
        gone = copy.deepcopy(out)
        gone["series"][0]["values"]["source"]["column_id"] = str(uuid.uuid4())
        source = charts.describe(self.book.db, "s", "c", payload(gone))["spec"]["series"][0]["values"]["source"]
        self.assertEqual((source["table"], source["column"], source["broken"]), (None, None, True))
        gone["series"][0]["values"]["source"] = {"kind": "table", "table_id": None, "column_id": None}
        source = charts.describe(self.book.db, "s", "c", payload(gone))["spec"]["series"][0]["values"]["source"]
        self.assertTrue(source["broken"])

    def test_spec_patch_keeps_a_table_source(self):
        created = self.apply("chart_create", spec=source_spec(table("Sales", "Units"), table("Sales", "Month")))
        chart_id = created["chart_id"]
        self.apply("chart_update", chart_id=chart_id, spec_patch={"title": {"text": "T"}})
        spec_after = charts.find(self.book, chart_id)[1]["spec"]
        self.assertEqual(spec_after["title"], {"text": "T"})
        self.assertEqual(spec_after["series"][0]["values"]["source"], self.stored(column="Units"))
        self.assertEqual(spec_after["categories"]["source"], self.stored(column="Month"))

    def test_spec_patch_follows_identity_and_refuses_a_broken_source(self):
        created = self.apply("chart_create", spec=source_spec(table("Sales", "Units")))
        chart_id = created["chart_id"]
        sheet_id, stored = charts.find(self.book, chart_id)
        stored["spec"]["series"][0]["values"]["source"] = {"kind": "table", "table_id": None, "column_id": None}
        self.book.db.execute("BEGIN")
        charts.store(self.book, sheet_id, chart_id, stored)
        self.book.db.execute("COMMIT")
        with self.assertRaises(EngineError) as caught:
            self.apply("chart_update", chart_id=chart_id, spec_patch={"title": {"text": "T"}})
        self.assertEqual((code(caught), caught.exception.status), ("SOURCE_BROKEN", 409))

    def test_table_sources_add_nothing_to_size_checks(self):
        out = charts.normalise_spec(self.book, source_spec(table("Sales", "Units")))
        self.assertIsNone(charts.limit_error(out))

    def test_range_charts_are_untouched(self):
        out = charts.normalise_spec(self.book, spec())
        self.assertIsNone(charts.validate_stored(payload(out)))
        source = charts.describe(self.book.db, "s", "c", payload(out))["spec"]["series"][0]["values"]["source"]
        self.assertEqual((source["ref"], source["broken"]), ("B2:B13", False))

    def test_a_workbook_without_a_relational_catalog_has_no_tables(self):
        # A fresh workbook below format 4 must refuse cleanly, not crash.
        other = Workbook(pathlib.Path(self.tmp.name) / "fresh.dhwb", create=True)
        self.addCleanup(other.close)
        with self.assertRaises(EngineError) as caught:
            charts.normalise_spec(other, source_spec(table("Sales", "Units")))
        self.assertEqual(code(caught), "TABLE_NOT_FOUND")
        out = charts.normalise_spec(self.book, source_spec(table("Sales", "Units")))
        source = charts.describe(other.db, "s", "c", payload(out))["spec"]["series"][0]["values"]["source"]
        self.assertTrue(source["broken"])


if __name__ == "__main__":
    unittest.main()
