# SPDX-License-Identifier: GPL-3.0-only
"""Tests for table-column chart sources in specifications."""

import copy
import json
import pathlib
import shutil
import tempfile
import time
import unittest
import uuid
from unittest import mock

from datahorizon import chart_render, chart_resolve, chart_sources, charts, relational
from datahorizon.engine import EngineError, Workbook
from datahorizon.service import Service
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


class Fixture(unittest.TestCase):
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



class Spec(Fixture):
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


    # Following the table.

    def make_chart(self):
        self.chart = self.apply("chart_create", spec=source_spec(table("Sales", "Units"), table("Sales", "Month")))["chart_id"]
        return self.chart

    def read_chart(self):
        return charts.find(self.book, self.chart)[1]

    def sources(self):
        spec_ = self.read_chart()["spec"]
        return spec_["series"][0]["values"]["source"], spec_["categories"]["source"]

    def review(self, columns, **args):
        return schema_preview(self.book, dict(table="Sales", columns=columns, expected_state=self.book.state(), **args))

    def change(self, review):
        return self.apply("table_schema", **review["source_request"], review_digest=review["review_digest"])

    def columns(self):
        return [dict(column) for column in relational.table(self.book.db, "Sales")["columns"]]

    def test_dropping_the_table_breaks_its_sources(self):
        self.make_chart()
        receipt = self.apply("table_drop", table="Sales", allow_data_loss=True)
        self.assertEqual(receipt["broken_charts"], [self.chart])
        values, categories = self.sources()
        self.assertEqual((values["table_id"], values["column_id"]), (None, None))
        self.assertEqual(categories, {"kind": "table", "table_id": None, "column_id": None})
        described = charts.read(self.book, dict(expected_state=self.book.state()))["charts"][0]
        self.assertTrue(described["spec"]["series"][0]["values"]["source"]["broken"])

    def test_a_new_table_of_the_same_name_does_not_repair_the_chart(self):
        self.make_chart()
        self.apply("table_drop", table="Sales", allow_data_loss=True)
        self.apply("table_create", name="Sales", columns=[dict(name="Month", type="text"), dict(name="Units", type="integer")])
        values, _ = self.sources()
        self.assertEqual((values["table_id"], values["column_id"]), (None, None))

    def test_removing_the_column_breaks_only_that_source(self):
        self.make_chart()
        columns = self.columns()
        removed = columns.pop(1)
        receipt = self.change(self.review(columns, drop_columns=[removed["id"]]))
        self.assertEqual(receipt["broken_charts"], [self.chart])
        values, categories = self.sources()
        self.assertEqual((values["table_id"], values["column_id"]), (None, None))
        self.assertEqual(categories, self.stored(column="Month"))

    def test_removing_another_column_breaks_nothing(self):
        self.make_chart()
        columns = self.columns()
        removed = columns.pop(2)
        receipt = self.change(self.review(columns, drop_columns=[removed["id"]]))
        self.assertNotIn("broken_charts", receipt)
        self.assertEqual(self.sources(), (self.stored(column="Units"), self.stored(column="Month")))

    def test_renaming_or_retyping_a_column_breaks_nothing(self):
        self.make_chart()
        columns = self.columns()
        columns[1].update(name="Quantity", type="number")
        receipt = self.change(self.review(columns))
        self.assertNotIn("broken_charts", receipt)
        self.assertEqual(self.sources(), (self.stored(column="Units"), self.stored(column="Month")))

    def test_renaming_the_table_breaks_nothing(self):
        self.make_chart()
        receipt = self.change(self.review(self.columns(), name="Revenue"))
        self.assertNotIn("broken_charts", receipt)
        self.assertEqual(self.sources(), (self.stored(column="Units"), self.stored(column="Month")))

    def test_broken_source_survives_reopen_and_blocks_spec_patch(self):
        self.make_chart()
        self.apply("table_drop", table="Sales", allow_data_loss=True)
        path = self.book.path
        self.book.close()
        self.book = Workbook(path)
        values, _ = self.sources()
        self.assertEqual((values["table_id"], values["column_id"]), (None, None))
        with self.assertRaises(EngineError) as caught:
            self.apply("chart_update", chart_id=self.chart, spec_patch={"title": {"text": "T"}})
        self.assertEqual((code(caught), caught.exception.status), ("SOURCE_BROKEN", 409))

    def test_charts_without_these_sources_are_not_rewritten(self):
        self.make_chart()
        other = self.apply("chart_create", spec=spec())["chart_id"]
        before = charts.find(self.book, other)[1]
        receipt = self.apply("table_drop", table="Sales", allow_data_loss=True)
        self.assertEqual(receipt["broken_charts"], [self.chart])
        self.assertEqual(charts.find(self.book, other)[1], before)
        self.assertEqual(receipt["changed_charts"], 1)

    def test_unreadable_chart_is_left_untouched(self):
        self.make_chart()
        stored = self.read_chart()
        stored["spec"] = {"spec_version": 1, "type": "bogus", "series": [{"values": {"source": {"table_id": self.table_id}}}]}
        self.book.db.execute("UPDATE dh_objects SET payload=json(?) WHERE namespace='chart'", (json.dumps(stored),))
        receipt = self.apply("table_drop", table="Sales", allow_data_loss=True)
        self.assertNotIn("broken_charts", receipt)
        self.assertEqual(self.read_chart()["spec"], stored["spec"])

    def test_break_table_below_format_8_returns_at_once(self):
        other = Workbook(pathlib.Path(self.tmp.name) / "old.dhwb", create=True)
        self.addCleanup(other.close)
        self.assertEqual(charts.break_table(other, "x"), [])


