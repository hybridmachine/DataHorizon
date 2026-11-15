# SPDX-License-Identifier: GPL-3.0-only
"""Tests for the ODS chart writer: a draft written by ``chart_document`` and ``package`` reads back, through this
project's own reader and converter, as the draft it came from."""
import copy
import io
import unittest
import xml.etree.ElementTree as ET
import zipfile

from datahorizon import export_charts
from datahorizon.conversion_report import ConversionReport
from datahorizon.interchange import read_xml
from datahorizon.ods import T, q
from datahorizon.import_charts import ChartParts, convert
from datahorizon.ods_charts import SheetFrames, chart_document, package, read_frame_draft, table_frames
from chart_fixtures import ODS_NAMESPACES
from ods_builder import manifest_document, workbook_bytes
from test_xlsx_chart_export import as_draft, column_draft, drafts_for_every_fixture, find, local, report_of

try:
    import odf.opendocument as odf_opendocument
except ImportError:  # pragma: no cover - the package check is skipped where odfpy is absent
    odf_opendocument = None

MEDIA = "application/vnd.oasis.opendocument.chart"


def xml_text(element):
    return ET.tostring(element, encoding="unicode")


def write(drafts, report=None):
    """``(parts, manifest, frames)`` of ``package`` for drafts built by ``chart_document``."""
    built = export_charts.build(drafts, chart_document, ConversionReport())
    return package(built, ConversionReport() if report is None else report)


def object_xml(element):
    return ET.tostring(element, encoding="utf-8", xml_declaration=True)


def packaged(drafts, names, adjust=None):
    """A minimal ODS with one sheet per name, each frame placed in the cell ``package`` says, plus the object parts;
    ``adjust(frame)``, when given, is applied to each frame first."""
    parts, manifest, frames = write(drafts)
    for found in frames.values():
        for _, _, frame in found:
            if adjust is not None:
                adjust(frame)
    tables = []
    for name in names:
        rows, current = "", 0
        cells = {}
        for row, col, frame in frames.get(name, []):
            cells.setdefault((row, col), []).append(xml_text(frame))
        for (row, col), found in sorted(cells.items()):
            before = f'<table:table-cell table:number-columns-repeated="{col - 1}"/>' if col > 1 else ""
            skip = (f'<table:table-row table:number-rows-repeated="{row - current - 1}"><table:table-cell/></table:table-row>'
                    if row - current > 1 else "")
            rows += f"{skip}<table:table-row>{before}<table:table-cell>{''.join(found)}</table:table-cell></table:table-row>"
            current = row
        tables.append(f'<table:table table:name="{name}">{rows}</table:table>')
    content = (f'<office:document-content {ODS_NAMESPACES}><office:automatic-styles/><office:body><office:spreadsheet>'
               f'{"".join(tables)}</office:spreadsheet></office:body></office:document-content>')
    entries = "".join(f'<manifest:file-entry manifest:full-path="{path}" manifest:media-type="{kind}"/>' for path, kind in manifest)
    return workbook_bytes({}, content=content, manifest=manifest_document(entries),
                          extra_parts={path: object_xml(element) for path, element in parts})


def read_part(archive, name):
    return read_xml(archive, name)


def read_back(data, names=("Data",)):
    """``(records, report)``: the reader and ``convert`` applied to every frame of every sheet of the package."""
    archive = zipfile.ZipFile(io.BytesIO(data))
    report, records = ConversionReport(), []
    root = read_xml(archive, "content.xml")
    for position, table in enumerate(root.iter(q(T, "table"))):
        name = table.get(q(T, "name"))
        for order, (frame, cell, _) in enumerate(table_frames(table)):
            draft = read_frame_draft(archive, frame, position, name, order, read_part, report, cell)
            if draft is not None:
                records.append(convert(draft, list(names), report))
    return records, report


def named(draft):
    """``draft`` with its text series names as header-cell references: ODS has no literal series name."""
    result = copy.deepcopy(draft)
    for number, item in enumerate(result["series"]):
        if item["name"] is None or item["name"][0] == "text":
            column = item["values"][2]
            item["name"] = ("ref", (item["values"][0], 1, column, 1, column))
    return result


