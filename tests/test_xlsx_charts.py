# SPDX-License-Identifier: GPL-3.0-only
import io
import unittest
import xml.etree.ElementTree as ET
import zipfile

from chart_fixtures import (absolute, axes_xml, cell_title, chart_xml, one_cell, plot_xml, rich_title, sample_like,
                            two_cell, xlsx_with_charts)
from datahorizon.conversion_report import ConversionReport
from datahorizon.import_charts import MAX_DRAFT_SERIES, MAX_FRAMES, UNUSABLE, convert
from datahorizon.xlsx_charts import read_sheet_drafts
from datahorizon.xlsx_stream import ByteBudget

SHEET = "Data"
SERIES = [{"name_ref": "Data!$B$1", "cat": "Data!$A$2:$A$8", "val": "Data!$B$2:$B$8"}]
BIG = 64*1024*1024


def read(data, sheet=1, name=None, budget=None):
    """Drafts and the report of the ``sheet``-th (1-based) worksheet of a package."""
    archive = zipfile.ZipFile(io.BytesIO(data))
    report = ConversionReport()
    part = f"xl/worksheets/sheet{sheet}.xml"
    drafts = read_sheet_drafts(archive, part, sheet - 1, name or SHEET, budget or ByteBudget(BIG), report, drawing_id(archive, part))
    return drafts, report


def drawing_id(archive, part):
    """The ``r:id`` of a worksheet's ``<drawing>``, as the sheet readers' features find it, or ``None``."""
    found = next((item for item in ET.fromstring(archive.read(part)).iter() if item.tag.endswith("}drawing")), None)
    return None if found is None else next(value for key, value in found.attrib.items() if key.endswith("}id"))


def one(xml, anchor=None, **options):
    """The single draft of a one-chart package."""
    drafts, report = read(xlsx_with_charts({SHEET: ""}, [{"sheet": SHEET, "xml": xml, "anchor": anchor}], **options))
    assert len(drafts) == 1, report.entries()
    return drafts[0]


def skipped(report):
    return [(entry["detail"], entry["samples"]) for entry in report.entries() if entry["category"] == "chart.skipped"]


class SampleTests(unittest.TestCase):
    def test_sample_like(self):
        data = sample_like()
        first, report = read(data, 1, "Column Chart")
        second, _ = read(data, 2, "Stacked Column")
        self.assertEqual((len(first), len(second)), (1, 1))
        self.assertEqual(report.entries(), [])
        chart = first[0]
        self.assertEqual(chart["plots"], [{"kind": "bar", "direction": "col", "grouping": "clustered",
                                           "three_d": False, "secondary": False}])
        self.assertEqual(chart["title"], {"text": "Sales by Division", "from_cell": False})
        self.assertEqual(len(chart["series"]), 1)
        series = chart["series"][0]
        self.assertEqual(series["name"], {"ref": ("Column Chart", 1, 2, 1, 2)})
        self.assertEqual(series["values"], ("Column Chart", 2, 2, 8, 2))
        self.assertEqual(series["categories"], ("Column Chart", 2, 1, 8, 1))
        self.assertEqual(chart["anchor"], {"row": 1, "col": 3})
        self.assertEqual(chart["offset"], {"x": 66, "y": 16})
        self.assertEqual((chart["sheet"], chart["name"], chart["order"]), (0, "Chart 1", 0))
        for item in ("colours", "data labels", "gap width"):
            self.assertIn(item, chart["styled"])
        self.assertEqual(chart["legend"], "right")
        self.assertEqual(second[0]["plots"][0]["grouping"], "stacked")
        self.assertEqual(second[0]["sheet"], 1)
        self.assertEqual(len(second[0]["series"]), 4)

    def test_strict_namespaces(self):
        data = xlsx_with_charts({SHEET: ""}, [{"sheet": SHEET, "xml": chart_xml(series=SERIES)}], strict=True)
        drafts, report = read(data)
        self.assertEqual(len(drafts), 1)
        self.assertEqual(report.entries(), [])


