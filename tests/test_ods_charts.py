# SPDX-License-Identifier: GPL-3.0-only
import io
import unittest
import zipfile

from chart_fixtures import ods_chart_xml, ods_title, ods_with_charts, sample_like
from datahorizon import ods_charts
from datahorizon.conversion_report import ConversionReport
from datahorizon.import_charts import MAX_DRAFT_SERIES, UNUSABLE, convert
from datahorizon.interchange import read_xml
from datahorizon.ods import O, T, q
from datahorizon.ods_charts import read_frame_draft
from datahorizon.ods_features import DRAW
from datahorizon.xlsx_charts import read_sheet_drafts
from datahorizon.xlsx_stream import ByteBudget

SHEET = "Data"
SERIES = [{"name_ref": "Data!$B$1", "cat": "Data!$A$2:$A$8", "val": "Data!$B$2:$B$8"}]
FRAME, REPEAT_ROWS, REPEAT_COLS = q(DRAW, "frame"), q(T, "number-rows-repeated"), q(T, "number-columns-repeated")
BIG = 64 * 1024 * 1024


def read_part(archive, name):
    return read_xml(archive, name)


def frames(archive):
    """``(sheet name, frame element, cell)`` for each draw:frame of content.xml, as a caller walking it finds them."""
    root = read_xml(archive, "content.xml")
    found = []
    for table in root.iter(q(T, "table")):
        name = table.get(q(T, "name"))
        for shapes in table.iter(q(T, "shapes")):
            found += [(name, frame, None) for frame in shapes.iter(FRAME)]
        row = 0
        for line in table.iter(q(T, "table-row")):
            row += int(line.get(REPEAT_ROWS, "1"))
            col = 0
            for cell in line:
                col += int(cell.get(REPEAT_COLS, "1"))
                found += [(name, frame, (row, col)) for frame in cell.iter(FRAME)]
    return found


def read(data, widths=None, order=None, report=None):
    """Drafts (``None`` for an unread frame) of every frame of a package, with the report."""
    archive = zipfile.ZipFile(io.BytesIO(data))
    report = report or ConversionReport()
    drafts = []
    for number, (name, frame, cell) in enumerate(frames(archive)):
        sheets = [item.get(q(T, "name")) for item in read_xml(archive, "content.xml").iter(q(T, "table"))]
        drafts.append(read_frame_draft(archive, frame, sheets.index(name), name, number if order is None else order,
                                       read_part, report, cell, widths))
    return drafts, report


def one(xml, **placement):
    drafts, report = read(ods_with_charts({SHEET: ""}, [{"sheet": SHEET, "xml": xml, **placement}]))
    assert len(drafts) == 1 and drafts[0] is not None, report.entries()
    return drafts[0]


def skipped(report):
    return [(entry["detail"], entry["samples"]) for entry in report.entries() if entry["category"] == "chart.skipped"]


class SampleTests(unittest.TestCase):
    def test_sample_like_matches_xlsx(self):
        first, second = "Column Chart", "Stacked Column"
        one_series = [{"name_ref": f"'{first}'!$B$1", "cat": f"'{first}'!$A$2:$A$8", "val": f"'{first}'!$B$2:$B$8"}]
        four = [{"name_ref": f"'{second}'!${chr(66 + n)}$1", "cat": f"'{second}'!$A$2:$A$8",
                 "val": f"'{second}'!${chr(66 + n)}$2:${chr(66 + n)}$8"} for n in range(4)]
        # C1 plus 632114 and 154565 EMU is 66 and 16 pixels into the cell.
        place = {"cell": (1, 3), "x": "66px", "y": "16px", "width": "448px", "height": "300px"}
        data = ods_with_charts(
            {first: "", second: ""},
            [{"sheet": first, "name": "Chart 1", "xml": ods_chart_xml("bar", "col", "clustered", one_series,
                                                                      "Sales by Division", "end"), **place},
             {"sheet": second, "name": "Chart 2", "xml": ods_chart_xml("bar", "col", "stacked", four,
                                                                       "Sales by Quarter", "end"), **place}])
        drafts, report = read(data)
        self.assertEqual(report.entries(), [])
        sample = sample_like()
        expected = []
        for number, name in enumerate((first, second), 1):
            part, _ = _xlsx_drafts(sample, number, name)
            expected += part
        self.assertEqual(len(drafts), 2)
        for ods, xlsx in zip(drafts, expected):
            for key in ("plots", "title", "series", "legend", "anchor"):
                self.assertEqual(ods[key], xlsx[key], key)
        self.assertEqual(drafts[0]["offset"], {"x": 66, "y": 16})
        self.assertEqual((drafts[1]["sheet"], drafts[1]["name"], drafts[1]["order"]), (1, "Chart 2", 1))
        self.assertEqual(drafts[0]["series"][0]["name"], {"ref": ("Column Chart", 1, 2, 1, 2)})


