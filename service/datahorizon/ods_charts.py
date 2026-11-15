# SPDX-License-Identifier: GPL-3.0-only
"""Read a ``draw:frame`` chart object of an ODS worksheet into a format-neutral chart draft (see import_charts.convert).

The frame names an object directory of the package; its ``content.xml`` holds ``office:chart`` with one
``chart:chart``.  Packages are untrusted: the object's part is read only through the import's ``ChartParts`` (its
allowance, its frame cap and its caches), the object's path must stay inside the package, and a chart that cannot be
read is reported and omitted, never raised.  A frame that holds something else (an image, a formula, an embedded
document) is not a chart and is passed over without a report.
"""
import math
import re
import xml.etree.ElementTree as ET

from .conversion_report import ConversionReport
from .engine import MAX_COLS, MAX_ROWS
from .export_charts import format_reference
from .import_charts import (DEFAULT_COLUMN_PX, DEFAULT_GROUPING, DEFAULT_ROW_PX, EMPTY_TITLE, LEFT_OUT, MAX_DRAFT_SERIES,
                            MAX_FRAMES, SKIPPED, STYLED, ChartParts, locate, parse_reference)
from .ods import F as FO, O, T, X, Y, q
from .ods_features import CHART, DRAW, SVG, XLINK

MAX_PIXELS = 1 << 30
PIXELS = {"in": 96.0, "cm": 96 / 2.54, "mm": 96 / 25.4, "pt": 96 / 72, "pc": 16.0, "px": 1.0}
LENGTH = re.compile(r"\s*(-?(?:\d+(?:\.\d*)?|\.\d+))\s*(in|cm|mm|pt|pc|px)\s*")
LEGEND_POSITIONS = {"end": "right", "start": "left", "top": "top", "bottom": "bottom", "top-start": "top",
                    "top-end": "top", "bottom-start": "bottom", "bottom-end": "bottom"}
KINDS = {"circle": "pie", "ring": "doughnut"}

NAME = q(DRAW, "name")
HREF = q(XLINK, "href")
STYLE_NAME, CLASS = q(CHART, "style-name"), q(CHART, "class")
STYLE_FAMILY = q(Y, "family")
BODY, DOCUMENT = q(O, "body"), q(O, "document-content")
SERIES_STYLED = frozenset(q(CHART, name) for name in ("chart", "plot-area", "axis", "series", "data-point"))
FONT_STYLED = frozenset(q(CHART, name) for name in ("chart", "title", "legend", "axis"))
FORMAT_STYLED = frozenset(q(CHART, name) for name in ("axis", "series"))
STROKE_COLOURS = (q(SVG, "stroke-color"), q(DRAW, "stroke-color"))
FONTS = (q(FO, "font-size"), q(FO, "font-family"), q(Y, "font-name"), q(FO, "font-weight"), q(FO, "font-style"), q(FO, "color"))


class _Unreadable(Exception):
    pass


class _NotChart(Exception):
    pass


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _kids(element, uri, name):
    return [child for child in element if child.tag == q(uri, name)]


def _kid(element, uri, name):
    if element is not None:
        for child in element:
            if child.tag == q(uri, name):
                return child
    return None


# Package access.

def _object_content(href, names):
    """The ``content.xml`` of the object directory ``href`` names, or ``None`` if it leaves the package or is absent."""
    if not isinstance(href, str) or "\\" in href or "\0" in href:
        return None
    path = (href[2:] if href.startswith("./") else href).rstrip("/")
    if not path or any(part in ("", ".", "..") for part in path.split("/")):
        return None
    content = path + "/content.xml"
    return content if content in names else None


# Placement.

def _pixels(text, default=0):
    """Pixels (96 DPI) of an ODF length; ``default`` for an absent one; raises ``_Unreadable`` for a malformed one."""
    if text is None:
        return default
    found = LENGTH.fullmatch(text)
    if found is None:
        raise _Unreadable
    return max(0, min(int(float(found.group(1)) * PIXELS[found.group(2)] + 0.5), MAX_PIXELS))