if __name__ == "__main__":
    unittest.main()


def column_spec(values, categories=None, type_="column", x=None):
    item = {"name": {"text": "s"}, "values": {"source": values}}
    if x is not None:
        item["x"] = {"source": x}
    result = {"spec_version": 1, "type": type_, "series": [item]}
    if categories is not None:
        result["categories"] = {"source": categories}
    return result


class Resolving(Fixture):
    def payload(self, spec, book=None):
        stored = charts.normalise_spec(book or self.book, spec)
        return {"name": "Chart", "anchor": {"row": 1, "col": 1}, "offset": {"x": 0, "y": 0},
                "size": {"width": 480, "height": 320}, "z": 0, "spec_version": 1, "spec": stored}

    def resolve(self, spec=None, payload=None, book=None, **kwargs):
        book = book or self.book
        return chart_resolve.resolve(book, payload or self.payload(spec, book), book.state(), **kwargs)

    def refused_resolve(self, *args, **kwargs):
        with self.assertRaises(EngineError) as caught:
            self.resolve(*args, **kwargs)
        return caught.exception

    def test_resolves_table_columns_into_series_and_categories(self):
        spec = column_spec(table("Sales", "Units"), table("Sales", "Month"))
        spec["series"].append({"name": {"text": "Cost"}, "values": {"source": table("Sales", "Cost")}})
        out = self.resolve(spec)
        resolved = out["resolved"]
        self.assertEqual(resolved["categories"], [f"M{n}" for n in range(1, 13)])
        self.assertEqual(resolved["series"][0]["values"], [float(n) for n in range(1, 13)])
        self.assertEqual(resolved["series"][1]["values"], [n * 1.5 for n in range(1, 13)])
        self.assertEqual(out["counts"], {"points": 24, "gaps": {"empty": 0, "text": 0, "error": 0}})
        self.assertEqual(out["state"], self.book.state())
        chart_render.render(resolved, 480, 300)

    def test_null_text_boolean_and_date_values_are_classified(self):
        self.apply("table_create", name="Mixed", columns=[
            dict(name="Num", type="number"), dict(name="Flag", type="boolean"), dict(name="Day", type="date")])
        self.apply("table_insert", table="Mixed", rows=[
            dict(Num=1, Flag=True, Day="2026-01-01"), dict(Num=None, Flag=False, Day="2026-01-02"),
            dict(Num=3, Flag=None, Day=None)])
        spec = column_spec(table("Mixed", "Num"))
        spec["series"] += [{"name": {"text": "f"}, "values": {"source": table("Mixed", "Flag")}},
                           {"name": {"text": "d"}, "values": {"source": table("Mixed", "Day")}}]
        out = self.resolve(spec)
        self.assertEqual(out["counts"], {"points": 2, "gaps": {"empty": 3, "text": 4, "error": 0}})
        self.assertEqual([item["values"] for item in out["resolved"]["series"]],
                         [[1.0, None, 3.0], [None] * 3, [None] * 3])

    def test_category_display_per_column_type(self):
        self.apply("table_create", name="Disp", columns=[
            dict(name="T", type="text"), dict(name="B", type="boolean"), dict(name="D", type="date"),
            dict(name="N", type="number"), dict(name="I", type="integer")])
        self.apply("table_insert", table="Disp", rows=[
            dict(T="a", B=True, D="2026-01-31", N=12.5, I=7), dict(T=None, B=False, D=None, N=None, I=None)])
        expected = {"T": ["a", ""], "B": ["TRUE", "FALSE"], "D": ["2026-01-31", ""], "N": ["12.5", ""], "I": ["7", ""]}
        for column, display in expected.items():
            out = self.resolve(column_spec(table("Disp", "N"), table("Disp", column)))
            self.assertEqual(out["resolved"]["categories"], display, column)

    def test_rows_come_in_stored_order_after_deletes_and_inserts(self):
        ids = [str(row[0]) for row in self.book.db.execute('SELECT "_dh_row_id" FROM "Sales" ORDER BY "_dh_row_id"')]
        self.apply("table_delete", table="Sales", record_ids=[ids[0], ids[5]])
        self.apply("table_insert", table="Sales", rows=[dict(Month="late", Units=99)])
        out = self.resolve(column_spec(table("Sales", "Units"), table("Sales", "Month")))
        self.assertEqual(out["resolved"]["categories"], [f"M{n}" for n in (2, 3, 4, 5, 7, 8, 9, 10, 11, 12)] + ["late"])
        self.assertEqual(out["resolved"]["series"][0]["values"], [2.0, 3.0, 4.0, 5.0, 7.0, 8.0, 9.0, 10.0, 11.0, 12.0, 99.0])

    def test_pie_over_its_point_limit_is_refused_by_name(self):
        self.apply("table_insert", table="Sales", rows=[dict(Units=n) for n in range(989)])
        self.assertEqual(self.book.db.execute('SELECT count(*) FROM "Sales"').fetchone()[0], 1001)
        error = self.refused_resolve(column_spec(table("Sales", "Units"), type_="pie"))
        self.assertEqual(error.body["code"], "LIMIT_PIE_POINTS")
        self.assertIn("Sales", error.body["message"])
        self.assertIn("1,000", error.body["message"])
        self.assertEqual(self.resolve(column_spec(table("Sales", "Units"), type_="column"))["counts"]["points"], 1001)

    def test_running_total_enforces_limit_cells(self):
        spec = column_spec(table("Big", "N"))
        spec["series"] = [{"name": {"text": f"s{n}"}, "values": {"source": table("Big", "N")}} for n in range(11)]
        error = self.refused_resolve(spec, book=self.big)
        self.assertEqual((error.body["code"], error.status), ("LIMIT_CELLS", 400))
        spec["series"] = spec["series"][:10]
        self.assertEqual(self.resolve(spec, book=self.big)["counts"]["points"], 100_000)

    def test_table_over_the_point_limit_is_refused_by_name(self):
        copy_path = pathlib.Path(self.tmp.name) / "over.dhwb"
        shutil.copy(self.big_path, copy_path)
        over = Workbook(copy_path)
        self.addCleanup(over.close)
        over.mutate(dict(action="table_insert", operation_id=str(uuid.uuid4()), expected_state=over.state(),
                         table="Big", rows=[dict(N=1)]))
        error = self.refused_resolve(column_spec(table("Big", "N")), book=over)
        self.assertEqual(error.body["code"], "LIMIT_POINTS")
        self.assertIn("Big", error.body["message"])
        self.assertIn("10,000", error.body["message"])

    def test_change_between_two_column_reads_is_stale(self):
        spec = column_spec(table("Sales", "Units"), table("Sales", "Month"))
        payload = self.payload(spec)
        real = chart_sources.read_table
        calls = []

        def read_then_change(*args, **kwargs):
            calls.append(1)
            result = real(*args, **kwargs)
            if len(calls) == 1:
                other = Workbook(pathlib.Path(self.tmp.name) / "t.dhwb")
                try:
                    other.mutate(dict(action="table_insert", operation_id=str(uuid.uuid4()),
                                      expected_state=other.state(), table="Sales", rows=[dict(Month="x", Units=1)]))
                finally:
                    other.close()
            return result
        with mock.patch.object(chart_sources, "read_table", read_then_change):
            error = self.refused_resolve(payload=payload)
        self.assertEqual((error.body["code"], error.status), ("STALE_STATE", 409))
        self.assertEqual(len(calls), 2)

    def test_broken_or_vanished_source_is_refused_before_reading(self):
        spec = column_spec(table("Sales", "Units"), table("Sales", "Month"))
        for table_id, column_id in ((None, None), (self.table_id, str(uuid.uuid4())),
                                    (str(uuid.uuid4()), self.column_id["Units"])):
            payload = self.payload(spec)
            payload["spec"]["series"][0]["values"]["source"] = {"kind": "table", "table_id": table_id, "column_id": column_id}
            with mock.patch.object(chart_sources, "read_table") as read:
                with self.assertRaises(EngineError) as caught:
                    self.resolve(payload=payload)
            self.assertEqual((caught.exception.body["code"], caught.exception.status), ("SOURCE_BROKEN", 409))
            read.assert_not_called()

    def test_query_source_waits_for_a_query_runner(self):
        payload = self.payload(column_spec(table("Sales", "Units")))
        payload["spec"]["series"][0]["values"]["source"] = {"kind": "query", "query_id": str(uuid.uuid4()), "column": "x"}
        with self.assertRaises(EngineError) as caught:
            self.resolve(payload=payload)
        self.assertEqual(caught.exception.body["code"], "INVALID_ARGUMENT")
        self.assertIn("query runner", caught.exception.body["message"])

    def test_scatter_from_two_table_columns(self):
        out = self.resolve(column_spec(table("Sales", "Cost"), type_="scatter", x=table("Sales", "Units")))
        series = out["resolved"]["series"][0]
        self.assertEqual(series["x"], [float(n) for n in range(1, 13)])
        self.assertEqual(series["values"], [n * 1.5 for n in range(1, 13)])
        self.assertEqual(out["counts"]["points"], 12)
        chart_render.render(out["resolved"], 480, 300)

    def test_mixed_range_categories_and_table_values(self):
        names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
        self.apply("edit", sheet="Sheet1", cells=[{"row": n, "col": 1, "source": name} for n, name in enumerate(names, 1)])
        out = self.resolve(column_spec(table("Sales", "Units"), {"kind": "range", "sheet": "Sheet1", "ref": "A1:A12"}))
        self.assertEqual(out["resolved"]["categories"], names)
        self.assertEqual(out["resolved"]["series"][0]["values"], [float(n) for n in range(1, 13)])

    def test_progress_advances_for_table_sources(self):
        seen = []
        self.resolve(column_spec(table("Sales", "Units"), table("Sales", "Month")),
                     checkpoint=lambda done, phase: seen.append((done, phase)))
        self.assertEqual(seen[-1], (24, "Reading sources"))

    def test_through_a_real_chart_job(self):
        with Service() as server:
            info = server.dispatch("POST", "/v1/open", {"path": str(pathlib.Path(self.tmp.name) / "t.dhwb")})
            base = f"/v1/workbooks/{info['id']}"

            def operate(**args):
                state = server.dispatch("GET", base, {})["state"]
                return server.dispatch("POST", base + "/operations",
                                       {"operation_id": str(uuid.uuid4()), "expected_state": state, **args})
            chart = operate(action="chart_create", sheet="Sheet1",
                            spec=column_spec(table("Sales", "Units"), table("Sales", "Month")))["chart_id"]
            for kind in ("resolve", "render"):
                body = {"job_id": uuid.uuid4().hex, "expected_state": server.dispatch("GET", base, {})["state"],
                        "chart_id": chart, "kind": kind}
                if kind == "render":
                    body.update(width=480, height=300)
                job = server.dispatch("POST", base + "/charts/jobs", body)
                deadline = time.monotonic() + 30
                view = job
                while view["status"] == "running" and time.monotonic() < deadline:
                    time.sleep(0.01)
                    view = server.dispatch("GET", f"{base}/charts/jobs/{job['job_id']}", {})
                self.assertEqual(view["status"], "completed", view)
                self.assertEqual(view["result"]["counts"]["points"], 12)
                self.assertEqual(view["result"]["resolved"]["categories"][:2], ["M1", "M2"])

    @classmethod
    def setUpClass(cls):
        cls.big_dir = tempfile.mkdtemp()
        cls.addClassCleanup(shutil.rmtree, cls.big_dir, True)
        cls.big_path = pathlib.Path(cls.big_dir) / "big.dhwb"
        big = Workbook(cls.big_path, create=True)
        try:
            big.mutate(dict(action="table_create", operation_id=str(uuid.uuid4()), expected_state=big.state(),
                            name="Big", columns=[dict(name="N", type="integer")]))
            big.mutate(dict(action="table_insert", operation_id=str(uuid.uuid4()), expected_state=big.state(),
                            table="Big", rows=[dict(N=n) for n in range(10_000)]))
        finally:
            big.close()

    def setUp(self):
        super().setUp()
        self.big = Workbook(self.big_path)
        self.addCleanup(self.big.close)