def _xlsx_drafts(data, number, name):
    archive = zipfile.ZipFile(io.BytesIO(data))
    report = ConversionReport()
    return read_sheet_drafts(archive, f"xl/worksheets/sheet{number}.xml", number - 1, name, ByteBudget(BIG), report), report


class ChartPartTests(unittest.TestCase):
    def test_class_and_style_mapping(self):
        def plot(**options):
            return one(ods_chart_xml(series=SERIES, **options))["plots"][0]
        self.assertEqual(plot(kind="bar", direction="bar")["direction"], "bar")
        self.assertEqual(plot(kind="bar")["direction"], "col")
        self.assertEqual(plot(kind="bar")["grouping"], "clustered")
        self.assertEqual(plot(kind="bar", grouping="stacked")["grouping"], "stacked")
        self.assertEqual(plot(kind="bar", grouping="percentStacked")["grouping"], "percentStacked")
        self.assertEqual(plot(kind="line", grouping="standard")["grouping"], "standard")
        self.assertEqual(plot(kind="area", grouping="stacked")["grouping"], "stacked")
        three = plot(kind="bar3D")
        self.assertEqual((three["kind"], three["three_d"]), ("bar", True))
        self.assertFalse(plot(kind="bar")["three_d"])
        for kind, expected in (("line", "line"), ("area", "area"), ("pie", "pie"), ("doughnut", "doughnut"),
                               ("scatter", "scatter"), ("radar", "radar")):
            found = plot(kind=kind, grouping=None)
            self.assertEqual((found["kind"], found["direction"], found["secondary"]), (expected, None, False), kind)
        self.assertIsNone(plot(kind="pie", grouping="stacked")["grouping"])
        self.assertEqual(plot(kind="stock", grouping=None)["kind"], "stock")

    def test_series_sources(self):
        chart = one(ods_chart_xml(series=SERIES + [{"val": "Data!$C$2:$C$8"}]))
        first, second = chart["series"]
        self.assertEqual(first["values"], ("Data", 2, 2, 8, 2))
        self.assertEqual(first["name"], {"ref": ("Data", 1, 2, 1, 2)})
        self.assertEqual(first["categories"], ("Data", 2, 1, 8, 1))
        self.assertEqual(second["categories"], ("Data", 2, 1, 8, 1))
        self.assertIsNone(second["name"])
        self.assertIsNone(first["x"])
        scatter = one(ods_chart_xml("scatter", series=[{"x": "Data!$A$2:$A$8", "val": "Data!$B$2:$B$8", "cat": "Data!$C$2:$C$8"}]))
        self.assertEqual(scatter["series"][0]["x"], ("Data", 2, 1, 8, 1))
        self.assertEqual(scatter["series"][0]["values"], ("Data", 2, 2, 8, 2))
        self.assertIsNone(scatter["series"][0]["categories"])
        quoted = one(ods_chart_xml(series=[{"val": "'Bob''s Q1'!$B$2:$B$8"}]))
        self.assertEqual(quoted["series"][0]["values"], ("Bob's Q1", 2, 2, 8, 2))
        self.assertIsNone(quoted["series"][0]["categories"])
        multi = one(ods_chart_xml(series=[{"val": "Data!$B$2:$B$3 Data!$B$5:$B$6"}]))
        self.assertIs(multi["series"][0]["values"], UNUSABLE)
        absent = one(ods_chart_xml(series=[{"name_ref": "Data!$B$1"}]))
        self.assertIsNone(absent["series"][0]["values"])

    def test_series_with_own_chart_class_is_combination(self):
        series = [{"val": "Data!$B$2:$B$8"}, {"val": "Data!$C$2:$C$8", "cls": "line"}, {"val": "Data!$D$2:$D$8"}]
        chart = one(ods_chart_xml(series=series))
        self.assertEqual([(item["kind"], item["grouping"], item["secondary"]) for item in chart["plots"]],
                         [("bar", "clustered", False), ("line", "standard", False)])
        self.assertEqual([item["values"][2] for item in chart["series"]], [2, 3, 4])
        same = one(ods_chart_xml(series=[{"val": "Data!$B$2:$B$8", "cls": "bar"}]))
        self.assertEqual(len(same["plots"]), 1)

    def test_secondary_axis(self):
        series = [{"val": "Data!$B$2:$B$8", "axis": "primary-y"}, {"val": "Data!$C$2:$C$8", "axis": "secondary-y"}]
        chart = one(ods_chart_xml(series=series, secondary=True))
        self.assertEqual([(item["kind"], item["secondary"]) for item in chart["plots"]], [("bar", False), ("bar", True)])
        self.assertEqual(len(chart["series"]), 2)
        chart = one(ods_chart_xml(series=[{"val": "Data!$B$2:$B$8", "axis": "unknown"}], secondary=True))
        self.assertEqual([item["secondary"] for item in chart["plots"]], [False])

    def test_legend_and_titles(self):
        for position, expected in (("end", "right"), ("bottom", "bottom"), ("top", "top"), ("start", "left")):
            self.assertEqual(one(ods_chart_xml(series=SERIES, legend=position))["legend"], expected)
        self.assertIsNone(one(ods_chart_xml(series=SERIES))["legend"])
        chart = one(ods_chart_xml(series=SERIES, title="Sales"))
        self.assertEqual(chart["title"], {"text": "Sales", "from_cell": False})
        self.assertEqual(one(ods_chart_xml(series=SERIES))["title"], {"text": None, "from_cell": False})
        two = '<chart:title><text:p>One</text:p><text:p>Two <text:span>more</text:span></text:p></chart:title>'
        self.assertEqual(one(ods_chart_xml(series=SERIES).replace("<chart:plot-area", two + "<chart:plot-area", 1))
                         ["title"]["text"], "One\nTwo more")
        chart = one(ods_chart_xml(series=SERIES, category_axis=ods_title("Division"), value_axis=ods_title("Dollars"),
                                  value_min=-2.5, value_max=100))
        self.assertEqual(chart["axis_titles"]["category"], {"text": "Division", "from_cell": False})
        self.assertEqual(chart["axis_titles"]["value"], {"text": "Dollars", "from_cell": False})
        self.assertEqual((chart["value_min"], chart["value_max"]), (-2.5, 100.0))
        self.assertIsInstance(chart["value_max"], float)
        chart = one(ods_chart_xml(series=SERIES, value_min="nan", value_max="x"))
        self.assertEqual((chart["value_min"], chart["value_max"]), (None, None))
        plain = one(ods_chart_xml(series=SERIES))
        self.assertEqual((plain["value_min"], plain["value_max"]), (None, None))
        self.assertEqual(plain["axis_titles"]["value"], {"text": None, "from_cell": False})

    def test_styled_detection(self):
        def styled(series=SERIES, **options):
            return one(ods_chart_xml(series=series, **options))["styled"]
        def with_style(inner):
            return [{**SERIES[0], "sppr": inner}]
        self.assertEqual(styled(), [])
        self.assertEqual(styled(with_style('<style:graphic-properties draw:fill-color="#ff0000"/>')), ["colours"])
        self.assertEqual(styled(with_style('<style:chart-properties chart:data-label-number="none"/>')), [])
        self.assertEqual(styled(with_style('<style:chart-properties chart:data-label-number="value"/>')), ["data labels"])
        self.assertEqual(styled(axis_extras='chart:gap-width="150"'), ["gap width"])
        self.assertEqual(styled(axis_extras='chart:overlap="-10"'), ["gap width"])
        self.assertEqual(styled(extras='chart:symbol-type="none"'), [])
        self.assertEqual(styled(extras='chart:symbol-type="automatic"'), ["markers"])
        self.assertEqual(styled(extras='chart:interpolation="none"'), [])
        self.assertEqual(styled(extras='chart:interpolation="cubic-spline"'), ["smoothing"])
        self.assertEqual(styled(value_axis='<chart:grid chart:class="major"/>'), ["gridlines"])
        self.assertEqual(styled(with_style('<style:chart-properties chart:data-label-number="value"/>'
                                           '<style:graphic-properties draw:fill-color="#ff0000"/>'),
                                extras='chart:interpolation="b-spline"', value_axis="<chart:grid/>"),
                         ["colours", "data labels", "smoothing", "gridlines"])