class RoundTrip(unittest.TestCase):
    def test_each_type_reads_back(self):
        drafts = drafts_for_every_fixture()
        self.assertEqual({draft["type"] for _, draft in drafts}, {"column", "bar", "line", "area", "pie", "scatter"})
        for label, draft in drafts:
            draft = named(draft)
            with self.subTest(label):
                records, report = read_back(packaged([draft], ["Data"]))
                self.assertEqual(report_of(report), [])
                self.assertEqual(len(records), 1)
                self.assertEqual(as_draft(records[0]), draft)

    def test_every_option_reads_back(self):
        cases = [column_draft(legend="right"), column_draft(legend="none"), column_draft(title=None),
                 column_draft(category_title=None, value_title=None), column_draft(min=None, max=None),
                 column_draft(min=-2.5, max=None), column_draft(min=None, max=1e6), column_draft(grouping="stacked"),
                 column_draft(type="bar"), column_draft(type="bar", grouping="stacked"),
                 column_draft(type="area", grouping="stacked"), column_draft(type="line", grouping=None),
                 column_draft(categories=None), column_draft(title="two\nlines"),
                 column_draft(offset={"x": 0, "y": 0}, anchor={"row": 1, "col": 1})]
        for number, draft in enumerate(cases):
            draft = named(draft)
            with self.subTest(number):
                records, report = read_back(packaged([draft], ["Data"]))
                self.assertEqual(report_of(report), [])
                self.assertEqual(as_draft(records[0]), draft)

    def test_references_with_quoted_sheet_names_read_back(self):
        names = ["Q1 Sales", "it's", "A1"]
        ref = lambda name, first, last: (name, first, 2, last, 2)
        draft = column_draft(sheet=names[0], categories=None,
                             series=[{"name": ("ref", ref("it's", 1, 1)), "values": ref("A1", 2, 9), "x": None},
                                     {"name": ("ref", ref("Q1 Sales", 1, 1)), "values": ref("Q1 Sales", 2, 9), "x": None}])
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

    def test_text_series_name_reads_back_as_numbered(self):
        # ODF has no literal series name without a local data table, which an exported chart does not carry.
        records, report = read_back(packaged([column_draft()], ["Data"]))
        self.assertEqual(report_of(report), [])
        self.assertEqual([item["name"].get("text") for item in records[0]["spec"]["series"]], [None, "Series 2"])

    @unittest.skipIf(odf_opendocument is None, "odfpy is not installed")
    def test_odfpy_opens_the_package(self):
        document = odf_opendocument.load(io.BytesIO(packaged([column_draft()], ["Data"])))
        self.assertIsNotNone(document)

    def test_two_charts_keep_their_order(self):
        drafts = [column_draft(name=name) for name in ("Third", "First", "Second")]
        records, report = read_back(packaged(drafts, ["Data"]))
        self.assertEqual(report_of(report), [])
        self.assertEqual([item["name"] for item in records], ["Third", "First", "Second"])

    def test_charts_on_two_sheets(self):
        names = ["Data", "Other"]
        drafts = [column_draft(name="A"), column_draft(name="B", sheet="Other", categories=("Other", 2, 1, 13, 1),
                  series=[{"name": ("ref", ("Other", 1, 2, 1, 2)), "values": ("Other", 2, 2, 13, 2), "x": None}]),
                  column_draft(name="C")]
        drafts = [named(item) for item in drafts]
        records, report = read_back(packaged(drafts, names), names)
        self.assertEqual(report_of(report), [])
        self.assertEqual([(item["name"], names[item["sheet"]]) for item in records],
                         [("A", "Data"), ("C", "Data"), ("B", "Other")])
        for item, draft in zip(records, [drafts[0], drafts[2], drafts[1]]):
            self.assertEqual(as_draft(item, names), draft)
        parts, manifest, frames = write(drafts)
        self.assertEqual({name: [(row, col) for row, col, _ in found] for name, found in frames.items()},
                         {"Data": [(3, 4), (3, 4)], "Other": [(3, 4)]})
        links = {name: [next(iter(frame)).get("xlink:href") for _, _, frame in found] for name, found in frames.items()}
        self.assertEqual(links, {"Data": ["./Object 1", "./Object 3"], "Other": ["./Object 2"]})

    def test_frame_reads_back_at_its_cell(self):
        records, report = read_back(packaged([column_draft(anchor={"row": 5, "col": 3}, offset={"x": 7, "y": 9})], ["Data"]))
        record = records[0]
        self.assertEqual((record["anchor"], record["offset"]), ({"row": 5, "col": 3}, {"x": 7, "y": 9}))
        self.assertNotIn("anchor_absolute", [e["detail"] for e in report.entries()])
        self.assertEqual(record["size"], {"width": 480, "height": 320})

    def test_nothing_built_writes_nothing(self):
        self.assertEqual(write([]), ([], [], {}))


