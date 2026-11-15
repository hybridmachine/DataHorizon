# SPDX-License-Identifier: GPL-3.0-only
"""Tests for the format-neutral chart conversion rules and record handling."""

import copy
import pathlib
import tempfile
import unittest
import uuid

from datahorizon import charts
from datahorizon.conversion_report import ConversionReport
from datahorizon.engine import EngineError, Workbook, encoded
from datahorizon.import_charts import (RECORD_BYTES, RECORDS_BYTES, STYLED, UNUSABLE, Collector, check_records, convert,
                                       create, for_sheet, parse_reference, summary)

SHEETS = ["Data", "Other"]


def draft(**overrides):
    base = {"sheet": 0, "name": "Chart 1", "order": 0,
            "plots": [{"kind": "bar", "direction": "col", "grouping": "clustered", "three_d": False, "secondary": False}],
            "title": {"text": None, "from_cell": False},
            "axis_titles": {"category": {"text": None, "from_cell": False}, "value": {"text": None, "from_cell": False}},
            "value_min": None, "value_max": None, "legend": None,
            "series": [{"name": {"text": "Sales"}, "values": ("Data", 2, 2, 8, 2),
                        "categories": ("Data", 2, 1, 8, 1), "x": None}],
            "styled": [], "anchor": {"row": 1, "col": 3}, "offset": {"x": 66, "y": 16},
            "size": {"width": 480, "height": 300}, "approximate_size": False, "absolute_anchor": False}
    base.update(overrides)
    return base


def plot(kind="bar", direction="col", grouping="clustered", **overrides):
    return dict({"kind": kind, "direction": direction, "grouping": grouping, "three_d": False, "secondary": False},
                **overrides)


def series_of(values=("Data", 2, 2, 8, 2), **overrides):
    return dict({"name": {"text": "Sales"}, "values": values, "categories": ("Data", 2, 1, 8, 1), "x": None}, **overrides)


def entries(report):
    return [(entry["category"], entry["detail"]) for entry in report.entries()]


class Reference(unittest.TestCase):
    def test_parse_reference_xlsx(self):
        self.assertEqual(parse_reference("'Column Chart'!$B$2:$B$8", "xlsx"), ("Column Chart", 2, 2, 8, 2))
        self.assertEqual(parse_reference("Data!$A$1", "xlsx"), ("Data", 1, 1, 1, 1))
        self.assertEqual(parse_reference("'Bob''s Q1'!$A$2:$A$8", "xlsx"), ("Bob's Q1", 2, 1, 8, 1))
        self.assertEqual(parse_reference("Données!$A$2:$A$8", "xlsx"), ("Données", 2, 1, 8, 1))
        self.assertEqual(parse_reference("Data!$B:$B", "xlsx"), ("Data", 1, 2, 1048576, 2))
        for bad in ("(Data!$A$1:$A$3,Data!$C$1:$C$3)", "MyName", "[1]Data!$A$1", "Table1[Sales]", "{1,2,3}", ""):
            self.assertIs(parse_reference(bad, "xlsx"), UNUSABLE)

    def test_parse_reference_ods(self):
        self.assertEqual(parse_reference("$'Column Chart'.$B$2:.$B$8", "ods"), ("Column Chart", 2, 2, 8, 2))
        self.assertEqual(parse_reference("Data.A1", "ods"), ("Data", 1, 1, 1, 1))
        self.assertEqual(parse_reference("$'Bob''s Q1'.$A$2:$'Bob''s Q1'.$A$8", "ods"), ("Bob's Q1", 2, 1, 8, 1))
        self.assertIs(parse_reference("Data.A1:Data.A3 Data.C1:Data.C3", "ods"), UNUSABLE)
        self.assertIs(parse_reference("$Data.$A$1:$Other.$A$3", "ods"), UNUSABLE)