class StyleAndBoundTests(unittest.TestCase):
    def test_more_styling_is_reported(self):
        def styled(**options):
            return one(ods_chart_xml(series=options.pop("series", SERIES), **options))["styled"]
        for attribute in ("svg:stroke-color", "draw:stroke-color"):
            line = [{**SERIES[0], "sppr": f'<style:graphic-properties {attribute}="#ff0000"/>'}]
            self.assertEqual(styled(series=line), ["colours"], attribute)
            self.assertEqual(styled(chart_style=f'<style:graphic-properties {attribute}="#ff0000"/>'), ["colours"])
        self.assertEqual(styled(chart_style='<style:graphic-properties draw:stroke="none" svg:stroke-color="#b3b3b3"/>'), [])
        text = '<style:text-properties {}="x"/>'
        for attribute in ("fo:font-size", "fo:font-family", "style:font-name", "fo:font-weight", "fo:font-style", "fo:color"):
            self.assertEqual(styled(chart_style=text.format(attribute)), ["fonts"], attribute)
        self.assertEqual(styled(axis_style=text.format("fo:font-size")), ["fonts"])
        self.assertEqual(styled(title="T", title_style=text.format("fo:color")), ["fonts"])
        self.assertEqual(styled(legend="end", legend_style=text.format("fo:font-weight")), ["fonts"])
        self.assertEqual(styled(chart_style='<style:text-properties fo:language="en"/>'), [])
        self.assertEqual(styled(axis_attributes='style:data-style-name="N10"'), ["number formats"])
        self.assertEqual(styled(series=[{**SERIES[0], "style_attributes": 'style:data-style-name="N10"'}]), ["number formats"])

    def test_title_from_cell(self):
        chart = one(ods_chart_xml(series=SERIES, title="Cached", title_attributes='table:cell-range="$Data.$D$1"'))
        self.assertEqual(chart["title"], {"text": "Cached", "from_cell": True})
        chart = one(ods_chart_xml(series=SERIES, category_axis=ods_title("Axis", 'table:cell-range="$Data.$D$2"'),
                                  value_axis=ods_title("Plain")))
        self.assertEqual(chart["axis_titles"]["category"], {"text": "Axis", "from_cell": True})
        self.assertEqual(chart["axis_titles"]["value"], {"text": "Plain", "from_cell": False})

    def test_series_cap(self):
        many = [{"val": f"Data!$B$2:$B${n % 7 + 3}"} for n in range(5000)]
        chart = one(ods_chart_xml(series=many))
        self.assertEqual(len(chart["series"]), MAX_DRAFT_SERIES)
        report = ConversionReport()
        self.assertIsNone(convert(chart, [SHEET], report))
        self.assertEqual([item[0] for item in skipped(report)], ["limit"])
        classes = [{"val": "Data!$B$2:$B$8", "cls": f"kind{n}"} for n in range(5000)]
        chart = one(ods_chart_xml(series=classes))
        self.assertEqual((len(chart["series"]), len(chart["plots"])), (MAX_DRAFT_SERIES, MAX_DRAFT_SERIES))


