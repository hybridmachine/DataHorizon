# SPDX-License-Identifier: GPL-3.0-only
"""Tests for the XLSX chart writer: a draft written by ``chart_part`` and ``package`` reads back, through this
project's own reader and converter, as the draft it came from."""
import copy
import io
import json
import pathlib
import unittest
import xml.etree.ElementTree as ET
import zipfile

from datahorizon import export_charts
from datahorizon.conversion_report import ConversionReport
from datahorizon.import_charts import convert, parse_reference
from datahorizon.xlsx_charts import chart_part, package, read_sheet_drafts
from datahorizon.xlsx_stream import ByteBudget
from xlsx_builder import workbook_bytes

try:
    import openpyxl
except ImportError:  # pragma: no cover - the package check is skipped where openpyxl is absent
    openpyxl = None

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "charts"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG = "http://schemas.openxmlformats.org/package/2006/relationships"
TYPES = "http://schemas.openxmlformats.org/package/2006/content-types"
BIG = 64 * 1024 * 1024


def column_draft(**overrides):
    draft = {"name": "Units", "sheet": "Data", "anchor": {"row": 3, "col": 4}, "offset": {"x": 7, "y": 11},
             "size": {"width": 480, "height": 320}, "type": "column", "grouping": "clustered", "title": "Units by month",
             "category_title": "Month", "value_title": "Units", "min": 0.0, "max": 100.0, "legend": "bottom",
             "categories": ("Data", 2, 1, 13, 1),
             "series": [{"name": ("ref", ("Data", 1, 2, 1, 2)), "values": ("Data", 2, 2, 13, 2), "x": None},
                        {"name": ("text", "South"), "values": ("Data", 2, 3, 13, 3), "x": None}]}
    draft.update(overrides)
    return draft


def drafts_for_every_fixture():
    """One draft per ``tests/fixtures/charts/*.json``: series bound to columns of the sheet "Data"."""
    found = []
    for path in sorted(FIXTURES.glob("*.json")):
        spec = json.loads(path.read_text())
        rows = len(spec["series"][0]["values"]) + 1
        scatter = spec["type"] == "scatter"
        series = []
        for number, item in enumerate(spec["series"]):
            column = 2 + number
            series.append({"name": ("text", item["name"]), "values": ("Data", 2, column, rows, column),
                           "x": ("Data", 2, column + len(spec["series"]), rows, column + len(spec["series"])) if scatter else None})
        value_axis = spec["axes"]["value"]
        found.append((path.stem, {
            "name": path.stem, "sheet": "Data", "anchor": {"row": 2, "col": 5}, "offset": {"x": 6, "y": 9},
            "size": {"width": 640, "height": 400}, "type": spec["type"], "grouping": spec["grouping"],
            "title": spec["title"], "category_title": spec["axes"]["category"]["title"],
            "value_title": value_axis["title"], "min": value_axis["min"], "max": value_axis["max"],
            "legend": spec["legend"]["position"],
            "categories": None if scatter or spec["categories"] is None else ("Data", 2, 1, rows, 1),
            "series": series}))
    return found


def xml_bytes(element):
    return ET.tostring(element, encoding="utf-8", xml_declaration=True)


def write(drafts, names):
    """``(parts, rids, overrides)`` of ``package`` for drafts built by ``chart_part``."""
    built = export_charts.build(drafts, chart_part, ConversionReport())
    return package(built, names)