def _placement(frame, cell):
    """``(row, col), (x, y), (width, height), absolute`` of a frame; a frame outside a cell is placed by its absolute
    position, measured with the default cell sizes."""
    x, y = _pixels(frame.get(q(SVG, "x"))), _pixels(frame.get(q(SVG, "y")))
    size = (_pixels(frame.get(q(SVG, "width"))), _pixels(frame.get(q(SVG, "height"))))
    if cell is not None:
        return (cell[0], cell[1]), (x, y), size, False
    col, offset_x = locate(x, DEFAULT_COLUMN_PX, MAX_COLS)
    number, offset_y = locate(y, DEFAULT_ROW_PX, MAX_ROWS)
    return (number, col), (offset_x, offset_y), size, True


# The chart.

def _styles(root):
    """``{style name: {attribute: value}}`` over the property elements of the automatic chart styles."""
    found = {}
    for styles in _kids(root, O, "automatic-styles"):
        for style in _kids(styles, Y, "style"):
            merged = found.setdefault(style.get(q(Y, "name")), dict(style.attrib))
            for properties in style:
                merged.update(properties.attrib)
    return found


def _text(element):
    """The paragraphs of a ``chart:title`` joined by newlines, or ``None``."""
    if element is None:
        return dict(EMPTY_TITLE)
    paragraphs = ["".join(paragraph.itertext()) for paragraph in element.iter(q(X, "p"))]
    return {"text": "\n".join(paragraphs) or None, "from_cell": element.get(q(T, "cell-range")) is not None}


def _flag(properties, name):
    return properties.get(q(CHART, name)) == "true"


def _limit(properties, name):
    try:
        number = float(properties.get(q(CHART, name)))
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _reference(element, uri, name):
    text = None if element is None else element.get(q(uri, name))
    return None if text is None else parse_reference(text, "ods")


def _kind(name):
    name = (name or "").rpartition(":")[2]
    return KINDS.get(name, name)


def _plot(kind, properties, secondary):
    grouping = DEFAULT_GROUPING.get(kind)
    if grouping is not None:
        grouping = "percentStacked" if _flag(properties, "percentage") else "stacked" if _flag(properties, "stacked") else grouping
    return {"kind": kind, "direction": ("bar" if _flag(properties, "vertical") else "col") if kind == "bar" else None,
            "grouping": grouping, "three_d": _flag(properties, "three-dimensional"), "secondary": secondary}


def _styled(chart, styles):
    """The dropped styling a chart carries, in the order of ``STYLED``."""
    found = set()
    if any(element.tag == q(CHART, "grid") for element in chart.iter()):
        found.add("gridlines")
    for element in chart.iter():
        properties = styles.get(element.get(STYLE_NAME), {})
        if element.tag in FONT_STYLED and any(name in properties for name in FONTS):
            found.add("fonts")
        if element.tag in FORMAT_STYLED and q(Y, "data-style-name") in properties:
            found.add("number formats")
        if element.tag not in SERIES_STYLED:
            continue
        if q(DRAW, "fill-color") in properties and properties.get(q(DRAW, "fill")) != "none":
            found.add("colours")
        if properties.get(q(DRAW, "stroke")) != "none" and any(name in properties for name in STROKE_COLOURS):
            found.add("colours")
        if properties.get(q(CHART, "data-label-number"), "none") != "none":
            found.add("data labels")
        if q(CHART, "gap-width") in properties or q(CHART, "overlap") in properties:
            found.add("gap width")
        if properties.get(q(CHART, "symbol-type"), "none") != "none":
            found.add("markers")
        if properties.get(q(CHART, "interpolation"), "none") != "none":
            found.add("smoothing")
    return [item for item in STYLED if item in found]