class Convert(unittest.TestCase):
    def setUp(self):
        self.report = ConversionReport()

    def convert(self, **overrides):
        return convert(draft(**overrides), SHEETS, self.report)

    def test_clustered_column(self):
        record = self.convert()
        spec = record["spec"]
        self.assertEqual((record["sheet"], record["name"], record["anchor"], record["offset"], record["size"]),
                         (0, "Chart 1", {"row": 1, "col": 3}, {"x": 66, "y": 16}, {"width": 480, "height": 300}))
        self.assertEqual((spec["type"], spec["grouping"], spec["legend"]), ("column", "clustered", {"position": "none"}))
        self.assertEqual(spec["categories"], {"source": {"kind": "range", "sheet": 0, "ref": "A2:A8"}})
        self.assertEqual(spec["series"][0]["values"], {"source": {"kind": "range", "sheet": 0, "ref": "B2:B8"}})
        self.assertEqual(entries(self.report), [])

    def test_types(self):
        for kind, direction, wanted in (("bar", "col", "column"), ("bar", "bar", "bar"), ("line", None, "line"),
                                        ("area", None, "area"), ("pie", None, "pie"), ("scatter", None, "scatter")):
            record = self.convert(plots=[plot(kind, direction)], series=[series_of(x=("Data", 2, 1, 8, 1))])
            self.assertEqual(record["spec"]["type"], wanted)
            self.assertEqual(record["spec"]["grouping"], "clustered" if wanted in ("column", "bar", "area") else None)
        record = self.convert(plots=[plot("scatter", None, None)], series=[series_of(x=("Data", 2, 1, 8, 1))])
        self.assertIsNone(record["spec"]["categories"])
        self.assertEqual(record["spec"]["series"][0]["x"], {"source": {"kind": "range", "sheet": 0, "ref": "A2:A8"}})
        self.assertEqual(entries(self.report), [])

    def test_grouping(self):
        self.assertEqual(self.convert(plots=[plot(grouping="standard")])["spec"]["grouping"], "clustered")
        self.assertEqual(self.convert(plots=[plot(grouping="stacked")])["spec"]["grouping"], "stacked")
        self.assertEqual(entries(self.report), [])
        self.assertEqual(self.convert(plots=[plot(grouping="percentStacked")])["spec"]["grouping"], "stacked")
        self.assertEqual(entries(self.report), [("chart.converted", "percent_stacked")])
        for grouping in ("stacked", "percentStacked"):
            self.report = ConversionReport()
            record = self.convert(plots=[plot("line", None, grouping)])
            self.assertIsNone(record["spec"]["grouping"])
            self.assertEqual(entries(self.report), [("chart.converted", "grouping")])

    def test_three_d_and_doughnut(self):
        record = self.convert(plots=[plot(three_d=True)])
        self.assertEqual(record["spec"]["type"], "column")
        self.assertEqual(entries(self.report), [("chart.converted", "three_d")])
        report = self.report = ConversionReport()
        record = self.convert(plots=[plot("doughnut", None, None)])
        self.assertEqual(record["spec"]["type"], "pie")
        self.assertEqual(entries(report), [("chart.converted", "doughnut")])

    def test_legend(self):
        for given, wanted, noted in (("right", "right", []), ("bottom", "bottom", []),
                                     ("top", "right", [("chart.converted", "legend_position")]),
                                     ("left", "right", [("chart.converted", "legend_position")])):
            self.report = ConversionReport()
            self.assertEqual(self.convert(legend=given)["spec"]["legend"], {"position": wanted})
            self.assertEqual(entries(self.report), noted)

    def test_titles(self):
        record = self.convert(title={"text": "x" * 300, "from_cell": False})
        self.assertEqual(record["spec"]["title"], {"text": "x" * 255})
        self.assertEqual(entries(self.report), [("chart.converted", "title_cut")])
        self.report = ConversionReport()
        record = self.convert(title={"text": "Sales", "from_cell": True},
                              axis_titles={"category": {"text": "Division", "from_cell": False},
                                           "value": {"text": "USD", "from_cell": False}},
                              value_min=1, value_max=9)
        self.assertEqual(record["spec"]["title"], {"text": "Sales"})
        self.assertEqual(record["spec"]["axes"], {"category": {"title": "Division"},
                                                  "value": {"title": "USD", "min": 1, "max": 9}})
        self.assertEqual(entries(self.report), [("chart.converted", "title_from_cell")])
        self.report = ConversionReport()
        record = self.convert(value_min=9, value_max=1)
        self.assertEqual(record["spec"]["axes"]["value"], {"title": None, "min": None, "max": None})
        self.assertEqual(entries(self.report), [])

    def test_series_name(self):
        def named(name):
            return self.convert(series=[series_of(name=name)])["spec"]["series"][0]["name"]

        self.assertEqual(named({"text": "Sales"}), {"text": "Sales"})
        self.assertEqual(named({"ref": ("Data", 1, 2, 1, 2)}), {"source": {"kind": "range", "sheet": 0, "ref": "B1"}})
        self.assertEqual(named(None), {"text": "Series 1"})
        self.assertEqual(entries(self.report), [])
        for bad in ({"ref": ("Data", 1, 2, 2, 2)}, {"ref": UNUSABLE}, {"ref": ("Missing", 1, 2, 1, 2)}):
            self.report = ConversionReport()
            self.assertEqual(named(bad), {"text": "Series 1"})
            self.assertEqual(entries(self.report), [("chart.converted", "series_name")])

    def test_series_names_and_axis_titles_are_cut(self):
        long = "N" * 300_000
        record = self.convert(series=[series_of(name={"text": long})],
                              axis_titles={"category": {"text": "C" * 300, "from_cell": False},
                                           "value": {"text": "V" * 300_000, "from_cell": False}})
        spec = record["spec"]
        self.assertEqual(spec["series"][0]["name"], {"text": "N" * charts.TITLE_LENGTH})
        self.assertEqual((spec["axes"]["category"]["title"], spec["axes"]["value"]["title"]),
                         ("C" * charts.TITLE_LENGTH, "V" * charts.TITLE_LENGTH))
        found = {(entry["category"], entry["detail"]): entry["count"] for entry in self.report.entries()}
        self.assertEqual(found, {("chart.converted", "series_name_cut"): 1, ("chart.converted", "title_cut"): 2})
        self.report = ConversionReport()
        self.convert(series=[series_of(name={"text": "N" * charts.TITLE_LENGTH})])
        self.assertEqual(entries(self.report), [])

    def test_oversized_record_is_skipped(self):
        # 60 names of 255 characters that JSON writes as six bytes each: about 92 KB, past RECORD_BYTES.
        wide = [series_of(name={"text": "☃" * 300}) for _ in range(60)]
        self.assertIsNone(self.convert(series=wide, styled=["colours"]))
        self.assertEqual(entries(self.report), [("chart.skipped", "limit")])
        self.report = ConversionReport()
        record = self.convert(series=wide[:30])
        self.assertLessEqual(len(encoded(record).encode()), RECORD_BYTES)

    def test_categories_differ(self):
        other = series_of(categories=("Data", 2, 3, 8, 3))
        record = self.convert(series=[series_of(), other])
        self.assertEqual(record["spec"]["categories"], {"source": {"kind": "range", "sheet": 0, "ref": "A2:A8"}})
        self.assertEqual(entries(self.report), [("chart.converted", "categories_differ")])

    def test_styled(self):
        self.assertEqual(len(STYLED), 8)
        self.convert(styled=["colours", "data labels"])
        found = self.report.entries()
        self.assertEqual(sorted(entry["detail"] for entry in found), ["styling: colours", "styling: data labels"])
        self.assertEqual({entry["category"] for entry in found}, {"chart.converted"})
        self.assertEqual(found[0]["samples"], [{"sheet": "Data", "cell": "Chart 1"}])
        self.report = ConversionReport()
        name = "".join(chr(97 + n % 26) for n in range(64))
        self.convert(styled=list(STYLED), name=name)
        found = self.report.entries()
        self.assertEqual(sorted(entry["detail"] for entry in found), sorted(f"styling: {item}" for item in STYLED))
        self.assertTrue(all(entry["samples"] == [{"sheet": "Data", "cell": name[:32]}] for entry in found))

    def test_name(self):
        self.assertEqual(self.convert(name="n" * 70)["name"], "n" * 64)
        self.assertEqual(entries(self.report), [("chart.converted", "name")])
        for bad in ("bad\x01name", ""):
            self.report = ConversionReport()
            self.assertEqual(self.convert(name=bad)["name"], "Chart")
            self.assertEqual(entries(self.report), [("chart.converted", "name")])

    def test_placement_clamped(self):
        record = self.convert(size={"width": 10, "height": 10})
        self.assertEqual(record["size"], {"width": 64, "height": 48})
        record = self.convert(size={"width": 9000, "height": 9000}, offset={"x": 5000, "y": 5000})
        self.assertEqual((record["size"], record["offset"]), ({"width": 4096, "height": 4096}, {"x": 4096, "y": 4096}))
        self.assertEqual(entries(self.report), [])
        self.convert(approximate_size=True, absolute_anchor=True)
        self.assertEqual(entries(self.report), [("chart.converted", "size_approximate"),
                                                ("chart.converted", "anchor_absolute")])

    def test_skips(self):
        many = [series_of() for _ in range(65)]
        eleven = [series_of(values=("Data", 1, 2, 10000, 2), categories=None) for _ in range(11)]
        cases = [
            ("type", dict(plots=[plot("radar", None, None)])),
            ("combination", dict(plots=[plot(), plot("line", None, None)])),
            ("secondary_axis", dict(plots=[plot(secondary=True)])),
            ("pie_series", dict(plots=[plot("pie", None, None)], series=[series_of(), series_of()])),
            ("source", dict(series=[series_of(UNUSABLE)])),
            ("source", dict(series=[series_of(("Missing", 2, 2, 8, 2))])),
            ("source", dict(series=[series_of(("Data", 2, 2, 4, 4))])),
            ("source", dict(series=[series_of(None)])),
            ("source", dict(series=[])),
            ("source", dict(plots=[plot("scatter", None, None)], series=[series_of(x=None)])),
            ("limit", dict(series=many)),
            ("limit", dict(series=[series_of(("Data", 1, 2, 10001, 2))])),
            ("limit", dict(plots=[plot("pie", None, None)], series=[series_of(("Data", 1, 2, 1001, 2))])),
            ("limit", dict(series=eleven)),
            ("limit", dict(series=[series_of(("Data", 1, 2, 1048576, 2))])),
            ("off_grid", dict(anchor={"row": 1, "col": 20000})),
            ("off_grid", dict(anchor={"row": 1048577, "col": 1})),
            ("off_grid", dict(anchor={"row": 0, "col": 1})),
        ]
        for detail, overrides in cases:
            self.report = ConversionReport()
            self.assertIsNone(self.convert(**overrides), detail)
            self.assertEqual(entries(self.report), [("chart.skipped", detail)], overrides.get("plots", detail))

    def test_sheet_name_match_ignores_case_as_import_does(self):
        record = self.convert(series=[series_of(("data", 2, 2, 8, 2), categories=("DATA", 2, 1, 8, 1))])
        self.assertEqual(record["spec"]["series"][0]["values"]["source"]["sheet"], 0)
        record = self.convert(series=[series_of(("other", 2, 2, 8, 2))])
        self.assertEqual(record["spec"]["series"][0]["values"]["source"]["sheet"], 1)