class PlacementTests(unittest.TestCase):
    def test_placement(self):
        chart = one(ods_chart_xml(series=SERIES), x="2.54cm", y="1.27cm", width="12.7cm", height="7.62cm")
        self.assertEqual((chart["anchor"], chart["offset"]), ({"row": 3, "col": 2}, {"x": 32, "y": 8}))
        self.assertEqual(chart["size"], {"width": 480, "height": 288})
        self.assertTrue(chart["absolute_anchor"])
        self.assertFalse(chart["approximate_size"])
        self.assertEqual((chart["sheet"], chart["name"], chart["order"]), (0, "Chart 1", 0))
        chart = one(ods_chart_xml(series=SERIES), cell=(2, 4), x="0.5cm", y="0.25cm", width="12.7cm", height="7.62cm")
        self.assertEqual((chart["anchor"], chart["offset"]), ({"row": 2, "col": 4}, {"x": 19, "y": 9}))
        self.assertEqual(chart["size"], {"width": 480, "height": 288})
        self.assertFalse(chart["absolute_anchor"])
        chart = one(ods_chart_xml(series=SERIES), cell=(2, 3), x="0.5cm", y="0cm", width="12.7cm", height="7.62cm")
        self.assertEqual((chart["anchor"], chart["offset"]), ({"row": 2, "col": 3}, {"x": 19, "y": 0}))

    def test_units(self):
        for unit, value in (("cm", "2.54cm"), ("mm", "25.4mm"), ("in", "1in"), ("pt", "72pt"), ("px", "96px"),
                            ("pc", "6pc")):
            with self.subTest(unit):
                chart = one(ods_chart_xml(series=SERIES), cell=(1, 1), x=value, y="0cm", width=value, height=value)
                self.assertEqual((chart["offset"]["x"], chart["size"]), (96, {"width": 96, "height": 96}))

    def test_absolute_position_uses_the_cell_sizes_asked_for(self):
        chart = one(ods_chart_xml(series=SERIES), x="2.54cm", y="1.27cm")
        self.assertEqual(chart["anchor"], {"row": 3, "col": 2})
        data = ods_with_charts({SHEET: ""}, [{"sheet": SHEET, "xml": ods_chart_xml(series=SERIES), "x": "2.54cm", "y": "1.27cm"}])
        drafts, _ = read(data, widths=(lambda col: 100, lambda row: 30))
        self.assertEqual((drafts[0]["anchor"], drafts[0]["offset"]), ({"row": 2, "col": 1}, {"x": 96, "y": 18}))

    def test_unreadable_lengths(self):
        for label, extra in (("percent", {"width": "50%"}), ("words", {"x": "left"}), ("malformed", {"y": "1.2.3cm"}),
                             ("missing unit", {"height": "96"})):
            with self.subTest(label):
                drafts, report = read(ods_with_charts({SHEET: ""}, [{"sheet": SHEET, "xml": ods_chart_xml(series=SERIES), **extra}]))
                self.assertEqual(drafts, [None])
                self.assertEqual([item[0] for item in skipped(report)], ["unreadable"])