class AnchorTests(unittest.TestCase):
    def test_size_from_two_cell_anchor(self):
        anchor = two_cell((2, 0, 0, 0), (9, 0, 15, 0))
        data = xlsx_with_charts({SHEET: ""}, [{"sheet": SHEET, "xml": chart_xml(series=SERIES), "anchor": anchor}])
        drafts, _ = read(data)
        self.assertEqual(drafts[0]["size"], {"width": 448, "height": 300})
        self.assertFalse(drafts[0]["approximate_size"])
        self.assertFalse(drafts[0]["absolute_anchor"])

    def test_one_cell_and_absolute_anchor(self):
        chart = one(chart_xml(series=SERIES), one_cell((2, 0, 0, 0), (4572000, 2743200)))
        self.assertEqual(chart["size"], {"width": 480, "height": 288})
        self.assertEqual(chart["anchor"], {"row": 1, "col": 3})
        self.assertFalse(chart["absolute_anchor"])
        chart = one(chart_xml(series=SERIES), absolute((1219200, 381000), (4572000, 2743200)))
        self.assertEqual((chart["anchor"], chart["offset"]), ({"row": 3, "col": 3}, {"x": 0, "y": 0}))
        self.assertEqual(chart["size"], {"width": 480, "height": 288})
        self.assertTrue(chart["absolute_anchor"])