class SeriesNameReport(unittest.TestCase):
    def entries(self, drafts):
        report = ConversionReport()
        write(drafts, report)
        return [(item["category"], item["severity"], item["detail"], sample["sheet"], sample["cell"], item["count"])
                for item in report.entries() for sample in item["samples"]]

    def test_text_names_give_one_entry_per_chart(self):
        self.assertEqual(self.entries([column_draft(series=[
            {"name": ("text", "a"), "values": ("Data", 2, 2, 5, 2), "x": None},
            {"name": ("text", "b"), "values": ("Data", 2, 3, 5, 3), "x": None}])]),
            [("chart.converted", "converted", "series_name", "Data", "Units", 1)])

    def test_text_name_among_reference_names_is_reported(self):
        self.assertEqual(len(self.entries([column_draft()])), 1)  # "South" is a text name

    def test_reference_and_missing_names_give_no_entry(self):
        draft = column_draft(series=[{"name": ("ref", ("Data", 1, 2, 1, 2)), "values": ("Data", 2, 2, 5, 2), "x": None},
                                     {"name": None, "values": ("Data", 2, 3, 5, 3), "x": None}])
        self.assertEqual(self.entries([draft]), [])

    def test_two_charts_give_two_entries_naming_each(self):
        found = self.entries([column_draft(name="First"), column_draft(name="Second" + "x" * 40)])
        self.assertEqual([(item[3], item[4]) for item in found], [("Data", "First"), ("Data", ("Second" + "x" * 40)[:32])])


class NotifyRanges(unittest.TestCase):
    def notified(self, draft):
        _, _, frames = write([draft])
        return frames["Data"][0][2][0].get("draw:notify-on-update-of-ranges")

    def test_column_chart_lists_categories_names_and_values(self):
        draft = column_draft(series=[{"name": ("ref", ("Data", 1, 2, 1, 2)), "values": ("Data", 2, 2, 13, 2), "x": None},
                                     {"name": ("ref", ("Data", 1, 3, 1, 3)), "values": ("Data", 2, 3, 13, 3), "x": None}])
        self.assertEqual(self.notified(draft), "$Data.$A$2:.$A$13 $Data.$B$1 $Data.$B$2:.$B$13 $Data.$C$1 $Data.$C$2:.$C$13")
        records, report = read_back(packaged([draft], ["Data"]))
        self.assertEqual(report_of(report), [])
        self.assertEqual(as_draft(records[0]), draft)

    def test_repeated_ranges_are_listed_once(self):
        draft = column_draft(series=[{"name": None, "values": ("Data", 2, 2, 13, 2), "x": None},
                                     {"name": None, "values": ("Data", 2, 2, 13, 2), "x": None}])
        self.assertEqual(self.notified(draft), "$Data.$A$2:.$A$13 $Data.$B$2:.$B$13")

    def test_scatter_chart_lists_values_and_x(self):
        draft = column_draft(type="scatter", grouping=None, categories=None,
                             series=[{"name": ("ref", ("Data", 1, 3, 1, 3)), "values": ("Data", 2, 3, 13, 3), "x": ("Data", 2, 2, 13, 2)}])
        self.assertEqual(self.notified(draft), "$Data.$C$1 $Data.$C$2:.$C$13 $Data.$B$2:.$B$13")
        records, report = read_back(packaged([draft], ["Data"]))
        self.assertEqual(report_of(report), [])
        self.assertEqual(as_draft(records[0]), draft)

    def test_values_only_chart_lists_its_values(self):
        draft = column_draft(categories=None, series=[{"name": None, "values": ("Data", 2, 2, 5, 2), "x": None}])
        self.assertEqual(self.notified(draft), "$Data.$B$2:.$B$5")

    def test_sheet_name_with_a_space_is_quoted(self):
        names = ["Q1 Sales"]
        draft = column_draft(sheet="Q1 Sales", categories=("Q1 Sales", 2, 1, 13, 1),
                             series=[{"name": ("ref", ("Q1 Sales", 1, 2, 1, 2)), "values": ("Q1 Sales", 2, 2, 13, 2), "x": None}])
        _, _, frames = write([draft])
        expected = "$'Q1 Sales'.$A$2:.$A$13 $'Q1 Sales'.$B$1 $'Q1 Sales'.$B$2:.$B$13"
        self.assertEqual(frames["Q1 Sales"][0][2][0].get("draw:notify-on-update-of-ranges"), expected)
        self.assertEqual(find(chart_document(draft), "plot-area")[0].get("table:cell-range-address"), expected)
        records, report = read_back(packaged([draft], names), names)
        self.assertEqual(report_of(report), [])
        self.assertEqual(as_draft(records[0], names), draft)


