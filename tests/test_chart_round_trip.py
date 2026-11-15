# SPDX-License-Identifier: GPL-3.0-only
"""Charts survive a trip through export and import: a native workbook exported to XLSX and ODS (the inline route and the
streamed export job), imported again (the inline preview and the staged job), holds the same charts. Also the committed
two-sheet fixture and the three documented differences that come from the importer's rules."""
import base64
from itertools import product
from pathlib import Path
import tempfile
import unittest

from datahorizon.client import LocalService
from datahorizon.interchange import xlsx_preview
from datahorizon.ods import ods_preview

FORMATS = ("xlsx", "ods")
ROUTES = ("inline", "streamed")
PREVIEWS = {"xlsx": xlsx_preview, "ods": ods_preview}
FIXTURES = Path(__file__).resolve().parent / "fixtures"
CHART_WORDS = ("chart", "drawing")


def fill(book, sheet, scale=1):
    """Month, Sales, Cost and Units in A1:D13 of ``sheet``."""
    cells = [{"row": 1, "col": col, "source": text} for col, text in enumerate(("Month", "Sales", "Cost", "Units"), 1)]
    for row in range(2, 14):
        cells += [{"row": row, "col": 1, "source": f"M{row - 1}"}, {"row": row, "col": 2, "source": str(row * 10 * scale)},
                  {"row": row, "col": 3, "source": str(row * 4 * scale)}, {"row": row, "col": 4, "source": str(row * 3 + scale)}]
    book.apply("edit", sheet, cells=cells)


def range_source(sheet, ref):
    return {"source": {"kind": "range", "sheet": sheet, "ref": ref}}


def spec_of(kind, sheet="Data", grouping=None, title=None, legend="right", axes=None, series=(("B1", "B2:B13"), ("C1", "C2:C13"))):
    """A chart specification over A1:D13 of ``sheet``; every series is named by a cell reference."""
    built = {"spec_version": 1, "type": kind, "title": {"text": title}, "legend": {"position": legend},
             "series": [{"name": range_source(sheet, name), "values": range_source(sheet, values)} for name, values in series]}
    if kind != "scatter":
        built["categories"] = range_source(sheet, "A2:A13")
    else:
        for item in built["series"]:
            item["x"] = range_source(sheet, "D2:D13")
    if grouping:
        built["grouping"] = grouping
    if axes:
        built["axes"] = axes
    return built


AXES = {"category": {"title": "Month"}, "value": {"title": "Amount", "min": 0, "max": 500}}
# (name, sheet, specification, anchor, offset, size) for every supported type and grouping, on two sheets, with
# every title, axis, legend position and placement that a specification can carry.
CHARTS = [
    ("Column clustered", "Data", spec_of("column", grouping="clustered", title="Sales and cost", axes=AXES), (2, 6), (7, 9), (520, 310)),
    ("Column stacked", "Data", spec_of("column", grouping="stacked", title="Stacked", legend="bottom"), (20, 1), (0, 0), (480, 300)),
    ("Bar clustered", "Data", spec_of("bar", grouping="clustered", legend="none", axes={"category": {"title": None}, "value": {"title": "Amount", "min": None, "max": 400}}),
     (20, 8), (3, 4), (400, 350)),
    ("Bar stacked", "Data", spec_of("bar", grouping="stacked", title="Bars, stacked", axes={"category": {"title": "Month"}, "value": {"title": None, "min": 10, "max": None}}),
     (40, 1), (0, 12), (640, 280)),
    ("Line", "Data", spec_of("line", title="Trend", legend="bottom", series=(("B1", "B2:B13"), ("C1", "C2:C13"), ("D1", "D2:D13"))), (40, 9), (0, 0), (500, 300)),
    ("Area clustered", "Data", spec_of("area", grouping="clustered", title="Area"), (60, 1), (1, 1), (480, 300)),
    ("Area stacked", "Data", spec_of("area", grouping="stacked", title="Area, stacked", legend="bottom"), (60, 9), (0, 0), (480, 300)),
    ("Pie", "Other", spec_of("pie", sheet="Other", title="Share of sales", series=(("B1", "B2:B13"),)), (3, 2), (10, 20), (360, 360)),
    ("Scatter", "Other", spec_of("scatter", sheet="Other", title="Cost against units", axes={"category": {"title": "Units"}, "value": {"title": "Cost", "min": None, "max": None}}),
     (3, 10), (0, 0), (480, 300)),
    # A chart on one sheet whose sources are on another.
    ("Across sheets", "Other", spec_of("line", sheet="Data", title="Data on Other"), (30, 2), (0, 0), (480, 300)),
]