def packaged(drafts, names, *, content_types=False):
    """A minimal XLSX with one empty sheet per name plus the parts ``package`` writes."""
    parts, rids, overrides = write(drafts, names)
    sheet_after = {names[index - 1]: f'<drawing r:id="{rid}"/>' for index, rid in rids.items()}
    extra = {path: xml_bytes(element) for path, element in parts}
    if content_types:
        types = ET.Element("Types", xmlns=TYPES)
        ET.SubElement(types, "Default", Extension="rels", ContentType="application/vnd.openxmlformats-package.relationships+xml")
        ET.SubElement(types, "Default", Extension="xml", ContentType="application/xml")
        ET.SubElement(types, "Override", PartName="/xl/workbook.xml",
                      ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml")
        for number in range(1, len(names) + 1):
            ET.SubElement(types, "Override", PartName=f"/xl/worksheets/sheet{number}.xml",
                          ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml")
        for part, kind in overrides:
            ET.SubElement(types, "Override", PartName="/" + part, ContentType=kind)
        root = ET.Element("Relationships", xmlns=PKG)
        ET.SubElement(root, "Relationship", Id="rId1", Type=REL + "/officeDocument", Target="xl/workbook.xml")
        extra["[Content_Types].xml"] = xml_bytes(types)
        extra["_rels/.rels"] = xml_bytes(root)
    return workbook_bytes({name: "" for name in names}, extra_parts=extra, sheet_after=sheet_after)


def drawing_id(archive, part):
    found = next((item for item in ET.fromstring(archive.read(part)).iter() if item.tag.endswith("}drawing")), None)
    return None if found is None else next(value for key, value in found.attrib.items() if key.endswith("}id"))


def read_back(data, names=("Data",)):
    """``(records, report)``: the reader and ``convert`` applied to every sheet of the package."""
    archive = zipfile.ZipFile(io.BytesIO(data))
    report, records = ConversionReport(), []
    for position, name in enumerate(names):
        part = f"xl/worksheets/sheet{position + 1}.xml"
        for draft in read_sheet_drafts(archive, part, position, name, ByteBudget(BIG), report, drawing_id(archive, part)):
            records.append(convert(draft, list(names), report))
    return records, report


def report_of(report):
    return [(entry["category"], entry["detail"]) for entry in report.entries()]


def ref_of(source, names):
    sheet = names[source["sheet"]].replace("'", "''")
    return parse_reference(f"'{sheet}'!{source['ref']}", "xlsx")


def as_draft(record, names=("Data",)):
    """The export draft equal to an imported record."""
    spec = record["spec"]

    def ref(part):
        return None if part is None else ref_of(part["source"], names)

    def label(part):
        return ("text", part["text"]) if "text" in part else ("ref", ref(part))

    return {"name": record["name"], "sheet": names[record["sheet"]], "anchor": dict(record["anchor"]),
            "offset": dict(record["offset"]), "size": dict(record["size"]), "type": spec["type"],
            "grouping": spec["grouping"], "title": spec["title"]["text"],
            "category_title": spec["axes"]["category"]["title"], "value_title": spec["axes"]["value"]["title"],
            "min": spec["axes"]["value"]["min"], "max": spec["axes"]["value"]["max"], "legend": spec["legend"]["position"],
            "categories": ref(spec["categories"]),
            "series": [{"name": label(item["name"]), "values": ref(item["values"]), "x": ref(item["x"])}
                       for item in spec["series"]]}


def local(tag):
    return tag.rsplit("}", 1)[-1].rsplit(":", 1)[-1]


def find(element, name):
    return [item for item in element.iter() if local(item.tag) == name]


class RoundTrip(unittest.TestCase):
    def test_each_type_reads_back(self):
        drafts = drafts_for_every_fixture()
        self.assertEqual({draft["type"] for _, draft in drafts}, {"column", "bar", "line", "area", "pie", "scatter"})
        for label, draft in drafts:
            with self.subTest(label):
                records, report = read_back(packaged([draft], ["Data"]))
                self.assertEqual(report_of(report), [])
                self.assertEqual(len(records), 1)
                self.assertEqual(as_draft(records[0]), draft)

    def test_every_option_reads_back(self):
        cases = [column_draft(legend="right"), column_draft(legend="none"), column_draft(title=None),
                 column_draft(category_title=None, value_title=None), column_draft(min=None, max=None),
                 column_draft(min=-2.5, max=None), column_draft(min=None, max=1e6), column_draft(grouping="stacked"),
                 column_draft(type="bar"), column_draft(type="area", grouping="stacked"),
                 column_draft(type="line", grouping=None), column_draft(categories=None),
                 column_draft(title="two\nlines"), column_draft(offset={"x": 0, "y": 0}, anchor={"row": 1, "col": 1})]
        for number, draft in enumerate(cases):
            with self.subTest(number):
                records, report = read_back(packaged([draft], ["Data"]))
                self.assertEqual(report_of(report), [])
                self.assertEqual(as_draft(records[0]), draft)

    def test_references_with_quoted_sheet_names_read_back(self):
        names = ["Q1 Sales", "it's", "A1"]
        ref = lambda name, first, last: (name, first, 2, last, 2)
        draft = column_draft(sheet=names[0], categories=None,
                             series=[{"name": ("ref", ref("it's", 1, 1)), "values": ref("A1", 2, 9), "x": None},
                                     {"name": ("text", "plain"), "values": ref("Q1 Sales", 2, 9), "x": None}])
        records, report = read_back(packaged([draft], names), names)
        self.assertEqual(report_of(report), [])
        self.assertEqual(as_draft(records[0], names), draft)

    def test_unnamed_series_reads_back_as_numbered(self):
        draft = column_draft(series=[{"name": None, "values": ("Data", 2, 2, 5, 2), "x": None},
                                     {"name": None, "values": ("Data", 2, 3, 5, 3), "x": None}])
        records, report = read_back(packaged([draft], ["Data"]))
        self.assertEqual(report_of(report), [])
        self.assertEqual([item["name"] for item in records[0]["spec"]["series"]],
                         [{"text": "Series 1"}, {"text": "Series 2"}])

    @unittest.skipIf(openpyxl is None, "openpyxl is not installed")
    def test_openpyxl_opens_the_package(self):
        book = openpyxl.load_workbook(io.BytesIO(packaged([column_draft()], ["Data"], content_types=True)))
        self.assertEqual(len(book["Data"]._charts), 1)

    def test_two_charts_keep_their_order(self):
        drafts = [column_draft(name=name) for name in ("Third", "First", "Second")]
        records, report = read_back(packaged(drafts, ["Data"]))
        self.assertEqual(report_of(report), [])
        self.assertEqual([item["name"] for item in records], ["Third", "First", "Second"])

    def test_charts_on_two_sheets(self):
        names = ["Data", "Other"]
        drafts = [column_draft(name="A"), column_draft(name="B", sheet="Other", categories=("Other", 2, 1, 13, 1),
                  series=[{"name": ("text", "s"), "values": ("Other", 2, 2, 13, 2), "x": None}]),
                  column_draft(name="C")]
        data = packaged(drafts, names)
        records, report = read_back(data, names)
        self.assertEqual(report_of(report), [])
        self.assertEqual([(item["name"], names[item["sheet"]]) for item in records],
                         [("A", "Data"), ("C", "Data"), ("B", "Other")])
        for item, draft in zip(records, [drafts[0], drafts[2], drafts[1]]):
            self.assertEqual(as_draft(item, names), draft)
        archive = zipfile.ZipFile(io.BytesIO(data))
        for sheet in (1, 2):
            rels = ET.fromstring(archive.read(f"xl/worksheets/_rels/sheet{sheet}.xml.rels"))
            self.assertEqual([item.get("Target") for item in rels], [f"../drawings/drawing{sheet}.xml"])

    def test_sheets_without_charts_get_no_drawing(self):
        names = ["Empty", "Data", "Last"]
        parts, rids, _ = write([column_draft()], names)
        self.assertEqual(list(rids), [2])
        self.assertEqual([path for path, _ in parts if "sheet" in path], ["xl/worksheets/_rels/sheet2.xml.rels"])

    def test_nothing_built_writes_nothing(self):
        self.assertEqual(write([], ["Data"]), ([], {}, []))


class Structure(unittest.TestCase):
    def parts(self, drafts, names=("Data",)):
        parts, rids, overrides = write(drafts, list(names))
        return dict(parts), rids, overrides

    def chart(self, **overrides):
        return chart_part(column_draft(**overrides))

    def test_part_names_rids_and_content_types(self):
        parts, rids, overrides = self.parts([column_draft(name="A"), column_draft(name="B")])
        self.assertEqual(sorted(parts), ["xl/charts/chart1.xml", "xl/charts/chart2.xml", "xl/drawings/_rels/drawing1.xml.rels",
                                         "xl/drawings/drawing1.xml", "xl/worksheets/_rels/sheet1.xml.rels"])
        self.assertEqual(rids, {1: "rId1"})
        self.assertEqual(sorted(overrides), [
            ("xl/charts/chart1.xml", "application/vnd.openxmlformats-officedocument.drawingml.chart+xml"),
            ("xl/charts/chart2.xml", "application/vnd.openxmlformats-officedocument.drawingml.chart+xml"),
            ("xl/drawings/drawing1.xml", "application/vnd.openxmlformats-officedocument.drawing+xml")])
        rels = {item.get("Id"): (item.get("Type"), item.get("Target")) for item in parts["xl/drawings/_rels/drawing1.xml.rels"]}
        self.assertEqual(rels, {"rId1": (REL + "/chart", "../charts/chart1.xml"), "rId2": (REL + "/chart", "../charts/chart2.xml")})
        sheet = list(parts["xl/worksheets/_rels/sheet1.xml.rels"])
        self.assertEqual([(item.get("Id"), item.get("Type"), item.get("Target")) for item in sheet],
                         [("rId1", REL + "/drawing", "../drawings/drawing1.xml")])

    def test_one_cell_anchor_in_emu(self):
        parts, _, _ = self.parts([column_draft()])
        anchors = list(parts["xl/drawings/drawing1.xml"])
        self.assertEqual([local(item.tag) for item in anchors], ["oneCellAnchor"])
        text = lambda name: find(anchors[0], name)[0].text
        self.assertEqual((text("col"), text("colOff"), text("row"), text("rowOff")), ("3", str(7 * 9525), "2", str(11 * 9525)))
        extent = next(item for item in anchors[0] if local(item.tag) == "ext")
        self.assertEqual((extent.get("cx"), extent.get("cy")), (str(480 * 9525), str(320 * 9525)))
        self.assertEqual(find(anchors[0], "cNvPr")[0].get("name"), "Units")

    def test_frames_follow_draft_order(self):
        parts, _, _ = self.parts([column_draft(name=name) for name in ("c", "a", "b")])
        self.assertEqual([item.get("name") for item in find(parts["xl/drawings/drawing1.xml"], "cNvPr")], ["c", "a", "b"])
        ids = [item.get("id") for item in find(parts["xl/drawings/drawing1.xml"], "cNvPr")]
        self.assertEqual(len(set(ids)), 3)

    def test_absent_title_deletes_the_automatic_title(self):
        absent, present = self.chart(title=None), self.chart()
        self.assertEqual([item for item in find(absent, "chart")[0] if local(item.tag) == "title"], [])
        self.assertEqual(find(absent, "autoTitleDeleted")[0].get("val"), "1")
        self.assertEqual([local(item.tag) for item in find(present, "chart")[0] if local(item.tag) == "title"], ["title"])
        self.assertEqual(find(present, "autoTitleDeleted")[0].get("val"), "0")

    def test_square_corners_come_first_and_read_back_unreported(self):
        # Excel draws a chart with rounded corners when c:roundedCorners is absent.
        for _, draft in drafts_for_every_fixture():
            with self.subTest(draft["type"]):
                chart = chart_part(draft)
                self.assertEqual((chart[0].tag, chart[0].attrib), ("c:roundedCorners", {"val": "0"}))
                self.assertEqual(len(find(chart, "roundedCorners")), 1)
                records, report = read_back(packaged([draft], ["Data"]))
                self.assertEqual(report_of(report), [])
                self.assertEqual(as_draft(records[0]), draft)

    def test_legend_none_has_no_legend_element(self):
        self.assertEqual(find(self.chart(legend="none"), "legend"), [])
        self.assertEqual([item.get("val") for item in find(self.chart(legend="right"), "legendPos")], ["r"])
        self.assertEqual([item.get("val") for item in find(self.chart(legend="bottom"), "legendPos")], ["b"])

    def test_stacked_bars_overlap_fully_and_read_back_unreported(self):
        for kind in ("column", "bar"):
            for grouping, overlap in (("stacked", ["100"]), ("clustered", [])):
                with self.subTest(kind, grouping=grouping):
                    draft = column_draft(type=kind, grouping=grouping)
                    chart = chart_part(draft)
                    self.assertEqual([item.get("val") for item in find(chart, "overlap")], overlap)
                    records, report = read_back(packaged([draft], ["Data"]))
                    self.assertEqual(report_of(report), [])
                    self.assertEqual(as_draft(records[0]), draft)
        for kind in ("area", "line"):
            self.assertEqual(find(chart_part(column_draft(type=kind, grouping="stacked" if kind == "area" else None)), "overlap"), [])

    def test_limits_go_on_the_value_axis_scaling(self):
        chart = self.chart()
        axis = find(chart, "valAx")[0]
        scaling = next(item for item in axis if local(item.tag) == "scaling")
        self.assertEqual({local(item.tag): item.get("val") for item in scaling}, {"orientation": "minMax", "max": "100.0", "min": "0.0"})
        self.assertEqual(find(find(chart, "catAx")[0], "min"), [])
        free = find(self.chart(min=None, max=None), "valAx")[0]
        self.assertEqual([local(item.tag) for item in find(free, "scaling")[0]], ["orientation"])

    def test_series_names_literal_or_reference(self):
        chart = self.chart()
        first, second = find(chart, "ser")
        tx = next(item for item in first if local(item.tag) == "tx")
        self.assertEqual([(local(item.tag), find(item, "f")[0].text) for item in tx], [("strRef", "'Data'!$B$1")])
        tx = next(item for item in second if local(item.tag) == "tx")
        self.assertEqual([(local(item.tag), item.text) for item in tx], [("v", "South")])
        self.assertEqual(find(find(chart, "ser")[0], "f")[1].text, "'Data'!$A$2:$A$13")

    def test_no_cached_values(self):
        for _, draft in drafts_for_every_fixture():
            parts, _, _ = self.parts([draft])
            for path, element in parts.items():
                with self.subTest(path):
                    self.assertEqual([item for item in element.iter() if local(item.tag) in ("numCache", "strCache", "numLit", "strLit")], [])
        data = packaged([column_draft()], ["Data"])
        archive = zipfile.ZipFile(io.BytesIO(data))
        for path in archive.namelist():
            self.assertNotIn(b"Cache", archive.read(path))

    def test_draft_is_not_changed(self):
        draft = column_draft()
        before = copy.deepcopy(draft)
        chart_part(draft)
        self.assertEqual(draft, before)

    def test_special_characters(self):
        draft = column_draft(name="A & B <1>", title="x\ty\x01z")
        records, report = read_back(packaged([draft], ["Data"]))
        self.assertEqual(report_of(report), [])
        self.assertEqual(records[0]["name"], "A & B <1>")
        self.assertEqual(records[0]["spec"]["title"]["text"], "x\tyz")

    def test_characters_xml_cannot_hold_are_dropped_everywhere(self):
        junk = "\x00\x08\x0b\x0c\x0e\x1f￾￿"
        draft = column_draft(name="N" + junk + "m", title="T" + junk + "t", category_title="C" + junk + "c",
                             value_title="V" + junk + "v",
                             series=[{"name": ("text", "S" + junk + "s"), "values": ("Data", 2, 2, 5, 2), "x": None}])
        data = packaged([draft], ["Data"])  # every part must parse
        records, report = read_back(data)
        self.assertEqual(report_of(report), [])
        expected = column_draft(name="Nm", title="Tt", category_title="Cc", value_title="Vv",
                                series=[{"name": ("text", "Ss"), "values": ("Data", 2, 2, 5, 2), "x": None}])
        self.assertEqual(as_draft(records[0]), expected)

    def test_carriage_returns_read_back_as_line_breaks(self):
        records, _ = read_back(packaged([column_draft(title="a\r\nb\rc")], ["Data"]))
        self.assertEqual(records[0]["spec"]["title"]["text"], "a\nb\nc")


if __name__ == "__main__":
    unittest.main()