class DataBinding(unittest.TestCase):
    """The chart object says that its data comes from the spreadsheet, as LibreOffice Calc writes it."""
    def check(self, draft, ranges):
        document = chart_document(draft)
        [chart] = [item for item in document.iter() if item.tag == "chart:chart"]
        self.assertEqual((chart.get("xlink:href"), chart.get("xlink:type")), ("..", "simple"))
        self.assertEqual(document.get("xmlns:xlink"), "http://www.w3.org/1999/xlink")
        self.assertEqual(document.get("xmlns:table"), "urn:oasis:names:tc:opendocument:xmlns:table:1.0")
        [area] = find(document, "plot-area")
        self.assertEqual(area.get("table:cell-range-address"), ranges)
        self.assertEqual(area.get("chart:data-source-has-labels"), "none")
        _, _, frames = write([draft])
        self.assertEqual(frames["Data"][0][2][0].get("draw:notify-on-update-of-ranges"), ranges)
        records, report = read_back(packaged([draft], ["Data"]))
        self.assertEqual(report_of(report), [])
        self.assertEqual(as_draft(records[0]), draft)

    def test_column_chart(self):
        draft = column_draft(series=[{"name": ("ref", ("Data", 1, 2, 1, 2)), "values": ("Data", 2, 2, 13, 2), "x": None},
                                     {"name": ("ref", ("Data", 1, 3, 1, 3)), "values": ("Data", 2, 3, 13, 3), "x": None}])
        self.check(draft, "$Data.$A$2:.$A$13 $Data.$B$1 $Data.$B$2:.$B$13 $Data.$C$1 $Data.$C$2:.$C$13")

    def test_scatter_chart(self):
        draft = column_draft(type="scatter", grouping=None, categories=None,
                             series=[{"name": ("ref", ("Data", 1, 3, 1, 3)), "values": ("Data", 2, 3, 13, 3), "x": ("Data", 2, 2, 13, 2)}])
        self.check(draft, "$Data.$C$1 $Data.$C$2:.$C$13 $Data.$B$2:.$B$13")


Z_INDEX = "{urn:oasis:names:tc:opendocument:xmlns:drawing:1.0}z-index"