def record(position=0, name="Chart 1", **spec_overrides):
    made = convert(draft(sheet=position, name=name, series=[series_of(("Data", 2, 2, 8, 2) if position == 0 else ("Other", 2, 2, 8, 2),
                                                                      categories=None)]), SHEETS, ConversionReport())
    made["spec"].update(spec_overrides)
    return made


def wide_draft():
    """A supported chart of 64 series named with 255 characters: about 20 KB as a record."""
    return draft(series=[series_of(name={"text": chr(65 + n % 26) * 255}, categories=None) for n in range(64)])


def wide_record():
    return convert(wide_draft(), SHEETS, ConversionReport())


class Records(unittest.TestCase):
    def test_for_sheet(self):
        report = ConversionReport()
        records = [record(0, "A"), record(1, "B")]
        found = for_sheet(records, 1, 3, 2, "Other", report)
        self.assertEqual([item["name"] for item in found], ["B"])
        item = found[0]
        self.assertEqual(item["sheet"], 0)
        self.assertEqual(item["anchor"], {"row": 1 + 2, "col": 3 + 1})
        self.assertEqual(item["spec"]["series"][0]["values"]["source"], {"kind": "range", "sheet": 0, "ref": "C4:C10"})
        self.assertEqual(records[1]["spec"]["series"][0]["values"]["source"]["ref"], "B2:B8")
        self.assertEqual(entries(report), [])

        crossing = record(0, "Cross")
        crossing["spec"]["series"][0]["values"]["source"]["sheet"] = 1
        self.assertEqual(for_sheet([crossing], 0, 1, 1, "Data", report), [])
        self.assertEqual(entries(report), [("chart.skipped", "source")])

        report = ConversionReport()
        self.assertEqual(for_sheet([record(0, "Low")], 0, 1048575, 1, "Data", report), [])
        self.assertEqual(for_sheet([record(0, "Wide")], 0, 1, 16384, "Data", report), [])
        far = record(0, "Anchor")
        far["spec"]["series"][0]["values"]["source"]["ref"] = "B2"
        far["anchor"] = {"row": 1, "col": 3}
        self.assertEqual(for_sheet([far], 0, 1048576, 1, "Data", report), [])
        self.assertEqual(entries(report), [("chart.skipped", "off_grid")])
        edge = record(0, "Edge")
        self.assertEqual(len(for_sheet([edge], 0, 1, 1, "Data", ConversionReport())), 1)

    def test_check_records(self):
        good = [record(0, "A"), record(1, "B")]
        self.assertEqual(check_records(copy.deepcopy(good), 2), good)
        self.assertEqual(check_records([], 2), [])

        def refused(value, count=2):
            with self.assertRaises(EngineError) as caught:
                check_records(value, count)
            self.assertEqual(caught.exception.body["code"], "INVALID_ARGUMENT")

        refused("nope")
        refused({"sheet": 0})
        refused([record(0, str(n)) for n in range(65)])
        extra = record(0)
        extra["colour"] = "red"
        refused([extra])
        refused([record(0)], 0)
        outside = record(0)
        outside["sheet"] = 2
        refused([outside])
        outside = record(0)
        outside["sheet"] = True
        refused([outside])
        bad = record(0)
        bad["spec"]["series"][0]["values"]["source"]["sheet"] = "Data"
        refused([bad])
        bad = record(0)
        bad["spec"]["series"][0]["values"]["source"]["sheet"] = 5
        refused([bad])

        def broken(change):
            made = record(0)
            change(made)
            refused([made])

        def source(made):
            return made["spec"]["series"][0]["values"]["source"]

        broken(lambda made: made.update(anchor="A1"))
        broken(lambda made: made.update(anchor={"row": 0, "col": 1}))
        broken(lambda made: made.update(anchor={"row": 1}))
        broken(lambda made: made.update(anchor={"row": 1.5, "col": 1}))
        broken(lambda made: made.update(offset={"x": -1, "y": 0}))
        broken(lambda made: made.update(offset={"x": 0, "y": 4097}))
        broken(lambda made: made.update(size={"width": 63, "height": 100}))
        broken(lambda made: made.update(size={"width": 100, "height": 4097}))
        broken(lambda made: made.update(size=None))
        broken(lambda made: made.update(name=""))
        broken(lambda made: made.update(name="n" * 65))
        broken(lambda made: made.update(name=5))
        broken(lambda made: source(made).pop("ref"))
        broken(lambda made: source(made).update(ref=7))
        broken(lambda made: source(made).update(ref="not a ref"))
        broken(lambda made: source(made).update(ref="Data!B2"))
        broken(lambda made: source(made).update(kind="table"))

    def test_check_records_refuses_malformed_refs(self):
        for ref in ("A1:", "", ":", "A1:B2:C3", "Data!A1", "A:A", "1:1", "A0", "XFE1", "A1048577", None, 5):
            made = record(0)
            made["spec"]["series"][0]["values"]["source"]["ref"] = ref
            with self.subTest(ref=ref), self.assertRaises(EngineError) as caught:
                check_records([made], 2)
            self.assertEqual(caught.exception.body["code"], "INVALID_ARGUMENT")

    def test_check_records_size_caps(self):
        def refused(records):
            with self.assertRaises(EngineError) as caught:
                check_records(records, 2)
            self.assertEqual(caught.exception.body["code"], "INVALID_ARGUMENT")
            self.assertIn("KiB", caught.exception.body["message"])

        big = record(0)
        big["spec"]["series"] = [dict(big["spec"]["series"][0], name={"text": "☃" * 255}) for _ in range(60)]
        self.assertGreater(len(encoded(big).encode()), RECORD_BYTES)
        refused([big])
        wide = wide_record()
        size = len(encoded(wide).encode())
        self.assertLessEqual(size, RECORD_BYTES)
        fits = RECORDS_BYTES // size
        self.assertEqual(len(check_records([wide] * fits, 2)), fits)
        refused([wide] * (fits + 1))

    def test_collector_total_budget(self):
        report = ConversionReport()
        collector = Collector()
        wide = wide_draft()
        size = len(encoded(convert(wide, SHEETS, ConversionReport())).encode())
        fits = RECORDS_BYTES // size
        self.assertLess(fits, charts.LIMIT_CHARTS)
        made = [collector.add(dict(wide, name=f"C{n}"), SHEETS, report) for n in range(fits + 2)]
        self.assertEqual(sum(item is not None for item in made), fits)
        self.assertLessEqual(sum(len(encoded(item).encode()) for item in collector.records), RECORDS_BYTES)
        skipped = [entry for entry in report.entries() if entry["category"] == "chart.skipped"]
        self.assertEqual([(entry["detail"], entry["count"], entry["samples"][0]["cell"]) for entry in skipped],
                         [("workbook_limit", 2, f"C{fits}")])
        # A small chart still fits in what is left.
        self.assertIsNotNone(collector.add(draft(name="Small"), SHEETS, report))

    def test_for_sheet_keeps_the_record_caps(self):
        report = ConversionReport()
        wide = wide_record()
        fits = RECORDS_BYTES // len(encoded(wide).encode())
        # Moving the sources down lengthens every reference; the records still pass check_records.
        kept = for_sheet([wide] * (fits + 1), 0, 1_000_000, 1_000, "Data", report)
        self.assertLessEqual(len(kept), fits)
        self.assertEqual(check_records(kept, 1), kept)
        self.assertIn(("chart.skipped", "workbook_limit"), entries(report))

    def test_summary(self):
        self.assertEqual(summary([record(0, "A"), record(1, "B")], SHEETS),
                         [{"name": "A", "sheet": "Data", "type": "column"}, {"name": "B", "sheet": "Other", "type": "column"}])