def sheet_names(book):
    book.refresh()
    return {sheet["id"]: sheet["name"] for sheet in book.info["sheets"]}


def stored_sources(chart):
    spec = chart["spec"]
    return ([spec["categories"]["source"]] if spec["categories"] else []) + [part["source"] for item in spec["series"]
                                                                              for part in (item["name"], item["values"], item["x"]) if part and "source" in part]


def comparable(book, text_names=True):
    """What a chart is, as the round trip must keep it: sources as (sheet name, ref), in the order of the sheets then
    the stacking order. ``text_names`` False replaces a series name given as text with None (ODS cannot carry it)."""
    names = sheet_names(book)
    order = {sheet["id"]: number for number, sheet in enumerate(book.info["sheets"])}

    def source(part):
        return (names[part["sheet_id"]], part["ref"]) if part["kind"] == "range" else part

    def series_name(item):
        if "source" in item["name"]:
            return source(item["name"]["source"])
        return item["name"]["text"] if text_names else None

    found = []
    for chart in sorted(book.charts(), key=lambda item: (order[item["sheet_id"]], item["z"])):
        spec = chart["spec"]
        found.append({"sheet": names[chart["sheet_id"]], "name": chart["name"], "anchor": chart["anchor"], "offset": chart["offset"],
                      "size": chart["size"], "type": spec["type"], "grouping": spec["grouping"], "title": spec["title"],
                      "axes": spec["axes"], "legend": spec["legend"],
                      "categories": spec["categories"] and source(spec["categories"]["source"]),
                      "series": [{"name": series_name(item), "values": source(item["values"]["source"]),
                                  "x": item["x"] and source(item["x"]["source"])} for item in spec["series"]]})
    return found


def cells_of(book):
    """Every stored cell as ``{sheet name: [(row, col, source)]}``, read through a whole-workbook XLSX preview."""
    result = book.export_spreadsheet("xlsx", scope="workbook", mode="values")
    preview = xlsx_preview({"file_base64": result["file_base64"], "scope": "workbook"})
    return {sheet["name"]: sorted((cell["row"], cell["col"], cell["source"]) for cell in sheet["cells"]) for sheet in preview["workbook_sheets"]}


def chart_entries(entries):
    """Report entries that speak of charts or drawings: the import must report none for what the export wrote."""
    return [entry for entry in entries if any(word in f"{entry['category']} {entry['detail']}".lower() for word in CHART_WORDS)]


class RoundTripCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.service = LocalService()
        cls.client = cls.service.__enter__()
        cls.count = 0

    @classmethod
    def tearDownClass(cls):
        cls.service.__exit__(None, None, None)
        cls.temp.cleanup()

    def new_book(self, label, sheets=("Data", "Other")):
        type(self).count += 1
        book = self.client.open(Path(self.temp.name) / f"{label}-{self.count}.dhwb", create=True)
        self.addCleanup(book.close)
        book.apply("sheet_rename", sheet="Sheet1", name=sheets[0])
        for name in sheets[1:]:
            book.apply("sheet_add", name=name)
        for number, name in enumerate(sheets, 1):
            fill(book, name, number)
        return book

    def travel(self, book, fmt, route, keep=False):
        """Export ``book`` to ``fmt`` by ``route`` and import the file into a new workbook by the matching import path
        (inline preview or staged job). Returns ``(snapshot, export result, import report entries)``; the snapshot holds
        the new workbook's charts (``charts``, and ``charts_without_text_names`` for ODS), its ``cells`` and, with ``keep``,
        the open ``book`` (the caller closes it). Few workbooks may be open at once, so the new one closes otherwise."""
        type(self).count += 1
        target = self.client.open(Path(self.temp.name) / f"target-{fmt}-{route}-{self.count}.dhwb", create=True)
        self.addCleanup(target.close)
        if route == "inline":
            result = book.export_spreadsheet(fmt, scope="workbook")
            preview = PREVIEWS[fmt]({"file_base64": result["file_base64"], "scope": "workbook"})
            self.assertTrue(preview["can_import"], preview["issues"])
            target.import_prepared(preview)
            entries = preview["report_entries"]
        else:
            path = Path(self.temp.name) / f"streamed-{fmt}-{self.count}.{fmt}"
            result = book.export_spreadsheet_file(path, format=fmt, overwrite=True)
            summary = self.client.prepare_spreadsheet_file(path, format=fmt)
            self.assertTrue(summary["can_import"], summary)
            target.import_staged(summary)
            entries = summary["report_entries"]
        snapshot = {"charts": comparable(target), "charts_without_text_names": comparable(target, False), "cells": cells_of(target)}
        if keep:
            snapshot["book"] = target
        else:
            target.close()
        return snapshot, result, entries