class StackingOrder(unittest.TestCase):
    """Frames sit in their anchor cells, so document order is anchor order; ``draw:z-index`` carries the chart order."""
    def drafts(self):
        # Draft order (stacking order) is the reverse of anchor order.
        return [named(column_draft(name=name, anchor={"row": row, "col": 2})) for name, row in (("Back", 30), ("Middle", 9), ("Front", 2))]

    def sheet_drafts(self, data):
        archive = zipfile.ZipFile(io.BytesIO(data))
        table = next(read_xml(archive, "content.xml").iter(q(T, "table")))
        frames = SheetFrames(archive, 0, "Data", ChartParts(reader=read_part), ConversionReport())
        for frame, cell, grouped in table_frames(table):
            frames.frame(frame, cell, grouped)
        return [draft["name"] for draft in frames.finish()]

    def test_each_frame_carries_its_place_in_draft_order_on_its_sheet(self):
        drafts = self.drafts() + [named(column_draft(name="Elsewhere", sheet="Other", categories=("Other", 2, 1, 13, 1),
                                                     series=[{"name": ("ref", ("Other", 1, 2, 1, 2)), "values": ("Other", 2, 2, 13, 2), "x": None}]))]
        _, _, frames = write(drafts)
        self.assertEqual({name: [(frame.get("draw:name"), frame.get("draw:z-index")) for _, _, frame in found] for name, found in frames.items()},
                         {"Data": [("Back", "0"), ("Middle", "1"), ("Front", "2")], "Other": [("Elsewhere", "0")]})

    def test_reader_orders_a_sheet_by_z_index(self):
        self.assertEqual(self.sheet_drafts(packaged(self.drafts(), ["Data"])), ["Back", "Middle", "Front"])

    def test_reader_keeps_document_order_without_z_index(self):
        data = packaged(self.drafts(), ["Data"], adjust=lambda frame: frame.attrib.pop("draw:z-index"))
        self.assertEqual(self.sheet_drafts(data), ["Front", "Middle", "Back"])

    def test_reader_keeps_document_order_when_one_frame_lacks_z_index(self):
        def adjust(frame):
            if frame.get("draw:name") == "Middle":
                del frame.attrib["draw:z-index"]
        self.assertEqual(self.sheet_drafts(packaged(self.drafts(), ["Data"], adjust=adjust)), ["Front", "Middle", "Back"])

    def test_reader_keeps_document_order_when_a_z_index_cannot_be_read(self):
        def adjust(frame):
            if frame.get("draw:name") == "Middle":
                frame.set("draw:z-index", "-1")
        self.assertEqual(self.sheet_drafts(packaged(self.drafts(), ["Data"], adjust=adjust)), ["Front", "Middle", "Back"])

    def test_reader_keeps_document_order_when_a_z_index_is_too_long(self):
        def adjust(frame):
            if frame.get("draw:name") == "Middle":
                frame.set("draw:z-index", "1" * 5000)
        self.assertEqual(self.sheet_drafts(packaged(self.drafts(), ["Data"], adjust=adjust)), ["Front", "Middle", "Back"])

    def test_values_that_are_not_a_short_plain_decimal_are_absent(self):
        for value in ("-1", "1.5", " 3", "3 ", "\u0663", "", "+3", "1" * 10, "1e3"):
            def adjust(frame, value=value):
                if frame.get("draw:name") == "Middle":
                    frame.set("draw:z-index", value)
            with self.subTest(value=value[:12]):
                self.assertEqual(self.sheet_drafts(packaged(self.drafts(), ["Data"], adjust=adjust)), ["Front", "Middle", "Back"])
        nine = packaged(self.drafts(), ["Data"], adjust=lambda frame: frame.set("draw:z-index", {"Back": "0", "Middle": "999999998", "Front": "999999999"}[frame.get("draw:name")]))
        self.assertEqual(self.sheet_drafts(nine), ["Back", "Middle", "Front"])

    def test_ties_keep_document_order(self):
        data = packaged(self.drafts(), ["Data"], adjust=lambda frame: frame.set("draw:z-index", "5"))
        self.assertEqual(self.sheet_drafts(data), ["Front", "Middle", "Back"])