def _read_chart(root):
    """The chart-specific part of a draft from an object's ``content.xml`` root.

    Raises ``_NotChart`` for another kind of object and ``_Unreadable`` for a part that is neither."""
    if _local(root.tag) == "math":
        raise _NotChart
    body = _kid(root, O, "body") if root.tag == DOCUMENT else None
    if body is None or len(body) == 0:
        raise _Unreadable
    holder = body[0]
    if holder.tag != q(O, "chart"):
        raise _NotChart
    chart = _kid(holder, CHART, "chart")
    area = _kid(chart, CHART, "plot-area")
    if area is None:
        raise _Unreadable
    styles = _styles(root)
    own = styles.get(area.get(STYLE_NAME), {})
    axes = _kids(area, CHART, "axis")
    horizontal = next((item for item in axes if item.get(q(CHART, "dimension")) == "x"), None)
    values = [item for item in axes if item.get(q(CHART, "dimension")) == "y"]
    names = [item.get(q(CHART, "name")) for item in values]
    primary = "primary-y" if "primary-y" in names else (names[0] if names else None)
    vertical = next((item for item in values if item.get(q(CHART, "name")) == primary), None)
    limits = styles.get(vertical.get(STYLE_NAME), {}) if vertical is not None else {}
    categories = _kid(horizontal, CHART, "categories")
    if categories is None:
        categories = next(area.iter(q(CHART, "categories")), None)
    shared = _reference(categories, T, "cell-range-address")
    chart_kind = _kind(chart.get(CLASS))
    plots, series, seen = [], [], set()
    for element in _kids(area, CHART, "series"):
        if len(series) >= MAX_DRAFT_SERIES:
            break
        kind = _kind(element.get(CLASS)) or chart_kind
        attached = element.get(q(CHART, "attached-axis"))
        secondary = attached is not None and attached in names and attached != primary
        if (kind, secondary) not in seen:
            seen.add((kind, secondary))
            plots.append(_plot(kind, own, secondary))
        scatter = kind in ("scatter", "bubble")
        label = _reference(element, CHART, "label-cell-address")
        series.append({"name": None if label is None else {"ref": label},
                       "values": _reference(element, CHART, "values-cell-range-address"),
                       "categories": None if scatter else shared,
                       "x": _reference(_kid(element, CHART, "domain"), T, "cell-range-address") if scatter else None})
    legend = _kid(chart, CHART, "legend")
    return {"plots": plots or [_plot(chart_kind, own, False)],
            "title": _text(_kid(chart, CHART, "title")),
            "axis_titles": {"category": _text(_kid(horizontal, CHART, "title")), "value": _text(_kid(vertical, CHART, "title"))},
            "value_min": _limit(limits, "minimum"), "value_max": _limit(limits, "maximum"),
            "legend": None if legend is None else LEGEND_POSITIONS.get(legend.get(q(CHART, "legend-position"), "end"), "right"),
            "series": series, "styled": _styled(chart, styles)}


NOT_CHART = object()  # the body of an object that is not a chart


def _chart_body(root):
    try:
        return _read_chart(root)
    except _NotChart:
        return NOT_CHART


def read_frame_draft(archive, frame: ET.Element, position: int, sheet_name: str, order: int, parts,
                     report: ConversionReport, cell, *, claim=True, on_chart=None):
    """The draft of the chart a ``draw:frame`` holds, or ``None``.

    ``None`` without a report: the frame is not a chart object.  ``None`` with a ``chart.skipped`` entry: the object
    cannot be read (``unreadable``), or no further chart can be imported (``workbook_limit`` and whatever ``convert``
    would report; an object not yet read is then not read, so it is reported ``workbook_limit`` even if it is not a
    chart).  ``parts`` is the import's ``ChartParts`` (a callable ``read_part(archive, name)`` returning the parsed
    root of a part gets a reader of its own).  ``cell`` is the 1-based ``(row, col)`` of the cell holding the frame,
    or ``None`` for a frame in ``table:shapes``, which is placed by its absolute position on the default cell sizes,
    64 and 20 pixels.  ``claim=False`` keeps the draft from counting toward the import's chart limit (a grouped chart,
    which is reported and left out).  ``on_chart()`` is called once the frame's object is known to be a chart,
    whatever becomes of the chart after (a placement that cannot be read, or an import that is full, skips it)."""
    if not isinstance(parts, ChartParts):
        parts = ChartParts(reader=parts)
    label = frame.get(NAME) or f"Chart {order + 1}"
    try:
        element = _kid(frame, DRAW, "object")
        if element is None:
            return None
        part = _object_content(element.get(HREF), parts.names(archive))
        body = parts.body(archive, part, _chart_body) if part is not None else None
        if body is NOT_CHART:
            return None
        if body is LEFT_OUT:
            report.add(SKIPPED, "omitted", "workbook_limit", sheet=sheet_name, cell=label[:32], count=1)
            return None
        if body is None:
            raise _Unreadable
        parts.objects.add(part)
        if on_chart is not None:
            on_chart()
        (row, col), (x, y), (width, height), absolute = _placement(frame, cell)
        return parts.draft(body, {"sheet": position, "name": label, "order": order,
                                  "anchor": {"row": row, "col": col}, "offset": {"x": x, "y": y},
                                  "size": {"width": width, "height": height}, "approximate_size": False,
                                  "absolute_anchor": absolute}, report, claim=claim)
    except Exception:  # an untrusted chart never fails the import, whatever is wrong with it
        report.add(SKIPPED, "omitted", "unreadable", sheet=sheet_name, cell=label[:32], count=1)
        return None