class ChartRoundTripTests(RoundTripCase):
    def test_every_supported_chart_round_trips(self):
        book = self.new_book("source")
        for name, sheet, spec, (row, col), (x, y), (width, height) in CHARTS:
            book.create_chart(spec, sheet=sheet, name=name, anchor={"row": row, "col": col}, offset={"x": x, "y": y}, size={"width": width, "height": height})
        expected = comparable(book)
        self.assertEqual(len(expected), len(CHARTS))
        self.assertEqual({(item["type"], item["grouping"]) for item in expected},
                         {("column", "clustered"), ("column", "stacked"), ("bar", "clustered"), ("bar", "stacked"), ("line", None),
                          ("area", "clustered"), ("area", "stacked"), ("pie", None), ("scatter", None)})
        for fmt, route in product(FORMATS, ROUTES):
            with self.subTest(fmt=fmt, route=route):
                again, result, entries = self.travel(book, fmt, route)
                self.assertIn(f"Charts: {len(CHARTS)} written.", result["report"])
                self.assertEqual(again["charts"], expected)
                self.assertEqual(chart_entries(entries), [])
                self.assertEqual(again["cells"], cells_of(book))

    def test_a_stacked_chart_survives_every_route(self):
        # XLSX writes stacked column and bar charts with full overlap; the importer must not report that as a dropped style.
        book = self.new_book("stacked", sheets=("Data",))
        book.create_chart(spec_of("column", grouping="stacked"), sheet="Data", name="Column")
        book.create_chart(spec_of("bar", grouping="stacked"), sheet="Data", name="Bar", anchor={"row": 30, "col": 1})
        for fmt, route in product(FORMATS, ROUTES):
            with self.subTest(fmt=fmt, route=route):
                again, _, entries = self.travel(book, fmt, route)
                self.assertEqual([(item["name"], item["grouping"]) for item in again["charts"]], [("Column", "stacked"), ("Bar", "stacked")])
                self.assertEqual([entry for entry in entries if "gap" in f"{entry['category']} {entry['detail']}".lower()], [])

    def test_ods_names_a_text_series_series_n(self):
        book = self.new_book("text-names", sheets=("Data",))
        spec = spec_of("column")
        spec["series"][0]["name"] = {"text": "Revenue"}
        book.create_chart(spec, sheet="Data", name="Texts")
        again, result, entries = self.travel(book, "ods", "inline")
        converted = [entry for entry in result["report_entries"] if entry["category"] == "chart.converted"]
        self.assertEqual([entry["detail"] for entry in converted], ["series_name"])
        self.assertEqual([sample["cell"] for sample in converted[0]["samples"]], ["Texts"])
        self.assertIn("Charts: 1 written.", result["report"])
        [chart] = again["charts"]
        self.assertEqual([item["name"] for item in chart["series"]], ["Series 1", ("Data", "C1")])
        # XLSX keeps both kinds.
        again, result, entries = self.travel(book, "xlsx", "inline")
        self.assertEqual([entry for entry in result["report_entries"] if entry["category"] == "chart.converted"], [])
        [chart] = again["charts"]
        self.assertEqual([item["name"] for item in chart["series"]], ["Revenue", ("Data", "C1")])