class Create(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.book = Workbook(pathlib.Path(self.tmp.name) / "x.dhwb", create=True)
        self.book.mutate({"action": "sheet_add", "operation_id": str(uuid.uuid4()),
                          "expected_state": self.book.state(), "name": "Other"})
        self.ids = [self.book.sheet("Sheet1"), self.book.sheet("Other")]

    def tearDown(self):
        self.book.close()
        self.tmp.cleanup()

    def run_create(self, records):
        self.book.changes = {}
        self.book.db.execute("BEGIN IMMEDIATE")
        try:
            result = create(self.book, records, self.ids)
            self.book.db.execute("COMMIT")
            return result
        except BaseException:
            self.book.db.execute("ROLLBACK")
            raise

    def listed(self):
        return charts.read(self.book, {"expected_state": self.book.state()})["charts"]

    def test_create(self):
        records = [record(0, "A"), record(1, "B"), record(0, "C")]
        self.assertEqual(self.run_create(records), {"created": 3, "left_out": []})
        found = {item["name"]: item for item in self.listed()}
        self.assertEqual((found["A"]["sheet_id"], found["B"]["sheet_id"], found["C"]["sheet_id"]),
                         (self.ids[0], self.ids[1], self.ids[0]))
        self.assertLess(found["A"]["z"], found["C"]["z"])
        self.assertEqual(found["B"]["spec"]["series"][0]["values"]["source"]["sheet_id"], self.ids[1])
        self.assertEqual(found["B"]["spec"]["series"][0]["values"]["source"]["ref"], "B2:B8")

    def test_duplicate_names_become_unique_on_a_sheet(self):
        self.run_create([record(0, "Chart 1"), record(0, "Chart 1"), record(1, "Chart 1")])
        self.assertEqual(sorted((item["sheet_id"] == self.ids[0], item["name"]) for item in self.listed()),
                         [(False, "Chart 1"), (True, "Chart 1"), (True, "Chart 1 (2)")])

    def test_workbook_limit_leaves_out_the_rest(self):
        self.run_create([record(0, f"Old {n}") for n in range(charts.LIMIT_CHARTS - 1)])
        result = self.run_create([record(0, "First"), record(0, "Second")])
        self.assertEqual(result, {"created": 1, "left_out": ["Second"]})
        self.assertEqual(len(self.listed()), charts.LIMIT_CHARTS)