# A worksheet's frames, for the whole-workbook readers.

FRAME, OBJECT, GROUP = q(DRAW, "frame"), q(DRAW, "object"), q(DRAW, "g")
CELLS = (q(T, "table-cell"), q(T, "covered-table-cell"))
ROW = q(T, "table-row")
ROW_GROUPS = (q(T, "table-row-group"), q(T, "table-header-rows"), q(T, "table-rows"))


Z_INDEX = q(DRAW, "z-index")


def _z_index(frame):
    """A frame's ``draw:z-index`` (a non-negative integer), or ``None``."""
    text = frame.get(Z_INDEX)
    return int(text) if text is not None and text.isascii() and text.isdigit() else None


class SheetFrames:
    """The drafts of one ``table:table``'s chart frames, read in document order as a reader meets them.

    ``parts`` is the import's ``ChartParts``: at most ``MAX_FRAMES`` object frames are read in the whole import; this
    sheet's frames past that are reported once, with their count, by ``finish``.  A chart in a ``draw:g`` group is
    reported as ``grouped`` and left out.  ``charts`` holds the frames known to be charts that are not grouped: a
    frame is added when its object has been read and is removed by ``OdsSheetFeatures`` when the frame ends, so that
    the sheet's ``drawings`` count leaves it out (a frame that was not read, or whose object is not a chart, stays in)."""
    def __init__(self, archive, position, sheet_name, parts, report):
        self.archive, self.position, self.sheet_name, self.parts, self.report = archive, position, sheet_name, parts, report
        self.drafts, self.order, self.unread, self.last = [], 0, 0, None
        self.charts = set()
        self.stacking = []  # each draft's ``draw:z-index``, or ``None`` where its frame has none that can be read

    def frame(self, frame, cell, grouped):
        """A ``draw:frame`` holding a ``draw:object``; ``cell`` as ``read_frame_draft`` takes it.  A frame is read once."""
        if frame is self.last:
            return
        self.last = frame
        self.parts.tick()
        if self.parts.frames >= MAX_FRAMES:
            self.unread += 1
            return
        self.parts.frames += 1
        draft = read_frame_draft(self.archive, frame, self.position, self.sheet_name, self.order, self.parts, self.report, cell,
                                 claim=not grouped, on_chart=None if grouped else lambda: self.charts.add(frame))
        self.order += 1
        if draft is None:
            return
        if grouped:
            self.report.add(SKIPPED, "omitted", "grouped", sheet=self.sheet_name, cell=draft["name"][:32], count=1)
        else:
            self.drafts.append(draft)
            self.stacking.append(_z_index(frame))

    def finish(self, sizes_unknown=False):
        """The drafts read; ``sizes_unknown`` marks those placed by absolute position as ``approximate_size``.

        When every chart frame read carries a ``draw:z-index``, the drafts are in that order (document order breaks
        ties); otherwise, as for files from applications that do not write one, they stay in document order."""
        if self.drafts and None not in self.stacking:
            ranked = sorted(range(len(self.drafts)), key=lambda number: (self.stacking[number], number))
            self.drafts = [self.drafts[number] for number in ranked]
            self.stacking = [self.stacking[number] for number in ranked]
        if self.unread:
            self.report.add(SKIPPED, "omitted", "unreadable", sheet=self.sheet_name, cell="drawing", count=self.unread)
        for draft in self.drafts:
            if sizes_unknown and draft["absolute_anchor"]:
                draft["approximate_size"] = True
        return self.drafts


def table_frames(table):
    """``(frame, cell, grouped)`` for each ``draw:frame`` holding a ``draw:object`` in a parsed ``table:table``, in
    document order, as the streaming reader meets them: ``cell`` is the 1-based ``(row, col)`` of the cell of a sheet
    row holding the frame, or ``None``; ``grouped`` says the frame is inside a ``draw:g``."""
    from .ods import repeat
    found, row = [], [1]

    def visit(item, cell, grouped):
        if item.tag == FRAME and _kid(item, DRAW, "object") is not None:
            found.append((item, cell, grouped))
        grouped = grouped or item.tag == GROUP
        for child in item:
            visit(child, cell, grouped)

    def rows(element):
        for child in element:
            if child.tag in ROW_GROUPS:
                rows(child)
            elif child.tag == ROW:
                col = 1
                for item in child:
                    visit(item, (row[0], col) if item.tag in CELLS else None, False)
                    if item.tag in CELLS:
                        col += repeat(item, "number-columns-repeated", MAX_COLS)
                row[0] += repeat(child, "number-rows-repeated", MAX_ROWS)
            else:
                visit(child, None, False)
    rows(table)
    return found


