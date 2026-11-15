# SPDX-License-Identifier: GPL-3.0-only
"""Tests for the format-neutral chart export step."""

import json
import pathlib
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import uuid

from datahorizon import charts, export_charts, import_charts, query_views, relational
from datahorizon.conversion_report import ConversionReport
from datahorizon.engine import EngineError, Workbook

DIALECTS = ("xlsx", "ods")


def source(ref, sheet="Sheet1"):
    return {"source": {"kind": "range", "sheet": sheet, "ref": ref}}


def column_spec(**overrides):
    base = {"spec_version": 1, "type": "column", "categories": source("A2:A13"),
            "series": [{"name": source("B1"), "values": source("B2:B13"), "x": None}]}
    base.update(overrides)
    return base


class Export(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.book = Workbook(pathlib.Path(self.tmp.name) / "x.dhwb", create=True)
        self.sheet = self.book.sheet("Sheet1")
        self.names = {self.sheet: "Sales"}

    def tearDown(self):
        self.book.close()
        self.tmp.cleanup()

    def payload(self, spec=None, *, name="Chart", anchor=(1, 1), z=0):
        return {"name": name, "anchor": {"row": anchor[0], "col": anchor[1]}, "offset": {"x": 3, "y": 4},
                "size": {"width": 480, "height": 320}, "z": z, "spec_version": 1,
                "spec": charts.normalise_spec(self.book, spec or column_spec())}

    def chart(self, spec=None, *, chart_id="c1", **kwargs):
        return charts.describe(self.book.db, self.sheet, chart_id, self.payload(spec, **kwargs))

    def entry(self, chart):
        return {"sheet_id": self.sheet, "sheet": "Sheet1", "chart": chart}

    def run_drafts(self, chart, **kwargs):
        report = ConversionReport()
        result = export_charts.drafts([self.entry(chart)], self.names, report, **kwargs)
        return result, report.entries()

    def assert_skipped(self, chart, reason, **kwargs):
        result, entries = self.run_drafts(chart, **kwargs)
        self.assertEqual(result, [])
        self.assertEqual(len(entries), 1)
        only = entries[0]
        self.assertEqual((only["category"], only["severity"], only["detail"], only["count"]),
                         ("chart.skipped", "omitted", reason, 1))
        return only

    def test_range_chart_becomes_a_draft(self):
        spec = column_spec(title={"text": "T"}, legend={"position": "bottom"},
                           axes={"category": {"title": "C"}, "value": {"title": "V", "min": 0, "max": 9}})
        result, entries = self.run_drafts(self.chart(spec))
        draft = result[0]
        self.assertEqual(draft["categories"], ("Sales", 2, 1, 13, 1))
        self.assertEqual(draft["series"][0]["values"], ("Sales", 2, 2, 13, 2))
        self.assertEqual(draft["series"][0]["name"], ("ref", ("Sales", 1, 2, 1, 2)))
        self.assertEqual((draft["type"], draft["grouping"], draft["legend"]), ("column", "clustered", "bottom"))
        self.assertEqual((draft["name"], draft["sheet"], draft["anchor"], draft["offset"], draft["size"]),
                         ("Chart", "Sales", {"row": 1, "col": 1}, {"x": 3, "y": 4}, {"width": 480, "height": 320}))
        self.assertEqual((draft["title"], draft["category_title"], draft["value_title"], draft["min"], draft["max"]),
                         ("T", "C", "V", 0, 9))
        self.assertIsNone(draft["series"][0]["x"])
        self.assertEqual(entries, [])

    def test_text_name_and_scatter_x(self):
        item = {"name": {"text": "Fixed"}, "values": source("B2:B13"), "x": source("A2:A13")}
        result, _ = self.run_drafts(self.chart({"spec_version": 1, "type": "scatter", "series": [item]}))
        self.assertEqual((result[0]["series"][0]["name"], result[0]["series"][0]["x"], result[0]["categories"],
                          result[0]["grouping"]), (("text", "Fixed"), ("Sales", 2, 1, 13, 1), None, None))

    def test_each_reason(self):
        unsupported = self.chart()
        unsupported["issue"] = "INVALID_SPEC"
        broken = self.chart()
        broken["spec"]["series"][0]["values"]["source"]["sheet_id"] = None
        table = self.chart()
        table["spec"]["series"][0]["values"]["source"] = {"kind": "table", "table_id": "t", "column_id": "c"}
        query = self.chart()
        query["spec"]["series"][0]["values"]["source"] = {"kind": "query", "query_id": "q", "column": "x"}
        gone = self.chart()
        gone["spec"]["categories"]["source"]["sheet_id"] = "gone"
        two_dimensional = self.chart()
        two_dimensional["spec"]["series"][0]["values"]["source"]["col2"] = 3
        wide_name = self.chart()
        wide_name["spec"]["series"][0]["name"]["source"]["row2"] = 2
        for chart, reason in ((unsupported, "unsupported"), (broken, "broken_source"), (table, "table_source"),
                              (query, "query_source"), (gone, "source"), (two_dimensional, "source"),
                              (wide_name, "source")):
            only = self.assert_skipped(chart, reason)
            self.assertEqual(only["samples"], [{"sheet": "Sales", "cell": "Chart"}], reason)

    def test_report_names_the_chart_cut_to_32_characters(self):
        chart = self.chart(name="N" * 40)
        chart["issue"] = "INVALID_SPEC"
        self.assertEqual(self.assert_skipped(chart, "unsupported")["samples"], [{"sheet": "Sales", "cell": "N" * 32}])

    def test_broken_wins_over_table(self):
        chart = self.chart()
        chart["spec"]["series"][0]["values"]["source"] = {"kind": "table", "table_id": None, "column_id": None}
        chart["spec"]["categories"]["source"] = {"kind": "table", "table_id": "t", "column_id": "c"}
        self.assert_skipped(chart, "broken_source")

    def test_selection_keeps_chart_inside(self):
        selection = {"sheet_id": self.sheet, "range": [1, 1, 13, 2], "name": "Sheet1"}
        result, entries = self.run_drafts(self.chart(), selection=selection)
        self.assertEqual(entries, [])
        self.assertEqual(result[0]["sheet"], "Sheet1")
        self.assertEqual(result[0]["categories"], ("Sheet1", 2, 1, 13, 1))
        self.assertEqual(result[0]["series"][0]["values"], ("Sheet1", 2, 2, 13, 2))
        self.assertEqual(result[0]["series"][0]["name"], ("ref", ("Sheet1", 1, 2, 1, 2)))

    def test_selection_reports_chart_reaching_outside(self):
        selection = {"sheet_id": self.sheet, "range": [1, 1, 13, 2], "name": "Sheet1"}
        self.assert_skipped(self.chart(anchor=(1, 3)), "source", selection=selection)
        self.assert_skipped(self.chart(column_spec(categories=source("A2:A14"))), "source", selection=selection)
        other = self.chart()
        other["spec"]["series"][0]["values"]["source"]["sheet_id"] = "other"
        self.names["other"] = "Other"
        self.assert_skipped(other, "source", selection=selection)

    def test_selection_ignores_other_sheets(self):
        selection = {"sheet_id": "elsewhere", "range": [1, 1, 13, 2], "name": "Sheet1"}
        self.assertEqual(self.run_drafts(self.chart(), selection=selection), ([], []))

    def test_drafts_keep_entry_order_and_skip_only_the_bad_one(self):
        bad = self.chart(chart_id="c2")
        bad["issue"] = "INVALID_SPEC"
        report = ConversionReport()
        entries = [self.entry(self.chart(name="First")), self.entry(bad),
                   self.entry(self.chart(name="Third", chart_id="c3"))]
        result = export_charts.drafts(entries, self.names, report)
        self.assertEqual([draft["name"] for draft in result], ["First", "Third"])
        self.assertEqual(len(report.entries()), 1)

    def test_reference_round_trips(self):
        for name in ("Sales", "Bob's Q1", "Ventas 2026", "数据", "A1", "Q1.2026", "My Sheet"):
            for dialect in DIALECTS:
                for ref in ((name, 2, 1, 13, 1), (name, 5, 28, 5, 28), (name, 1, 1, 1, 3)):
                    text = export_charts.format_reference(ref, dialect)
                    self.assertEqual(import_charts.parse_reference(text, dialect), ref, (text, dialect))

    def test_reference_is_absolute(self):
        self.assertEqual(export_charts.format_reference(("Sales", 2, 1, 13, 1), "xlsx"), "Sales!$A$2:$A$13")
        self.assertEqual(export_charts.format_reference(("Bob's Q1", 2, 2, 2, 2), "xlsx"), "'Bob''s Q1'!$B$2")
        self.assertEqual(export_charts.format_reference(("Sales", 2, 1, 13, 1), "ods"), "$Sales.$A$2:.$A$13")

    def test_build_reports_a_failing_chart_and_keeps_the_rest(self):
        report = ConversionReport()
        made = [{"name": name, "sheet": "Sales"} for name in ("One", "Two", "Three")]

        def build_one(draft):
            if draft["name"] == "Two":
                raise ValueError("boom")
            return draft["name"].lower()

        result = export_charts.build(made, build_one, report)
        self.assertEqual([(draft["name"], built) for draft, built in result], [("One", "one"), ("Three", "three")])
        entries = report.entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual((entries[0]["category"], entries[0]["severity"], entries[0]["detail"], entries[0]["samples"]),
                         ("chart.skipped", "omitted", "unwritable", [{"sheet": "Sales", "cell": "Two"}]))

    def test_build_lets_engine_errors_through(self):
        def build_one(draft):
            raise EngineError("CANCELLED", "stopped")

        with self.assertRaises(EngineError) as caught:
            export_charts.build([{"name": "One", "sheet": "Sales"}], build_one, ConversionReport())
        self.assertEqual(caught.exception.body["code"], "CANCELLED")

    def test_lines(self):
        self.assertEqual(export_charts.lines(0, 0), [])
        self.assertEqual(export_charts.lines(3, 0), ["Charts: 3 written."])
        self.assertEqual(export_charts.lines(2, 1),
                         ["Charts: 2 written, 1 left out; each is listed below with its reason."])

    def test_collect_orders_by_sheet_then_z_then_id(self):
        self.assertEqual(export_charts.collect(self.book.db), [])
        self.book.db.execute("INSERT INTO sheets(id, name, position) VALUES ('s2', 'Other', 99)")
        self.book.changes = {}
        for sheet_id, chart_id, z in (("s2", "a", 0), (self.sheet, "b", 1), (self.sheet, "c", 0), (self.sheet, "a", 1)):
            charts.store(self.book, sheet_id, chart_id, self.payload(z=z))
        found = export_charts.collect(self.book.db)
        self.assertEqual([(item["sheet"], item["chart"]["id"]) for item in found],
                         [("Sheet1", "c"), ("Sheet1", "a"), ("Sheet1", "b"), ("Other", "a")])
        self.assertEqual((found[0]["sheet_id"], found[0]["chart"]["sheet_id"], found[0]["chart"]["issue"]),
                         (self.sheet, self.sheet, None))

    def test_collect_contains_an_unreadable_chart_and_drafts_reports_it(self):
        self.book.changes = {}
        for chart_id, name in (("good", "Good"), ("bad", "Bent"), ("junk", "Junk")):
            charts.store(self.book, self.sheet, chart_id, self.payload(name=name))
        broken = self.payload(name="Bent")
        del broken["z"]
        self.book.db.execute("UPDATE dh_objects SET payload=? WHERE id='bad'", (json.dumps(broken),))
        self.book.db.execute("UPDATE dh_objects SET payload='{not json' WHERE id='junk'")
        found = export_charts.collect(self.book.db)
        self.assertEqual(len(found), 3)
        by_name = {item["chart"]["name"]: item for item in found}
        self.assertEqual(set(by_name), {"Good", "Bent", "Chart"})
        self.assertIsNone(by_name["Good"]["chart"]["issue"])
        self.assertIsNotNone(by_name["Bent"]["chart"]["issue"])
        report = ConversionReport()
        result = export_charts.drafts(found, self.names, report)
        self.assertEqual([draft["name"] for draft in result], ["Good"])
        entries = report.entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual((entries[0]["detail"], entries[0]["count"]), ("unsupported", 2))
        self.assertEqual(sorted(sample["cell"] for sample in entries[0]["samples"]), ["Bent", "Chart"])
        self.assertEqual({sample["sheet"] for sample in entries[0]["samples"]}, {"Sales"})

    def apply(self, action, **args):
        return self.book.mutate(dict(action=action, operation_id=str(uuid.uuid4()), expected_state=self.book.state(), **args))

    def test_collect_reads_each_catalog_at_most_once(self):
        self.apply("table_create", name="Sales", columns=[dict(name=f"C{n}", type="integer") for n in range(4)])
        self.apply("table_create", name="Other", columns=[dict(name="N", type="integer")])
        table_spec = {"spec_version": 1, "type": "line",
                      "series": [{"name": {"text": f"s{n}"}, "values": {"source": {"kind": "table", "table": "Sales", "column": f"C{n % 4}"}}}
                                 for n in range(8)]}
        self.book.changes = {}
        for number in range(5):
            charts.store(self.book, self.sheet, f"t{number}", self.payload(table_spec, name=f"T{number}"))
            charts.store(self.book, self.sheet, f"r{number}", self.payload(name=f"R{number}"))
        with patch.object(relational, "tables", wraps=relational.tables) as tables, \
                patch.object(query_views, "queries", wraps=query_views.queries) as queries:
            found = export_charts.collect(self.book.db)
        self.assertEqual(len(found), 10)
        self.assertEqual(tables.call_count, 1)
        self.assertEqual(queries.call_count, 0)
        described = {item["chart"]["name"]: item["chart"] for item in found}
        self.assertEqual(described["T0"]["spec"]["series"][5]["values"]["source"]["column"], "C1")
        self.assertEqual(described["R0"]["spec"]["series"][0]["values"]["source"]["ref"], "B2:B13")
        report = ConversionReport()
        self.assertEqual(len(export_charts.drafts(found, self.names, report)), 5)
        self.assertEqual([(entry["detail"], entry["count"]) for entry in report.entries()], [("table_source", 5)])

    def test_collect_for_one_sheet(self):
        self.book.db.execute("INSERT INTO sheets(id, name, position) VALUES ('s2', 'Other', 99)")
        self.book.changes = {}
        charts.store(self.book, "s2", "a", self.payload(name="Elsewhere"))
        charts.store(self.book, self.sheet, "b", self.payload(name="Here"))
        self.assertEqual([item["chart"]["name"] for item in export_charts.collect(self.book.db, self.sheet)], ["Here"])
        self.assertEqual([item["chart"]["name"] for item in export_charts.collect(self.book.db, "s2")], ["Elsewhere"])
        self.assertEqual(len(export_charts.collect(self.book.db)), 2)

    def test_collect_contains_a_payload_that_is_not_an_object(self):
        # A bare database: the engine's workbook validation would refuse these payloads before any export runs.
        good = self.payload(name="Good")
        good["spec"]["series"][0]["values"]["source"]["sheet_id"] = "s1"
        good["spec"]["series"][0]["name"]["source"]["sheet_id"] = "s1"
        good["spec"]["categories"]["source"]["sheet_id"] = "s1"
        for label, text in (("list", "[]"), ("number", "5"), ("text", '"x"'), ("null", "null"), ("empty", "{}"),
                            ("not json", "{nope")):
            with self.subTest(label):
                db = sqlite3.connect(":memory:")
                self.addCleanup(db.close)
                db.executescript(f"CREATE TABLE dh_meta(format_version); INSERT INTO dh_meta VALUES({charts.FORMAT_VERSION});"
                                 "CREATE TABLE sheets(id, position, name); INSERT INTO sheets VALUES('s1', 0, 'Data');"
                                 "CREATE TABLE dh_objects(id, sheet_id, namespace, payload);")
                db.execute("INSERT INTO dh_objects VALUES('bad', 's1', 'chart', ?)", (text,))
                db.execute("INSERT INTO dh_objects VALUES('good', 's1', 'chart', ?)", (json.dumps(good),))
                found = export_charts.collect(db)
                self.assertEqual(len(found), 2)
                report = ConversionReport()
                result = export_charts.drafts(found, {"s1": "Data"}, report)
                self.assertEqual([draft["name"] for draft in result], ["Good"])
                self.assertEqual([(entry["detail"], entry["samples"]) for entry in report.entries()],
                                 [("unsupported", [{"sheet": "Data", "cell": "Chart"}])])


if __name__ == "__main__":
    unittest.main()
