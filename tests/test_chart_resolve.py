# SPDX-License-Identifier: GPL-3.0-only
"""Tests for resolving stored chart sources and suggesting specifications."""

import copy
import pathlib
import tempfile
import unittest
import uuid
import xml.etree.ElementTree as ET

from datahorizon import chart_render, chart_resolve, charts
from datahorizon.engine import MAX_CELLS, EngineError, Workbook

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def source(ref, sheet="Sheet1"):
    return {"kind": "range", "sheet": sheet, "ref": ref}


def series(values, name="B1", x=None):
    return {"name": {"source": source(name)}, "values": {"source": source(values)},
            "x": None if x is None else {"source": source(x)}}


def code(caught):
    return getattr(caught, "exception", caught).body["code"]


class Resolve(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.book = Workbook(pathlib.Path(self.tmp.name) / "r.dhwb", create=True)
        self.sheet = self.book.sheet("Sheet1")
        cells = [("A1", "Month"), ("B1", "Units"), ("C1", "Cost")]
        for index, month in enumerate(MONTHS):
            cells += [(f"A{index + 2}", month), (f"B{index + 2}", str(index + 1)), (f"C{index + 2}", f"=B{index + 2}*2")]
        self.edit(cells)

    def tearDown(self):
        self.book.close()
        self.tmp.cleanup()

    def edit(self, pairs, sheet="Sheet1"):
        cells = []
        for ref, text in pairs:
            letters = ref.rstrip("0123456789")
            col = 0
            for letter in letters:
                col = col * 26 + ord(letter) - 64
            cells.append({"row": int(ref[len(letters):]), "col": col, "source": text})
        self.book.mutate({"action": "edit", "sheet": sheet, "cells": cells, "operation_id": str(uuid.uuid4()),
                          "expected_state": self.book.state()})

    def spec(self, type_="column", **overrides):
        base = {"spec_version": 1, "type": type_, "categories": {"source": source("A2:A13")},
                "series": [series("B2:B13")]}
        base.update(overrides)
        return base

    def payload(self, spec=None):
        stored = charts.normalise_spec(self.book, spec or self.spec())
        return {"name": "Chart", "anchor": {"row": 1, "col": 1}, "offset": {"x": 0, "y": 0},
                "size": {"width": 480, "height": 320}, "z": 0, "spec_version": 1, "spec": stored}

    def resolve(self, payload=None, **kwargs):
        return chart_resolve.resolve(self.book, payload or self.payload(), self.book.state(), **kwargs)

    def test_resolves_stored_values_into_the_renderer_shape(self):
        out = self.resolve()
        self.assertEqual(out["resolved"]["categories"][:2], ["Jan", "Feb"])
        self.assertEqual(out["resolved"]["series"][0],
                         {"name": "Units", "values": [float(v) for v in range(1, 13)], "x": None})
        self.assertEqual(out["state"], self.book.state())
        chart_render.render(out["resolved"], 480, 300)

    def test_gaps_are_counted_by_cause(self):
        self.edit([("B4", "text"), ("B5", "=1/0")])
        self.book.mutate({"action": "edit", "sheet": "Sheet1", "cells": [{"row": 3, "col": 2, "source": ""}],
                          "operation_id": str(uuid.uuid4()), "expected_state": self.book.state()})
        out = self.resolve()
        self.assertEqual(out["counts"], {"points": 9, "gaps": {"empty": 1, "text": 1, "error": 1}})
        values = out["resolved"]["series"][0]["values"]
        self.assertEqual([values[1], values[2], values[3]], [None, None, None])
        self.assertEqual(values[4], 5.0)  # a value after a gap keeps its index

    def test_boolean_and_temporal_values_are_text_gaps_and_errors_are_errors(self):
        self.edit([("B2", "=TRUE"), ("B3", "=DATE(2024,1,2)"), ("B4", "=1/0")])
        out = self.resolve()
        self.assertEqual(out["counts"], {"points": 9, "gaps": {"empty": 0, "text": 2, "error": 1}})
        self.assertEqual(out["resolved"]["series"][0]["values"][:4], [None, None, None, 4.0])

    def test_formula_sources_are_calculated_without_a_viewport(self):
        cells = [("D1", "Derived")]
        for row in range(2, 14):
            cells += [(f"D{row}", f"=B{row}+1"), (f"E{row}", f"=D{row}*2")]
        self.edit(cells)
        payload = self.payload(self.spec(series=[series("D2:D13", name="D1"), series("E2:E13", name="D1")]))
        path = pathlib.Path(self.tmp.name) / "r.dhwb"
        self.book.close()
        self.book = Workbook(path, readonly=True)  # never read or calculated anything
        cached = self.book.read("Sheet1", {"r1": 2, "c1": 4, "r2": 13, "c2": 5, "calculation": "cached"})
        self.assertEqual({cell["calculation"] for cell in cached["cells"]}, {"pending"})
        out = chart_resolve.resolve(self.book, payload, self.book.state())
        first, second = out["resolved"]["series"]
        self.assertEqual(first["values"], [float(v + 1) for v in range(1, 13)])
        self.assertEqual(second["values"], [float((v + 1) * 2) for v in range(1, 13)])

    def test_calculation_that_cannot_finish_fails_with_no_series(self):
        real = self.book.read
        calls = []

        def slow_second(*args, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                raise EngineError("TIMEOUT", "too slow")
            return real(*args, **kwargs)
        self.book.read = slow_second
        self.addCleanup(setattr, self.book, "read", real)
        with self.assertRaises(EngineError) as caught:
            self.resolve()
        self.assertEqual((code(caught), caught.exception.status), ("CALCULATION_FAILED", 409))
        self.assertEqual(len(calls), 2)  # no read after the failure

    def test_other_read_errors_propagate(self):
        def limit(*args, **kwargs):
            raise EngineError("READ_LIMIT", "too big")
        self.book.read = limit
        with self.assertRaises(EngineError) as caught:
            self.resolve()
        self.assertEqual(code(caught), "READ_LIMIT")

    def counted_reads(self, fail_at=None):
        """Replace the book's read with one that records each call and can time out on call ``fail_at``."""
        calls = []
        real = self.book.read

        def counting(*args, **kwargs):
            calls.append((args, kwargs))
            if len(calls) == fail_at:
                raise EngineError("TIMEOUT", "too slow")
            return real(*args, **kwargs)
        self.book.read = counting
        self.addCleanup(setattr, self.book, "read", real)
        return calls

    def wide_payload(self):
        wide = self.payload(self.spec(series=[series("B2:B13")]))
        wide["spec"]["series"][0]["values"]["source"].update(row1=1, row2=10_000, col1=5, col2=5)
        return wide

    def test_each_source_is_read_in_slices_of_at_most_1000_cells(self):
        self.assertEqual(chart_resolve.SLICE_CELLS, 1_000)
        calls = self.counted_reads()
        out = self.resolve(self.wide_payload())
        self.assertEqual(len(calls), 12)  # categories 1, name 1, values 10
        self.assertTrue(all(kwargs["limit"] == MAX_CELLS for _, kwargs in calls))
        slices = [(args[1]["r1"], args[1]["r2"]) for args, _ in calls[2:]]
        self.assertEqual(slices, [(start, start + 999) for start in range(1, 10_001, 1_000)])
        self.assertEqual(len(out["resolved"]["series"][0]["values"]), 10_000)
        self.assertEqual(out["counts"]["gaps"]["empty"], 10_000)

    def test_a_row_source_is_sliced_along_the_row(self):
        wide = self.payload(self.spec(categories=None, series=[{"name": {"text": "Row"}, "values": {"source": source("B20:B20")}}]))
        wide["spec"]["series"][0]["values"]["source"].update(col1=1, col2=2_500)
        calls = self.counted_reads()
        out = self.resolve(wide)
        self.assertEqual([(args[1]["c1"], args[1]["c2"]) for args, _ in calls], [(1, 1000), (1001, 2000), (2001, 2500)])
        self.assertEqual(len(out["resolved"]["series"][0]["values"]), 2_500)

    def test_a_timeout_on_a_later_slice_fails_with_no_series(self):
        calls = self.counted_reads(fail_at=6)
        with self.assertRaises(EngineError) as caught:
            self.resolve(self.wide_payload())
        self.assertEqual((code(caught), caught.exception.status), ("CALCULATION_FAILED", 409))
        self.assertEqual(len(calls), 6)  # no read after the failure

    def test_a_calculation_budget_error_fails_the_resolve(self):
        real = self.book.read

        def exhausted(*args, **kwargs):
            result = real(*args, **kwargs)
            for cell in result["cells"]:
                if cell["col"] in (2, 3) and cell["row"] == 5:
                    cell.update(value=None, display="#LIMIT!", calculation="error", error=chart_resolve.BUDGET_ERROR)
            return result
        self.book.read = exhausted
        self.addCleanup(setattr, self.book, "read", real)
        for spec in (self.spec(), self.spec("scatter", categories=None, series=[series("B2:B13", x="C2:C13")]),
                     self.spec("scatter", categories=None, series=[series("C2:C13", x="B2:B13")])):
            with self.subTest(spec=spec["type"]), self.assertRaises(EngineError) as caught:
                self.resolve(self.payload(spec))
            self.assertEqual((code(caught), caught.exception.status), ("CALCULATION_FAILED", 409))

    def test_other_limit_errors_stay_counted_gaps(self):
        real = self.book.read

        def chain(*args, **kwargs):
            result = real(*args, **kwargs)
            for cell in result["cells"]:
                if cell["col"] == 2 and cell["row"] == 5:
                    cell.update(value=None, display="#LIMIT!", calculation="error", error="Dependency chain exceeds 128 cells")
            return result
        self.book.read = chain
        self.addCleanup(setattr, self.book, "read", real)
        self.assertEqual(self.resolve()["counts"]["gaps"]["error"], 1)

    def test_checkpoint_follows_each_read(self):
        seen = []
        self.resolve(checkpoint=lambda cells, message: seen.append((cells, message)))
        self.assertEqual([message for _, message in seen], ["Reading sources"] * 3)
        self.assertEqual([cells for cells, _ in seen], [12, 13, 25])

    def test_cancelled_checkpoint_propagates(self):
        class Cancelled(Exception):
            pass

        def checkpoint(cells, message):
            raise Cancelled()
        with self.assertRaises(Cancelled):
            self.resolve(checkpoint=checkpoint)

    def test_state_change_between_reads_is_stale(self):
        real = self.book.read
        calls = []

        def changing(*args, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                self.edit([("D1", "x")])
            return real(*args, **kwargs)
        self.book.read = changing
        with self.assertRaises(EngineError) as caught:
            self.resolve()
        self.assertEqual((code(caught), caught.exception.status), ("STALE_STATE", 409))

    def test_broken_source_and_issue_are_refused(self):
        broken = self.payload()
        broken["spec"]["series"][0]["values"]["source"].update(sheet_id=None, row1=None, col1=None, row2=None, col2=None)
        calls = []
        self.book.read = lambda *a, **k: calls.append(1)
        with self.assertRaises(EngineError) as caught:
            self.resolve(broken)
        self.assertEqual((code(caught), caught.exception.status), ("SOURCE_BROKEN", 409))
        self.assertEqual(calls, [])
        gone = self.payload()
        gone["spec"]["categories"]["source"]["sheet_id"] = str(uuid.uuid4())
        with self.assertRaises(EngineError) as caught:
            self.resolve(gone)
        self.assertEqual(code(caught), "SOURCE_BROKEN")
        invalid = self.payload()
        invalid["spec"]["type"] = "donut"
        with self.assertRaises(EngineError) as caught:
            self.resolve(invalid)
        self.assertEqual((code(caught), caught.exception.status), ("INVALID_SPEC", 400))
        future = self.payload()
        future["spec_version"] = future["spec"]["spec_version"] = 2
        with self.assertRaises(EngineError) as caught:
            self.resolve(future)
        self.assertEqual((code(caught), caught.exception.status), ("UNSUPPORTED_SPEC_VERSION", 409))

    def test_scatter_reads_x(self):
        spec = {"spec_version": 1, "type": "scatter", "series": [series("C2:C13", x="B2:B13")]}
        out = self.resolve(self.payload(spec))
        item = out["resolved"]["series"][0]
        self.assertEqual(item["x"], [float(v) for v in range(1, 13)])
        self.assertEqual(item["values"][0], 2.0)
        self.assertIsNone(out["resolved"]["categories"])

    def test_pie_negative_is_refused(self):
        self.edit([("B4", "-3")])
        with self.assertRaises(EngineError) as caught:
            self.resolve(self.payload(self.spec("pie")))
        self.assertEqual((code(caught), caught.exception.status), ("NEGATIVE_IN_PIE", 400))

    def test_title_axes_legend_and_grouping_pass_through(self):
        spec = self.spec(title={"text": "Sales"}, grouping="stacked", legend={"position": "bottom"},
                         axes={"category": {"title": "Month"}, "value": {"title": "n", "min": 0, "max": 20}})
        resolved = self.resolve(self.payload(spec))["resolved"]
        self.assertEqual((resolved["title"], resolved["grouping"], resolved["legend"], resolved["type"]),
                         ("Sales", "stacked", {"position": "bottom"}, "column"))
        self.assertEqual(resolved["axes"]["value"], {"title": "n", "min": 0, "max": 20})
        self.assertEqual(resolved["axes"]["category"], {"title": "Month"})
        self.assertIsNone(self.resolve()["resolved"]["title"])

    def test_every_type_resolves_and_renders(self):
        specs = [self.spec(kind) for kind in ("column", "bar", "line", "area", "pie")]
        specs.append({"spec_version": 1, "type": "scatter", "series": [series("C2:C13", x="B2:B13")]})
        for spec in specs:
            with self.subTest(type=spec["type"]):
                resolved = self.resolve(self.payload(spec))["resolved"]
                rendered = chart_render.render(resolved, 480, 300)
                ET.fromstring(rendered["svg"])
                self.assertTrue(rendered["marks"])


class Suggest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.book = Workbook(pathlib.Path(self.tmp.name) / "s.dhwb", create=True)
        cells = [(1, 1, "Month"), (1, 2, "Units"), (1, 3, "Cost")]
        for index, month in enumerate(MONTHS):
            cells += [(index + 2, 1, month), (index + 2, 2, str(index + 1)), (index + 2, 3, str(index * 3))]
        self.write(cells)

    def tearDown(self):
        self.book.close()
        self.tmp.cleanup()

    def write(self, cells):
        self.book.mutate({"action": "edit", "sheet": "Sheet1", "operation_id": str(uuid.uuid4()),
                          "expected_state": self.book.state(),
                          "cells": [{"row": r, "col": c, "source": s} for r, c, s in cells]})

    def suggest(self, **args):
        args = dict(dict(expected_state=self.book.state(), sheet="Sheet1", r1=1, c1=1, r2=13, c2=3), **args)
        return chart_resolve.suggest(self.book, args)

    def test_suggest_detects_headers_and_categories(self):
        out = self.suggest()
        spec = out["spec"]
        self.assertEqual(spec["categories"], {"source": source("A2:A13")})
        self.assertEqual([x["name"] for x in spec["series"]], [{"source": source("B1")}, {"source": source("C1")}])
        self.assertEqual([x["values"] for x in spec["series"]], [{"source": source("B2:B13")}, {"source": source("C2:C13")}])
        self.assertEqual((spec["spec_version"], spec["type"], spec["title"]), (1, "column", {"text": None}))
        charts.normalise_spec(self.book, spec)

    def test_suggest_without_headers_names_series_by_position(self):
        out = self.suggest(r1=2, c1=2, r2=13, c2=3)
        spec = out["spec"]
        self.assertIsNone(spec["categories"])
        self.assertEqual([x["name"] for x in spec["series"]], [{"text": "Series 1"}, {"text": "Series 2"}])
        self.assertEqual(spec["series"][0]["values"], {"source": source("B2:B13")})
        charts.normalise_spec(self.book, spec)

    def test_suggest_scatter_and_pie(self):
        scatter = self.suggest(type="scatter", c1=2)["spec"]
        self.assertNotIn("categories", scatter)
        self.assertEqual([x["x"] for x in scatter["series"]], [{"source": source("B2:B13")}])
        self.assertEqual([x["values"] for x in scatter["series"]], [{"source": source("C2:C13")}])
        charts.normalise_spec(self.book, scatter)
        pie = self.suggest(type="pie")["spec"]
        self.assertEqual(len(pie["series"]), 1)
        self.assertEqual(pie["series"][0]["values"], {"source": source("B2:B13")})
        charts.normalise_spec(self.book, pie)

    def test_suggest_refusals(self):
        with self.assertRaises(EngineError) as caught:
            chart_resolve.suggest(self.book, dict(expected_state="0:0", sheet="Sheet1", r1=1, c1=1, r2=2, c2=2))
        self.assertEqual((code(caught), caught.exception.status), ("STALE_STATE", 409))
        with self.assertRaises(EngineError) as caught:
            self.suggest(r2=10_001, c2=2)
        self.assertEqual(code(caught), "LIMIT")
        wide = [(1, col, f"h{col}") for col in range(2, 70)] + [(2, col, "1") for col in range(2, 70)]
        self.write(wide)
        with self.assertRaises(EngineError) as caught:
            self.suggest(r1=1, c1=2, r2=2, c2=69)
        self.assertEqual(code(caught), "LIMIT_SERIES")
        with self.assertRaises(EngineError) as caught:
            self.suggest(c1=1, c2=1)
        self.assertEqual(code(caught), "INVALID_SPEC")


if __name__ == "__main__":
    unittest.main()