def sizes_declared(features) -> bool:
    """Whether a sheet's ``OdsSheetFeatures`` met column widths or custom row heights, which the readers do not measure."""
    return any(width is not None for width in features.widths) or "row_heights" in features.counts


# Writing.
#
# ``chart_document`` and ``package`` write the parts of an export draft with literal prefixed names
# (``chart:chart``) and ``xmlns:`` attributes, like the XLSX writer: the elements serialize as they are.  Nothing is
# styled (the native chart has none) and no part carries a local data table or cached values: the chart names the
# cells of the sheets it draws.  ODF has no literal series name outside such a table, so a series named by text is
# written without a label and reads back as ``Series N``; a series named by a cell keeps its name.

PIXELS_PER_INCH = 96
CHART_MEDIA = "application/vnd.oasis.opendocument.chart"
NAMESPACES = {"xmlns:office": O, "xmlns:style": Y, "xmlns:text": X, "xmlns:table": T, "xmlns:fo": FO,
              "xmlns:draw": DRAW, "xmlns:chart": CHART, "xmlns:svg": SVG, "xmlns:xlink": XLINK}
CLASSES = {"column": "chart:bar", "bar": "chart:bar", "line": "chart:line", "area": "chart:area", "pie": "chart:circle",
           "scatter": "chart:scatter"}
LEGEND_WORDS = {"right": "end", "bottom": "bottom"}
UNWRITABLE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")  # what XML 1.0 cannot hold
LINE_BREAK = re.compile("\r\n|\r|\n")


def _clean(text):
    """``text`` without the characters XML 1.0 cannot hold; carriage returns become line feeds, as a parser reads them."""
    return LINE_BREAK.sub("\n", UNWRITABLE.sub("", text))


def _add(parent, tag, text=None, **attributes):
    element = ET.SubElement(parent, tag, attributes)
    if text is not None:
        element.text = text
    return element


def _title(parent, text):
    title = _add(parent, "chart:title")
    for line in text.split("\n"):
        _add(title, "text:p", line)


def _usable(text):
    return text if text and _clean(text) else None


def _length(pixels):
    return f"{pixels / PIXELS_PER_INCH:.8f}in"


def _style(styles, name, **properties):
    style = _add(styles, "style:style", **{"style:name": name, "style:family": "chart"})
    _add(style, "style:chart-properties", **properties)


def chart_document(draft):
    """The ``office:document-content`` of the chart object of an export draft."""
    kind, grouping = draft["type"], draft["grouping"]
    if not draft["series"]:
        raise ValueError("a chart needs a series")
    root = ET.Element("office:document-content", {**NAMESPACES, "office:version": "1.3"})
    styles = _add(root, "office:automatic-styles")
    # As LibreOffice Calc writes it: ``xlink:href=".."`` says the data comes from the spreadsheet holding the object, not
    # from a table of the chart's own (which an exported chart does not carry).
    chart = _add(_add(_add(root, "office:body"), "office:chart"), "chart:chart",
                 **{"chart:class": CLASSES[kind], "xlink:href": "..", "xlink:type": "simple"})
    title = _usable(draft["title"])
    if title is not None:
        _title(chart, _clean(title))
    if draft["legend"] in LEGEND_WORDS:
        _add(chart, "chart:legend", **{"chart:legend-position": LEGEND_WORDS[draft["legend"]]})
    flags = {}
    if kind == "bar":
        flags["chart:vertical"] = "true"
    if grouping == "stacked" and kind in ("column", "bar", "area"):
        flags["chart:stacked"] = "true"
    area = _add(chart, "chart:plot-area", **{"table:cell-range-address": " ".join(_sources(draft)),
                                             "chart:data-source-has-labels": "none"})
    if flags:
        _style(styles, "plot", **flags)
        area.set("chart:style-name", "plot")
    scatter = kind == "scatter"
    horizontal = _add(area, "chart:axis", **{"chart:dimension": "x", "chart:name": "primary-x"})
    titles = [_usable(draft[key]) for key in ("category_title", "value_title")]
    if kind != "pie" and titles[0] is not None:
        _title(horizontal, _clean(titles[0]))
    if draft["categories"] is not None and not scatter:
        _add(horizontal, "chart:categories", **{"table:cell-range-address": format_reference(draft["categories"], "ods")})
    if kind != "pie":
        vertical = _add(area, "chart:axis", **{"chart:dimension": "y", "chart:name": "primary-y"})
        limits = {f"chart:{word}": repr(float(draft[key])) for key, word in (("min", "minimum"), ("max", "maximum"))
                  if draft[key] is not None}
        if limits:
            _style(styles, "axis-y", **limits)
            vertical.set("chart:style-name", "axis-y")
        if titles[1] is not None:
            _title(vertical, _clean(titles[1]))
    for item in draft["series"]:
        attributes = {"chart:values-cell-range-address": format_reference(item["values"], "ods")}
        if item["name"] is not None and item["name"][0] == "ref":
            attributes["chart:label-cell-address"] = format_reference(item["name"][1], "ods")
        series = _add(area, "chart:series", **attributes)
        if scatter and item["x"] is not None:
            _add(series, "chart:domain", **{"table:cell-range-address": format_reference(item["x"], "ods")})
    return root