class ChartPartTests(unittest.TestCase):
    def test_minimal_chart(self):
        series = [{"val": "Data!$B$2:$B$8"}]
        chart = one(chart_xml(series=series))
        self.assertEqual(chart["title"], {"text": None, "from_cell": False})
        self.assertIsNone(chart["series"][0]["categories"])
        self.assertIsNone(chart["series"][0]["name"])
        self.assertIsNone(chart["legend"])
        self.assertEqual(chart["styled"], [])
        self.assertEqual(chart["axis_titles"]["value"], {"text": None, "from_cell": False})

    def test_kinds(self):
        def plots(**options):
            return one(chart_xml(series=SERIES, **options))["plots"]
        self.assertEqual(plots(kind="bar", direction="bar")[0]["direction"], "bar")
        self.assertEqual(plots(kind="bar")[0]["direction"], "col")
        for kind, grouping in (("line", "standard"), ("area", "standard"), ("pie", None), ("doughnut", None)):
            plot = plots(kind=kind, grouping=grouping)[0]
            self.assertEqual((plot["kind"], plot["direction"], plot["three_d"]), (kind, None, False))
        self.assertEqual(plots(kind="line", grouping="stacked")[0]["grouping"], "stacked")
        self.assertEqual(plots(kind="radar", grouping=None)[0]["kind"], "radar")
        self.assertTrue(plots(kind="bar3D")[0]["three_d"])
        self.assertEqual(plots(kind="bar3D")[0]["kind"], "bar")
        scatter = one(chart_xml("scatter", series=[{"x": "Data!$A$2:$A$8", "val": "Data!$B$2:$B$8"}]))
        self.assertEqual(scatter["plots"][0]["kind"], "scatter")
        self.assertEqual(scatter["series"][0]["x"], ("Data", 2, 1, 8, 1))
        self.assertEqual(scatter["series"][0]["values"], ("Data", 2, 2, 8, 2))

    def test_plots_and_secondary_axis(self):
        area = (plot_xml("bar", series=SERIES, axes=("1", "2")) + plot_xml("line", "col", "standard", SERIES, axes=("1", "2"))
                + axes_xml("bar"))
        chart = one(chart_xml(plot_area=area))
        self.assertEqual([item["kind"] for item in chart["plots"]], ["bar", "line"])
        self.assertEqual([item["secondary"] for item in chart["plots"]], [False, False])
        self.assertEqual(len(chart["series"]), 2)
        pair = (plot_xml("bar", series=SERIES, axes=("1", "2")) + plot_xml("line", "col", "standard", SERIES, axes=("3", "4"))
                + axes_xml("bar", ids=("1", "2")) + axes_xml("line", ids=("3", "4")))
        self.assertEqual([item["secondary"] for item in one(chart_xml(plot_area=pair))["plots"]], [False, True])
        far = (plot_xml("bar", series=SERIES, axes=("1", "2")) + plot_xml("line", "col", "standard", SERIES, axes=("1", "4"))
               + axes_xml("bar", ids=("1", "2")) + axes_xml("line", ids=("1", "4"), crosses="max"))
        self.assertEqual([item["secondary"] for item in one(chart_xml(plot_area=far))["plots"]], [False, True])

    def test_series_order(self):
        series = [{"name_text": "Later", "val": "Data!$B$2:$B$8", "order": 1},
                  {"name_text": "First", "val": "Data!$C$2:$C$8", "order": 0}]
        chart = one(chart_xml(series=series))
        self.assertEqual([item["name"] for item in chart["series"]], [{"text": "First"}, {"text": "Later"}])

    def test_title_from_cell_and_axis_titles(self):
        chart = one(chart_xml(series=SERIES, title=cell_title("Data!$D$1", "From a cell")))
        self.assertEqual(chart["title"], {"text": "From a cell", "from_cell": True})
        value = '<c:scaling><c:min val="-2.5"/><c:max val="100"/></c:scaling>' + rich_title("Dollars")
        chart = one(chart_xml(series=SERIES, category_axis=rich_title("Division"), value_axis=value))
        self.assertEqual(chart["axis_titles"]["category"]["text"], "Division")
        self.assertEqual(chart["axis_titles"]["value"]["text"], "Dollars")
        self.assertEqual((chart["value_min"], chart["value_max"]), (-2.5, 100.0))
        self.assertIsInstance(chart["value_max"], float)
        chart = one(chart_xml(series=SERIES, value_axis='<c:scaling><c:min val="nan"/><c:max val="x"/></c:scaling>'))
        self.assertEqual((chart["value_min"], chart["value_max"]), (None, None))

    def test_legend_positions(self):
        for position, expected in (("r", "right"), ("b", "bottom"), ("t", "top"), ("l", "left"), ("tr", "right")):
            self.assertEqual(one(chart_xml(series=SERIES, legend=position))["legend"], expected)

    def test_literal_and_multi_area_sources(self):
        literal = {"val": "<c:numLit><c:ptCount val=\"1\"/><c:pt idx=\"0\"><c:v>1</c:v></c:pt></c:numLit>",
                   "cat": "Data!$A$2:$A$8"}
        self.assertIs(one(chart_xml(series=[literal]))["series"][0]["values"], UNUSABLE)
        multi = {"val": "(Data!$B$2:$B$3,Data!$B$5:$B$6)", "cat": "Data!$A$2:$A$8"}
        self.assertIs(one(chart_xml(series=[multi]))["series"][0]["values"], UNUSABLE)

    def test_styled_detection(self):
        def styled(**options):
            return one(chart_xml(series=SERIES, **options))["styled"]
        self.assertEqual(styled(), [])
        self.assertEqual(styled(extras='<c:dLbls><c:showVal val="0"/></c:dLbls>'), [])
        self.assertEqual(styled(extras='<c:dLbls><c:showPercent val="1"/></c:dLbls>'), ["data labels"])
        self.assertEqual(styled(extras='<c:overlap val="100"/>'), ["gap width"])
        self.assertEqual(styled(kind="line", grouping="standard", extras='<c:marker val="1"/>'), [])
        marked = [{"val": "Data!$B$2:$B$8", "sppr": '<c:marker><c:symbol val="circle"/></c:marker><c:smooth val="1"/>'}]
        self.assertEqual(one(chart_xml("line", series=marked))["styled"], ["markers", "smoothing"])
        plain = [{"val": "Data!$B$2:$B$8", "sppr": '<c:marker><c:symbol val="none"/></c:marker><c:smooth val="0"/>'}]
        self.assertEqual(one(chart_xml("line", series=plain))["styled"], [])
        self.assertEqual(styled(value_axis='<c:majorGridlines/><c:numFmt formatCode="0%" sourceLinked="0"/><c:txPr/>'),
                         ["gridlines", "number formats", "fonts"])
        self.assertEqual(styled(value_axis='<c:numFmt formatCode="General" sourceLinked="1"/>'), [])
        outline = [{"val": "Data!$B$2:$B$8", "sppr": '<c:spPr><a:ln/></c:spPr>'}]
        self.assertEqual(one(chart_xml(series=outline))["styled"], [])