class FixtureRoundTripTests(RoundTripCase):
    def imported(self, data, fmt, label):
        path = Path(self.temp.name) / f"{label}.{fmt}"
        path.write_bytes(data)
        summary = self.client.prepare_spreadsheet_file(path, format=fmt)
        book = self.client.open(Path(self.temp.name) / f"{label}-{fmt}.dhwb", create=True)
        self.addCleanup(book.close)
        book.import_staged(summary)
        return book

    def test_fixture_file_round_trips(self):
        for name in ("charts-two-sheets", "charts-one-sheet"):
            data = (FIXTURES / f"{name}.xlsx").read_bytes()
            first = self.imported(data, "xlsx", f"{name}-first")
            charts, cells = comparable(first), cells_of(first)
            self.assertGreaterEqual(len(charts), 1)
            for fmt, route in product(FORMATS, ROUTES):
                with self.subTest(fixture=name, fmt=fmt, route=route):
                    second, result, entries = self.travel(first, fmt, route, keep=True)
                    self.addCleanup(second["book"].close)
                    self.assertEqual(chart_entries(entries), [])
                    self.assertIn(f"Charts: {len(charts)} written.", result["report"])
                    # A series named by text becomes "Series N" in ODS, so compare only what the format can carry.
                    key = "charts" if fmt == "xlsx" else "charts_without_text_names"
                    self.assertEqual(second[key], charts if fmt == "xlsx" else comparable(first, False))
                    self.assertEqual(second["cells"], cells)
                    # The third generation is what the second was.
                    third, _, _ = self.travel(second["book"], fmt, route)
                    self.assertEqual(third["charts"], second["charts"])
                    self.assertEqual(third["cells"], second["cells"])
                    second["book"].close()
            first.close()


class DocumentedDifferenceTests(RoundTripCase):
    def single(self, spec, name="Sales", sheet="Data"):
        book = self.new_book("difference", sheets=("Data",))
        book.create_chart(spec, sheet=sheet, name=name)
        return book

    def test_text_over_255_characters_is_cut(self):
        # A chart title is limited to 255 characters when it is made; a series name given as text and an axis title are not.
        spec = spec_of("column", axes={"category": {"title": "c" * 300}, "value": {"title": "v" * 256, "min": None, "max": None}})
        spec["series"][0]["name"] = {"text": "n" * 300}
        book = self.single(spec)
        for fmt, route in product(FORMATS, ROUTES):
            with self.subTest(fmt=fmt, route=route):
                again, _, _ = self.travel(book, fmt, route)
                [chart] = again["charts"]
                self.assertEqual(chart["axes"]["category"]["title"], "c" * 255)
                self.assertEqual(chart["axes"]["value"]["title"], "v" * 255)
                if fmt == "xlsx":
                    self.assertEqual(chart["series"][0]["name"], "n" * 255)
                else:
                    self.assertEqual(chart["series"][0]["name"], "Series 1")

    def test_duplicate_chart_names_are_made_unique(self):
        book = self.single(spec_of("column"))
        book.create_chart(spec_of("line"), sheet="Data", name="Sales", anchor={"row": 30, "col": 1})
        self.assertEqual([item["name"] for item in comparable(book)], ["Sales", "Sales"])
        for fmt, route in product(FORMATS, ROUTES):
            with self.subTest(fmt=fmt, route=route):
                again, _, entries = self.travel(book, fmt, route)
                found = again["charts"]
                self.assertEqual([item["type"] for item in found], ["column", "line"])
                self.assertEqual(found[0]["name"], "Sales")
                self.assertNotEqual(found[1]["name"], "Sales")
                self.assertTrue(found[1]["name"].startswith("Sales"), found[1]["name"])
                self.assertEqual(chart_entries(entries), [])

    def test_an_empty_series_name_comes_back_as_series_n(self):
        spec = spec_of("column")
        spec["series"][0]["name"] = {"text": ""}
        book = self.single(spec)
        for fmt, route in product(FORMATS, ROUTES):
            with self.subTest(fmt=fmt, route=route):
                again, _, _ = self.travel(book, fmt, route)
                [chart] = again["charts"]
                self.assertEqual([item["name"] for item in chart["series"]], ["Series 1", ("Data", "C1")])


if __name__ == "__main__":
    unittest.main()