def _sources(draft):
    """The source ranges of a draft in the ODS dialect: categories, then each series' name cell, values and x; once each."""
    refs = [draft["categories"]]
    for item in draft["series"]:
        refs += [item["name"][1] if item["name"] is not None and item["name"][0] == "ref" else None, item["values"], item["x"]]
    found = []
    for ref in refs:
        text = None if ref is None else format_reference(ref, "ods")
        if text is not None and text not in found:
            found.append(text)
    return found


def _frame(number, z, draft):
    place, offset, size = draft["anchor"], draft["offset"], draft["size"]
    # A frame sits in its anchor cell, so document order is anchor order; ``draw:z-index`` keeps the stacking order.
    frame = ET.Element("draw:frame", {"draw:name": _clean(draft["name"]), "draw:z-index": str(z),
                                      "svg:x": _length(offset["x"]), "svg:y": _length(offset["y"]),
                                      "svg:width": _length(size["width"]), "svg:height": _length(size["height"])})
    # As LibreOffice writes it: the chart listens to its source ranges, so it redraws when those cells change.
    _add(frame, "draw:object", **{"draw:notify-on-update-of-ranges": " ".join(_sources(draft)),
                                  "xlink:href": f"./Object {number}", "xlink:type": "simple", "xlink:show": "embed",
                                  "xlink:actuate": "onLoad"})
    return place["row"], place["col"], frame


def package(built, report):
    """The parts that carry the built charts of an export.

    ``built`` is ``export_charts.build``'s ``(draft, chart document)`` pairs.  Returns ``(parts, manifest, frames)``:
    the ``(path, element)`` of each chart object's ``Object N/content.xml`` (N counting the charts in draft order from
    1); the manifest entries, ``(full path, media type)``, of each object directory and its content part; and, for each
    exported sheet name, the ``(anchor row, anchor col, draw:frame)`` of its charts in draft order.  A frame is a
    cell-anchored frame: the caller places it in the cell at its anchor, so that the sheet reads back the anchor and
    the offset the draft has; its ``draw:z-index`` is its place in draft order on its sheet, from 0, which the reader
    orders the sheet's charts by.  Lengths are inches at 96 pixels to the inch.

    ODF holds no literal series name outside a local data table, which an exported chart does not carry, so a chart
    with a series named by text is still written, that name left out, and the import would number it ``Series N``.
    ``report`` (a ``ConversionReport``) gets one ``chart.converted`` / ``series_name`` entry for each such chart, on its
    sheet, named by the chart (the importer's own entry for a series name it replaced)."""
    parts, manifest, frames = [], [], {}
    for number, (draft, document) in enumerate(built, 1):
        if any(item["name"] is not None and item["name"][0] == "text" for item in draft["series"]):
            report.add("chart.converted", "converted", "series_name", sheet=draft["sheet"], cell=draft["name"][:32])
        parts.append((f"Object {number}/content.xml", document))
        manifest += [(f"Object {number}/", CHART_MEDIA), (f"Object {number}/content.xml", "text/xml")]
        placed = frames.setdefault(draft["sheet"], [])
        placed.append(_frame(number, len(placed), draft))
    return parts, manifest, frames