class BoundTests(unittest.TestCase):
    def test_frame_cap(self):
        charts = [{"sheet": SHEET, "name": f"C{n}", "xml": chart_xml(series=SERIES)} for n in range(300)]
        drafts, report = read(xlsx_with_charts({SHEET: ""}, charts))
        self.assertEqual((MAX_FRAMES, len(drafts)), (256, 256))
        entries = [item for item in report.entries() if item["category"] == "chart.skipped"]
        self.assertEqual([(item["detail"], item["count"], item["samples"]) for item in entries],
                         [("unreadable", 44, [{"sheet": SHEET, "cell": "drawing"}])])

    def test_wide_anchor_is_measured_with_default_sizes(self):
        anchor = two_cell((0, 0, 0, 0), (5000, 0, 0, 0))
        drafts, _ = read(xlsx_with_charts({SHEET: ""}, [{"sheet": SHEET, "xml": chart_xml(series=SERIES), "anchor": anchor}]))
        self.assertEqual((drafts[0]["size"]["width"], drafts[0]["approximate_size"]), (5000 * 64, False))

    def test_off_grid_anchor_is_skipped_by_convert(self):
        anchor = two_cell((19999, 0, 0, 0), (20005, 0, 5, 0))
        chart = one(chart_xml(series=SERIES), anchor)
        self.assertEqual(chart["anchor"]["col"], 20000)
        report = ConversionReport()
        self.assertIsNone(convert(chart, [SHEET], report))
        self.assertEqual(skipped(report), [("off_grid", [{"sheet": SHEET, "cell": "Chart 1"}])])


class SeriesCapTests(unittest.TestCase):
    def test_series_cap(self):
        many = [{"val": "Data!$B$2:$B$8"} for _ in range(5000)]
        chart = one(chart_xml(series=many))
        self.assertEqual(len(chart["series"]), MAX_DRAFT_SERIES)
        report = ConversionReport()
        self.assertIsNone(convert(chart, [SHEET], report))
        self.assertEqual([item[0] for item in skipped(report)], ["limit"])
        area = "".join(plot_xml("bar", series=many[:40], axes=("1", "2")) for _ in range(5)) + axes_xml("bar")
        chart = one(chart_xml(plot_area=area))
        self.assertEqual(len(chart["series"]), MAX_DRAFT_SERIES)