class BoundTests(unittest.TestCase):
    def test_measurement_steps_are_a_total(self):
        calls = []
        # 5,000,000 px down is far past MAX_STEPS rows of 1 px: the rest is measured with the default 20 px rows.
        data = ods_with_charts({SHEET: ""}, [{"sheet": SHEET, "xml": ods_chart_xml(series=SERIES), "x": "10px", "y": "5000000px"}])
        drafts, _ = read(data, widths=(lambda col: calls.append(col) or 64, lambda row: calls.append(row) or 1))
        self.assertEqual(len(calls), ods_charts.MAX_STEPS)
        self.assertTrue(drafts[0]["approximate_size"])
        self.assertTrue(drafts[0]["absolute_anchor"])
        walked = ods_charts.MAX_STEPS - 1  # the first step went to the column
        self.assertEqual(drafts[0]["anchor"], {"row": 1 + walked + (5_000_000 - walked) // 20, "col": 1})

    def test_steps_do_not_carry_between_calls(self):
        calls = []
        y = "5000000px"
        data = ods_with_charts({SHEET: ""}, [{"sheet": SHEET, "xml": ods_chart_xml(series=SERIES), "x": "0px", "y": y}] * 2)
        drafts, _ = read(data, widths=(lambda col: 64, lambda row: calls.append(row) or 1))
        self.assertEqual(len(calls), 2 * (ods_charts.MAX_STEPS - 1))  # one step of each frame went to a column
        self.assertEqual([item["approximate_size"] for item in drafts], [True, True])


class ObjectTests(unittest.TestCase):
    def test_unreadable_object(self):
        good = ods_chart_xml(series=SERIES)
        cases = (("no content.xml", {"xml": good, "missing": True}),
                 ("truncated", {"xml": "<office:document-content"}),
                 ("outside the package", {"xml": good, "target": "../other"}),
                 ("absolute", {"xml": good, "target": "/Object 1"}),
                 ("backslash", {"xml": good, "target": ".\\Object 1"}),
                 ("dot dot inside", {"xml": good, "target": "./Object 2/../Object 1"}),
                 ("fragment", {"xml": good, "target": "#Object 1"}),
                 ("not a document", {"xml": "<root/>"}),
                 ("no href", {"xml": good, "target": ""}))
        for label, extra in cases:
            with self.subTest(label):
                drafts, report = read(ods_with_charts({SHEET: ""}, [{"sheet": SHEET, "name": "Broken", **extra}]))
                self.assertEqual(drafts, [None])
                entries = [entry for entry in report.entries() if entry["category"] == "chart.skipped"]
                self.assertEqual([(item["detail"], item["severity"], item["count"]) for item in entries],
                                 [("unreadable", "omitted", 1)])
                self.assertEqual(entries[0]["samples"], [{"sheet": SHEET, "cell": "Broken"}])
                self.assertEqual(len(report.entries()), 1)

    def test_part_that_cannot_be_read_is_contained(self):
        data = ods_with_charts({SHEET: ""}, [{"sheet": SHEET, "name": "Big", "xml": ods_chart_xml(series=SERIES)}])
        archive = zipfile.ZipFile(io.BytesIO(data))
        (_, frame, cell), = frames(archive)
        for reader in (lambda a, n: None, lambda a, n: 1 / 0, lambda a, n: (_ for _ in ()).throw(RecursionError)):
            report = ConversionReport()
            self.assertIsNone(read_frame_draft(archive, frame, 0, SHEET, 0, reader, report, cell))
            self.assertEqual([item[0] for item in skipped(report)], ["unreadable"])

    def test_non_chart_object(self):
        formula = ('<office:document-content xmlns:office="%s"><office:body><office:formula/></office:body>'
                   '</office:document-content>' % O)
        math = '<math xmlns="http://www.w3.org/1998/Math/MathML"><mi>x</mi></math>'
        for label, xml in (("formula", formula), ("mathml", math)):
            with self.subTest(label):
                drafts, report = read(ods_with_charts({SHEET: ""}, [{"sheet": SHEET, "xml": xml}]))
                self.assertEqual(drafts, [None])
                self.assertEqual(report.entries(), [])
        archive = zipfile.ZipFile(io.BytesIO(ods_with_charts({SHEET: ""}, [{"sheet": SHEET, "xml": formula}])))
        image = read_xml(archive, "content.xml")
        frame = next(image.iter(FRAME))
        for child in list(frame):
            frame.remove(child)
        report = ConversionReport()
        self.assertIsNone(read_frame_draft(archive, frame, 0, SHEET, 0, read_part, report, None))
        self.assertEqual(report.entries(), [])

    def test_good_chart_survives_a_bad_one(self):
        charts = [{"sheet": SHEET, "name": "Bad", "xml": "<office:document-content"},
                  {"sheet": SHEET, "name": "Good", "xml": ods_chart_xml(series=SERIES)}]
        drafts, report = read(ods_with_charts({SHEET: ""}, charts))
        self.assertEqual([item and item["name"] for item in drafts], [None, "Good"])
        self.assertEqual([item[0] for item in skipped(report)], ["unreadable"])

    def test_default_name(self):
        data = ods_with_charts({SHEET: ""}, [{"sheet": SHEET, "xml": ods_chart_xml(series=SERIES), "name": ""}])
        drafts, _ = read(data, order=4)
        self.assertEqual((drafts[0]["name"], drafts[0]["order"]), ("Chart 5", 4))


if __name__ == "__main__":
    unittest.main()