class Structure(unittest.TestCase):
    def test_parts_and_manifest(self):
        parts, manifest, frames = write([column_draft(name="A"), column_draft(name="B")])
        self.assertEqual([path for path, _ in parts], ["Object 1/content.xml", "Object 2/content.xml"])
        self.assertEqual(manifest, [("Object 1/", MEDIA), ("Object 1/content.xml", "text/xml"),
                                    ("Object 2/", MEDIA), ("Object 2/content.xml", "text/xml")])
        self.assertEqual([(row, col) for row, col, _ in frames["Data"]], [(3, 4), (3, 4)])

    def test_frame_attributes_in_inches(self):
        _, _, frames = write([column_draft()])
        frame = frames["Data"][0][2]
        self.assertEqual((frame.tag, frame.get("draw:name")), ("draw:frame", "Units"))
        self.assertEqual((frame.get("svg:x"), frame.get("svg:y")), (f"{7 / 96:.8f}in", f"{11 / 96:.8f}in"))
        self.assertEqual((frame.get("svg:width"), frame.get("svg:height")), ("5.00000000in", f"{320 / 96:.8f}in"))
        self.assertEqual([item.tag for item in frame], ["draw:object"])

    def test_absent_title_has_no_title_element(self):
        self.assertEqual(find(chart_document(column_draft(title=None, category_title=None, value_title=None)), "title"), [])
        self.assertEqual(len(find(chart_document(column_draft()), "title")), 3)

    def test_legend_none_has_no_legend_element(self):
        self.assertEqual(find(chart_document(column_draft(legend="none")), "legend"), [])
        self.assertEqual([item.get("chart:legend-position") for item in find(chart_document(column_draft(legend="right")), "legend")], ["end"])
        self.assertEqual([item.get("chart:legend-position") for item in find(chart_document(column_draft(legend="bottom")), "legend")], ["bottom"])

    def test_limits_go_on_the_value_axis_style(self):
        document = chart_document(column_draft())
        axis = next(item for item in find(document, "axis") if item.get("chart:dimension") == "y")
        style = next(item for item in find(document, "style") if item.get("style:name") == axis.get("chart:style-name"))
        properties = find(style, "chart-properties")[0]
        self.assertEqual((properties.get("chart:minimum"), properties.get("chart:maximum")), ("0.0", "100.0"))
        free = chart_document(column_draft(min=None, max=None))
        self.assertEqual([item for item in find(free, "chart-properties") if item.get("chart:minimum") or item.get("chart:maximum")], [])

    def test_no_cached_values(self):
        for _, draft in drafts_for_every_fixture():
            document = chart_document(draft)
            with self.subTest(draft["name"]):
                self.assertEqual([item for item in document.iter() if local(item.tag) in ("table", "local-table", "table-cell")], [])
                self.assertEqual([item for item in document.iter() if local(item.tag) in ("cell-content-change", "data-point")], [])
                self.assertNotIn("<table:table", xml_text(document))
        for path, element in write([draft for _, draft in drafts_for_every_fixture()])[0]:
            self.assertEqual([item for item in element.iter() if local(item.tag) in ("table", "table-cell", "table-row")], [], path)

    def test_no_markers_or_gridlines(self):
        for _, draft in drafts_for_every_fixture():
            text = xml_text(chart_document(draft))
            self.assertNotIn("chart:grid", text)
            self.assertNotIn("symbol-type", text)

    def test_draft_is_not_changed(self):
        draft = column_draft()
        before = copy.deepcopy(draft)
        chart_document(draft)
        self.assertEqual(draft, before)

    def test_special_characters(self):
        draft = named(column_draft(name="A & B <1>", title="x\ty\x01z"))
        data = packaged([draft], ["Data"])  # every part must parse
        records, report = read_back(data)
        self.assertEqual(report_of(report), [])
        self.assertEqual(records[0]["name"], "A & B <1>")
        # The ODS reader keeps a literal tab in a title, as the XLSX reader does.
        self.assertEqual(records[0]["spec"]["title"]["text"], "x\tyz")

    def test_characters_xml_cannot_hold_are_dropped_everywhere(self):
        junk = "\x00\x08\x0b\x0c\x0e\x1f￾￿"
        draft = column_draft(name="N" + junk + "m", title="T" + junk + "t", category_title="C" + junk + "c",
                             value_title="V" + junk + "v",
                             series=[{"name": ("ref", ("Data", 1, 2, 1, 2)), "values": ("Data", 2, 2, 5, 2), "x": None}])
        records, report = read_back(packaged([draft], ["Data"]))
        self.assertEqual(report_of(report), [])
        expected = column_draft(name="Nm", title="Tt", category_title="Cc", value_title="Vv",
                                series=[{"name": ("ref", ("Data", 1, 2, 1, 2)), "values": ("Data", 2, 2, 5, 2), "x": None}])
        self.assertEqual(as_draft(records[0]), expected)

    def test_empty_title_is_written_as_no_title(self):
        draft = named(column_draft(title="\x01", category_title="", value_title=None))
        records, report = read_back(packaged([draft], ["Data"]))
        self.assertEqual(report_of(report), [])
        self.assertEqual((records[0]["spec"]["title"]["text"], records[0]["spec"]["axes"]["category"]["title"]), (None, None))

    def test_carriage_returns_read_back_as_line_breaks(self):
        records, _ = read_back(packaged([column_draft(title="a\r\nb\rc")], ["Data"]))
        self.assertEqual(records[0]["spec"]["title"]["text"], "a\nb\nc")


if __name__ == "__main__":
    unittest.main()