class DrawingTests(unittest.TestCase):
    def test_unreadable_chart_is_reported_not_raised(self):
        good = chart_xml(series=SERIES)
        cases = (("truncated", {"xml": "<c:chartSpace"}),
                 ("missing part", {"xml": good, "missing": True}),
                 ("outside the package", {"xml": good, "target": "../../../outside.xml"}),
                 ("absolute outside", {"xml": good, "target": "/../outside.xml"}),
                 ("not a chart", {"xml": "<root/>"}))
        for label, extra in cases:
            with self.subTest(label):
                drafts, report = read(xlsx_with_charts({SHEET: ""}, [{"sheet": SHEET, "name": "Broken", **extra}]))
                self.assertEqual(drafts, [])
                entries = [entry for entry in report.entries() if entry["category"] == "chart.skipped"]
                self.assertEqual([(item["detail"], item["severity"], item["count"]) for item in entries],
                                 [("unreadable", "omitted", 1)])
                self.assertEqual(entries[0]["samples"], [{"sheet": SHEET, "cell": "Broken"}])

    def test_missing_drawing_part_is_reported(self):
        data = xlsx_with_charts({SHEET: ""}, [{"sheet": SHEET, "xml": chart_xml(series=SERIES)}])
        source = zipfile.ZipFile(io.BytesIO(data))
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as target:
            for item in source.infolist():
                if item.filename != "xl/drawings/drawing1.xml":
                    target.writestr(item, source.read(item.filename))
        drafts, report = read(output.getvalue())
        self.assertEqual(drafts, [])
        self.assertEqual([item[0] for item in skipped(report)], ["unreadable"])

    def test_good_chart_survives_a_bad_one(self):
        charts = [{"sheet": SHEET, "name": "Bad", "xml": "<c:chartSpace"},
                  {"sheet": SHEET, "name": "Good", "xml": chart_xml(series=SERIES)}]
        drafts, report = read(xlsx_with_charts({SHEET: ""}, charts))
        self.assertEqual([(item["name"], item["order"]) for item in drafts], [("Good", 1)])
        self.assertEqual([item[0] for item in skipped(report)], ["unreadable"])

    def test_chart_part_over_budget(self):
        xml = chart_xml(series=SERIES)
        data = xlsx_with_charts({SHEET: ""}, [{"sheet": SHEET, "xml": xml}])
        archive = zipfile.ZipFile(io.BytesIO(data))
        small = ByteBudget(sum(archive.getinfo(name).file_size for name in archive.namelist() if "chart" not in name
                               or name.endswith(".rels")) + 8)
        drafts, report = read(data, budget=small)
        self.assertEqual(drafts, [])
        self.assertEqual([item[0] for item in skipped(report)], ["unreadable"])
        # Only what was read is charged: the unread chart part is not.
        self.assertLessEqual(small.used, small.limit)
        used = small.used
        drafts, _ = read(data, budget=ByteBudget(used + len(xml)))
        self.assertEqual(len(drafts), 1)

    def test_group_shape_and_extended_chart(self):
        grouped = ('<xdr:twoCellAnchor><xdr:from><xdr:col>0</xdr:col><xdr:colOff>0</xdr:colOff><xdr:row>0</xdr:row>'
                   '<xdr:rowOff>0</xdr:rowOff></xdr:from><xdr:to><xdr:col>5</xdr:col><xdr:colOff>0</xdr:colOff>'
                   '<xdr:row>5</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:to><xdr:grpSp><xdr:nvGrpSpPr>'
                   '<xdr:cNvPr id="9" name="Group"/></xdr:nvGrpSpPr>{frame}</xdr:grpSp><xdr:clientData/></xdr:twoCellAnchor>')
        data = xlsx_with_charts({SHEET: ""}, [{"sheet": SHEET, "name": "Inside", "xml": chart_xml(series=SERIES),
                                               "anchor": grouped},
                                              {"sheet": SHEET, "name": "Funnel", "xml": "<cx:chartSpace/>", "extended": True}])
        drafts, report = read(data)
        self.assertEqual(skipped(report), [("grouped", [{"sheet": SHEET, "cell": "Inside"}])])
        self.assertEqual([(item["name"], item["plots"][0]["kind"], item["series"]) for item in drafts],
                         [("Funnel", "extended", [])])
        self.assertEqual(drafts[0]["order"], 1)

    def test_mc_alternate_content(self):
        chart = chart_xml(series=SERIES)
        # The Choice holds a regular chart.
        anchor = two_cell(frame='<mc:AlternateContent><mc:Choice Requires="c14">{frame}</mc:Choice>'
                                '<mc:Fallback><xdr:sp/></mc:Fallback></mc:AlternateContent>')
        self.assertEqual(one(chart, anchor)["plots"][0]["kind"], "bar")
        # The Choice holds an extended chart, so the Fallback chart is read.
        extended = '<xdr:graphicFrame><a:graphic><a:graphicData uri="x"><cx:chart xmlns:cx="http://schemas.microsoft.com/office/drawing/2014/chartex" r:id="rIdX"/></a:graphicData></a:graphic></xdr:graphicFrame>'
        anchor = two_cell(frame=f'<mc:AlternateContent><mc:Choice Requires="cx1">{extended}</mc:Choice>'
                                '<mc:Fallback>{frame}</mc:Fallback></mc:AlternateContent>')
        self.assertEqual(one(chart, anchor)["plots"][0]["kind"], "bar")
        # Only an extended chart: it is reported as such.
        anchor = two_cell(frame=f'<mc:AlternateContent><mc:Choice Requires="cx1">{extended}</mc:Choice>'
                                '<mc:Fallback><xdr:sp/></mc:Fallback></mc:AlternateContent>')
        drafts, report = read(xlsx_with_charts({SHEET: ""}, [{"sheet": SHEET, "xml": chart, "anchor": anchor}]))
        self.assertEqual([item["plots"][0]["kind"] for item in drafts], ["extended"])

    def test_sheet_without_drawing(self):
        drafts, report = read(xlsx_with_charts({SHEET: "", "Other": ""}, [{"sheet": "Other", "xml": chart_xml(series=SERIES)}]))
        self.assertEqual((drafts, report.entries()), ([], []))


if __name__ == "__main__":
    unittest.main()
