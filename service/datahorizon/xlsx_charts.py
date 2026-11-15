# SPDX-License-Identifier: GPL-3.0-only
"""Read a worksheet's drawing and chart parts into format-neutral chart drafts (see import_charts.convert).

Path: worksheet part -> its relationships -> the relationship its ``<drawing r:id>`` names -> drawing part -> each
anchor's graphicFrame -> ``r:id`` -> chart part.  Elements are matched by local name, so Transitional and Strict
packages read alike.  Packages are untrusted: every part is read through the import's ``ChartParts`` (its
allowance, its frame cap and its caches), relationship targets must stay inside the package, and a chart that cannot
be read is reported and omitted, never raised.
"""
import math
import posixpath
import re
import xml.etree.ElementTree as ET

from .conversion_report import ConversionReport
from .engine import MAX_COLS, MAX_ROWS
from .export_charts import format_reference
from .import_charts import (DEFAULT_COLUMN_PX, DEFAULT_GROUPING, DEFAULT_ROW_PX, EMPTY_TITLE, LEFT_OUT, MAX_DRAFT_SERIES,
                            MAX_FRAMES, SKIPPED, STYLED, UNUSABLE, ChartParts, locate, parse_reference)
from .xlsx_features import local, relationships_path

EMU_PER_PIXEL = 9525
MAX_EMU = 1 << 40
CHARTEX = "chartex"
ANCHORS = ("twoCellAnchor", "oneCellAnchor", "absoluteAnchor")
LEGEND_POSITIONS = {"r": "right", "b": "bottom", "t": "top", "l": "left", "tr": "right"}
FILLS = ("solidFill", "gradFill", "pattFill", "blipFill")
LABEL_FLAGS = ("showLegendKey", "showVal", "showCatName", "showSerName", "showPercent", "showBubbleSize")


class _Unreadable(Exception):
    pass


def _kids(element, name):
    return [child for child in element if local(child.tag) == name]


def _kid(element, name):
    if element is not None:
        for child in element:
            if local(child.tag) == name:
                return child
    return None


def _val(element, name, default=None):
    child = _kid(element, name)
    return default if child is None else child.get("val", default)


def _true(text, default=True):
    return default if text is None else text.strip().lower() in ("1", "true")


def _namespace(tag):
    return tag[1:].split("}", 1)[0] if tag.startswith("{") else ""


# Package access.

def _resolve(base, target, names):
    """The package part a relationship of ``base`` names, or ``None`` if it leaves the package or is absent."""
    if not isinstance(target, str) or "\\" in target or "\0" in target:
        return None
    path = posixpath.normpath(target.lstrip("/") if target.startswith("/") else posixpath.join(posixpath.dirname(base), target))
    if path.startswith("..") or path.startswith("/") or path not in names:
        return None
    return path


def _relationships(archive, part, parts, names):
    """``{id: (kind, target)}`` for the internal relationships of ``part``; ``None`` when unreadable, ``{}`` when absent."""
    path = relationships_path(part)
    if path not in names:
        return {}
    root = parts.read(archive, path)
    if root is None:
        return None
    found = {}
    for item in _kids(root, "Relationship"):
        if item.get("TargetMode") != "External" and item.get("Id") is not None:
            found[item.get("Id")] = (item.get("Type", "").rstrip("/").rsplit("/", 1)[-1], item.get("Target", ""))
    return found


def _relationship_id(element):
    for key, value in element.attrib.items():
        if key.startswith("{") and key.endswith("}id") and "relationships" in key:
            return value
    return None


# Anchors.

def _pixels(emu):
    return int(emu / EMU_PER_PIXEL + 0.5)


def _number(text):
    try:
        return max(0, min(int((text or "0").strip()), MAX_EMU))
    except ValueError as error:
        raise _Unreadable from error


def _corner(element):
    """``(col, col offset px, row, row offset px)`` of an ``xdr:from`` or ``xdr:to`` element (0-based cells)."""
    if element is None:
        raise _Unreadable
    values = {name: _number(getattr(_kid(element, name), "text", None)) for name in ("col", "colOff", "row", "rowOff")}
    return values["col"], _pixels(values["colOff"]), values["row"], _pixels(values["rowOff"])


def _attribute_pair(element, first, second):
    if element is None:
        raise _Unreadable
    return _pixels(_number(element.get(first))), _pixels(_number(element.get(second)))


def _distance(first, offset_first, last, offset_last, default):
    """Pixels from ``offset_first`` into 0-based cell ``first`` to ``offset_last`` into cell ``last``, on cells of ``default`` pixels."""
    return offset_last - offset_first + max(last - first, 0) * default


def _placement(anchor):
    """``(row, col), (x, y), (width, height), absolute`` of a drawing anchor element, measured with default cell sizes."""
    kind = local(anchor.tag)
    if kind == "twoCellAnchor":
        col1, x1, row1, y1 = _corner(_kid(anchor, "from"))
        col2, x2, row2, y2 = _corner(_kid(anchor, "to"))
        size = (max(_distance(col1, x1, col2, x2, DEFAULT_COLUMN_PX), 0), max(_distance(row1, y1, row2, y2, DEFAULT_ROW_PX), 0))
        return (row1 + 1, col1 + 1), (x1, y1), size, False
    size = _attribute_pair(_kid(anchor, "ext"), "cx", "cy")
    if kind == "oneCellAnchor":
        col1, x1, row1, y1 = _corner(_kid(anchor, "from"))
        return (row1 + 1, col1 + 1), (x1, y1), size, False
    x, y = _attribute_pair(_kid(anchor, "pos"), "x", "y")
    col, offset_x = locate(x, DEFAULT_COLUMN_PX, MAX_COLS)
    number, offset_y = locate(y, DEFAULT_ROW_PX, MAX_ROWS)
    return (number, col), (offset_x, offset_y), size, True


# Chart parts.

def _title(element):
    """``{"text", "from_cell"}`` of a ``c:title`` (``None`` for an absent one)."""
    text, from_cell = None, False
    tx = _kid(element, "tx")
    rich, reference = _kid(tx, "rich"), _kid(tx, "strRef")
    if rich is not None:
        paragraphs = ["".join(item.text or "" for item in paragraph.iter() if local(item.tag) == "t")
                      for paragraph in rich.iter() if local(paragraph.tag) == "p"]
        text = "\n".join(paragraphs) or None
    elif reference is not None:
        from_cell = True
        cached = next((item for item in reference.iter() if local(item.tag) == "v"), None)
        text = (cached.text or None) if cached is not None else None
    return {"text": text, "from_cell": from_cell} if element is not None else dict(EMPTY_TITLE)


def _source(container):
    """``None`` for an absent source, ``UNUSABLE`` for literal or unparsable data, else the reference."""
    if container is None:
        return None
    for kind in ("numRef", "strRef", "multiLvlStrRef"):
        reference = _kid(container, kind)
        if reference is not None:
            formula = getattr(_kid(reference, "f"), "text", None)
            return parse_reference(formula, "xlsx") if formula else UNUSABLE
    return UNUSABLE if any(local(item.tag) in ("numLit", "strLit") for item in container) else None


def _series_name(element):
    tx = _kid(element, "tx")
    if tx is None:
        return None
    if _kid(tx, "strRef") is not None:
        return {"ref": _source(tx)}
    literal = getattr(_kid(tx, "v"), "text", None)
    return {"text": literal} if literal else None


def _series(plot, kind, room):
    """The first ``room`` series of a plot, in series order (``room`` bounds the work done on an oversized chart)."""
    items = []
    for number, element in enumerate(_kids(plot, "ser")):
        try:
            order = int(_val(element, "order", number))
        except ValueError:
            order = number
        items.append((order, number, element))
    scatter = kind in ("scatter", "bubble")
    return [{"name": _series_name(element), "values": _source(_kid(element, "yVal" if scatter else "val")),
             "categories": None if scatter else _source(_kid(element, "cat")),
             "x": _source(_kid(element, "xVal")) if scatter else None}
            for _, _, element in sorted(items, key=lambda item: item[:2])[:room]]


def _plots(area):
    """``(element, plot dict, axis ids)`` for each chart element of a plot area."""
    plots = []
    for element in area:
        name = local(element.tag)
        if not name.endswith("Chart") or name == "Chart":
            continue
        kind = name[:-len("Chart")]
        three_d = kind.endswith("3D")
        kind = kind[:-2] if three_d else kind
        plots.append((element, {"kind": kind, "direction": (_val(element, "barDir") or "col") if kind == "bar" else None,
                                "grouping": _val(element, "grouping") or DEFAULT_GROUPING.get(kind),
                                "three_d": three_d, "secondary": False},
                      {item.get("val") for item in _kids(element, "axId")}))
    return plots


def _mark_secondary(plots, area):
    """A plot is secondary when its axes differ from the first plot's, or it uses a value axis crossing at the far side."""
    values = _kids(area, "valAx")
    far = {_val(axis, "axId") for axis in values if _val(axis, "crosses") == "max"} if len(values) > 1 else set()
    for number, (_, plot, axes) in enumerate(plots):
        plot["secondary"] = (number > 0 and axes != plots[0][2]) or bool(far & axes)


def _axes(area):
    """The category (or horizontal) axis and the value axis of a plot area, either possibly ``None``."""
    categories = _kids(area, "catAx") + _kids(area, "dateAx")
    values = _kids(area, "valAx")
    if categories:
        return categories[0], (values[0] if values else None)
    horizontal = [item for item in values if _val(item, "axPos") in ("b", "t")]
    first = horizontal[0] if horizontal else (values[0] if values else None)
    rest = [item for item in values if item is not first]
    return first, (rest[0] if rest else None)


def _limit(axis, name):
    try:
        number = float(_val(_kid(axis, "scaling"), name))
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _filled(element):
    return element is not None and any(local(child.tag) in FILLS for child in element)


def _overlap_is_full(element):
    try:
        return float(element.get("val")) == 100
    except (TypeError, ValueError):
        return False


def _styled(root):
    """The dropped styling a chart carries, in the order of ``STYLED``."""
    found = set()
    if _filled(_kid(root, "spPr")):
        found.add("colours")
    whole = {id(overlap) for plot in root.iter() if local(plot.tag) == "barChart" and _val(plot, "grouping") == "stacked"
             for overlap in _kids(plot, "overlap") if _overlap_is_full(overlap)}  # what stacked means, not styling
    for element in root.iter():
        if id(element) in whole:
            continue
        name = local(element.tag)
        if name in ("ser", "dPt") and _filled(_kid(element, "spPr")):
            found.add("colours")
        elif name == "dLbls" and any(local(child.tag) in LABEL_FLAGS and _true(child.get("val")) for child in element):
            found.add("data labels")
        elif name in ("gapWidth", "overlap"):
            found.add("gap width")
        elif name == "marker" and _val(element, "symbol", "none") != "none":
            found.add("markers")
        elif name == "smooth" and _true(element.get("val")):
            found.add("smoothing")
        elif name in ("majorGridlines", "minorGridlines"):
            found.add("gridlines")
        elif name == "numFmt" and not _true(element.get("sourceLinked"), False):
            found.add("number formats")
        elif name == "txPr":
            found.add("fonts")
    return [item for item in STYLED if item in found]


def _all_series(plots):
    """The series of every plot, stopping at ``MAX_DRAFT_SERIES``: one past the limit is enough for ``convert`` to skip."""
    found = []
    for element, plot, _ in plots:
        found += _series(element, plot["kind"], MAX_DRAFT_SERIES - len(found))
    return found


def _read_chart(root):
    """The chart-specific part of a draft from a ``c:chartSpace`` root; raises ``_Unreadable``."""
    chart = _kid(root, "chart") if local(root.tag) == "chartSpace" else None
    if chart is None:
        raise _Unreadable
    area = _kid(chart, "plotArea")
    plots = _plots(area) if area is not None else []
    if area is not None:
        _mark_secondary(plots, area)
    category, value = _axes(area) if area is not None else (None, None)
    legend = _kid(chart, "legend")
    return {"plots": [plot for _, plot, _ in plots],
            "title": _title(_kid(chart, "title")),
            "axis_titles": {"category": _title(_kid(category, "title")), "value": _title(_kid(value, "title"))},
            "value_min": _limit(value, "min"), "value_max": _limit(value, "max"),
            "legend": None if legend is None else LEGEND_POSITIONS.get(_val(legend, "legendPos", "r")),
            "series": _all_series(plots),
            "styled": _styled(root)}


def _extended():
    """The draft body of a ``cx:chart`` graphic: its part is not read, ``convert`` skips the kind."""
    return {"plots": [{"kind": "extended", "direction": None, "grouping": None, "three_d": False, "secondary": False}],
            "title": dict(EMPTY_TITLE), "axis_titles": {"category": dict(EMPTY_TITLE), "value": dict(EMPTY_TITLE)},
            "value_min": None, "value_max": None, "legend": None, "series": [], "styled": []}


# Drawings.

def _chart_element(frame):
    data = next((item for item in frame.iter() if local(item.tag) == "graphicData"), None)
    return None if data is None else next((child for child in data if local(child.tag) == "chart"), None)


def _choose(content):
    """The graphicFrame of an ``mc:AlternateContent`` to read: a regular chart first, else an extended one."""
    frames = [frame for branch in content for frame in _kids(branch, "graphicFrame") if _chart_element(frame) is not None]
    regular = [frame for frame in frames if CHARTEX not in _namespace(_chart_element(frame).tag)]
    return (regular or frames or [None])[0]


def _frames(anchor):
    """``(graphicFrame, grouped)`` for each chart-bearing frame of an anchor, in document order."""
    found = []

    def visit(element, grouped):
        for child in element:
            name = local(child.tag)
            if name == "graphicFrame" and _chart_element(child) is not None:
                found.append((child, grouped))
            elif name == "grpSp":
                visit(child, True)
            elif name == "AlternateContent":
                chosen = _choose(_kids(child, "Choice") + _kids(child, "Fallback"))
                if chosen is not None:
                    found.append((chosen, grouped))
    visit(anchor, False)
    return found


def _anchors(root):
    for child in root:
        if local(child.tag) in ANCHORS:
            yield child
        elif local(child.tag) == "AlternateContent":
            for branch in _kids(child, "Choice") + _kids(child, "Fallback"):
                inner = [item for item in branch if local(item.tag) in ANCHORS]
                if inner:
                    yield from inner
                    break


def _frame_name(frame, number):
    name = next((item.get("name") for item in frame.iter() if local(item.tag) == "cNvPr"), None)
    return name or f"Chart {number}"


EXTENDED = "extended"  # a frame's chart that is a ``cx:chart``, whose part is not read


def _frame_entry(anchor, frame, grouped, label, placed, drawing, links, names):
    """What reading a frame needs, kept instead of the drawing's tree: its label and, unless it is grouped, its
    placement (``None`` when unreadable) and its chart (``EXTENDED``, the chart part, or ``None`` when unresolvable)."""
    if grouped:
        return {"label": label, "grouped": True}
    element = _chart_element(frame)
    if CHARTEX in _namespace(element.tag):
        chart = EXTENDED
    else:
        link = links.get(_relationship_id(element))
        chart = _resolve(drawing, link[1], names) if link is not None and link[0] == "chart" else None
    return {"label": label, "grouped": False, "placed": placed, "one_cell": local(anchor.tag) == "oneCellAnchor", "chart": chart}


def _drawing_frames(archive, drawing, parts, names, room):
    """``(entries, count)`` of a drawing part: the ``_frame_entry`` of its first ``room`` chart frames and how many
    chart frames it holds, or ``None`` when the drawing or its relationships cannot be read.  The parsed drawing is
    released when this returns."""
    root = parts.read(archive, drawing)
    links = _relationships(archive, drawing, parts, names) if root is not None else None
    if links is None:
        return None
    entries, count = [], 0
    for anchor in _anchors(root):
        placement = []  # the anchor's placement (None: unreadable), computed on its first frame that is kept
        for frame, grouped in _frames(anchor):
            if count < room:
                if not placement and not grouped:
                    try:
                        placement.append(_placement(anchor))
                    except _Unreadable:
                        placement.append(None)
                entries.append(_frame_entry(anchor, frame, grouped, _frame_name(frame, count + 1),
                                            placement[0] if placement else None, drawing, links, names))
            count += 1
    return entries, count


def read_sheet_drafts(archive, sheet_part, position, sheet_name, parts, report: ConversionReport, drawing_id, *,
                      sizes_unknown=False) -> list:
    """Drafts of the charts in a worksheet's drawing, in drawing order.

    ``drawing_id`` is the ``r:id`` of the worksheet's ``<drawing>`` (``None``: the sheet has none); only that
    relationship is followed.  ``parts`` is the import's ``ChartParts`` (a ``ByteBudget`` reads under that budget,
    with a reader of its own).  Unreadable and grouped charts are reported (``chart.skipped``) and left out, as are
    the frames past the import's ``MAX_FRAMES``, counted in one entry.  Charts are measured with the default cell
    sizes, 64 and 20 pixels; ``sizes_unknown`` says the worksheet declares other cell sizes, so every draft placed by
    measuring cells (all but one-cell anchors) is marked ``approximate_size``."""
    def skip(detail, label, count=1):
        report.add(SKIPPED, "omitted", detail, sheet=sheet_name, cell=label[:32], count=count)

    if not isinstance(parts, ChartParts):
        parts = ChartParts(parts)
    if drawing_id is None:
        return []
    names = parts.names(archive)
    related = _relationships(archive, sheet_part, parts, names)
    link = related.get(drawing_id) if related is not None else None
    drawing = _resolve(sheet_part, link[1], names) if link is not None and link[0] == "drawing" else None
    room = MAX_FRAMES - parts.frames
    found = parts.drawing(drawing, lambda: _drawing_frames(archive, drawing, parts, names, room)) if drawing is not None else None
    if found is None:
        skip("unreadable", "drawing")
        return []
    entries, count = found
    drafts, unread = [], count - len(entries)
    for order, entry in enumerate(entries):
        parts.tick()
        if parts.frames >= MAX_FRAMES:
            unread += 1
            continue
        parts.frames += 1
        label = entry["label"]
        if entry["grouped"]:
            skip("grouped", label)
            continue
        chart = entry["chart"]
        body = None
        if entry["placed"] is not None and chart is not None:
            body = _extended() if chart is EXTENDED else parts.body(archive, chart, _read_chart)
        if body is LEFT_OUT:
            skip("workbook_limit", label)
            continue
        if body is None:
            skip("unreadable", label)
            continue
        cell, offset, size, absolute = entry["placed"]
        draft = parts.draft(body, {"sheet": position, "name": label, "order": order,
                                   "anchor": {"row": cell[0], "col": cell[1]}, "offset": {"x": offset[0], "y": offset[1]},
                                   "size": {"width": size[0], "height": size[1]},
                                   "approximate_size": sizes_unknown and not entry["one_cell"], "absolute_anchor": absolute}, report)
        if draft is not None:
            drafts.append(draft)
    if unread:
        skip("unreadable", "drawing", unread)
    return drafts


def sizes_declared(features) -> bool:
    """Whether a reported worksheet's ``SheetFeatures`` found custom column widths, row heights or sheet defaults.

    Widths within the feature scanner's tolerance of the sheet's default are not custom; the readers measure with
    default cell sizes, so only a custom size makes a measured extent approximate."""
    return bool({"column_widths", "row_heights", "sheet_format_defaults"} & set(features.counts))


def worksheet_drafts(archive, sheet_part, position, sheet_name, parts, report: ConversionReport, features) -> list:
    """``read_sheet_drafts`` for a worksheet the whole-workbook readers have read, given its ``SheetFeatures``.

    ``features`` must have reported.  Only a worksheet with a ``<drawing>`` element shows charts, so no other is read,
    and only the relationship its first ``<drawing>`` names is followed (a worksheet has at most one; any other is
    reported as one unreadable drawing).  Its column widths and row heights are not kept per cell, so a sheet with
    custom sizes has its measured drafts marked ``approximate_size``.  ``parts`` is the import's ``ChartParts``."""
    if "drawings" not in features.counts:
        return []
    if features.drawings > 1:
        report.add(SKIPPED, "omitted", "unreadable", sheet=sheet_name, cell="drawing", count=features.drawings - 1)
    return read_sheet_drafts(archive, sheet_part, position, sheet_name, parts, report, features.drawing_id,
                             sizes_unknown=sizes_declared(features))


# Writing.
#
# ``chart_part`` and ``package`` write the parts of an export draft with literal prefixed names (``c:chart``) and
# ``xmlns:`` attributes, like the workbook parts of ``interchange``: the elements serialize as they are.

NS_C = "http://schemas.openxmlformats.org/drawingml/2006/chart"
NS_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
NS_XDR = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS_PACKAGE = "http://schemas.openxmlformats.org/package/2006/relationships"
CHART_TYPE = "application/vnd.openxmlformats-officedocument.drawingml.chart+xml"
DRAWING_TYPE = "application/vnd.openxmlformats-officedocument.drawing+xml"
LEGEND_CODES = {"right": "r", "bottom": "b"}
UNWRITABLE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")  # what XML 1.0 cannot hold
LINE_BREAK = re.compile("\r\n|\r|\n")


def _text(text):
    """``text`` without the characters XML 1.0 cannot hold; carriage returns become line feeds, as a parser reads them."""
    return LINE_BREAK.sub("\n", UNWRITABLE.sub("", text))


def _add(parent, tag, text=None, **attributes):
    element = ET.SubElement(parent, tag, attributes)
    if text is not None:
        element.text = text
    return element


def _title_element(parent, text):
    title = _add(parent, "c:title")
    rich = _add(_add(title, "c:tx"), "c:rich")
    _add(rich, "a:bodyPr")
    _add(rich, "a:lstStyle")
    for line in _text(text).split("\n"):
        _add(_add(_add(rich, "a:p"), "a:r"), "a:t", line)
    _add(title, "c:overlay", val="0")


def _reference(parent, tag, kind, ref):
    _add(_add(_add(parent, tag), "c:" + kind), "c:f", format_reference(ref, "xlsx"))


def _series_element(plot, number, draft, item):
    kind = draft["type"]
    series = _add(plot, "c:ser")
    _add(series, "c:idx", val=str(number))
    _add(series, "c:order", val=str(number))
    name = item["name"]
    if name is not None:
        if name[0] == "ref":
            _reference(series, "c:tx", "strRef", name[1])
        else:
            _add(_add(series, "c:tx"), "c:v", _text(name[1]))
    if kind == "scatter":
        shape = _add(series, "c:spPr")
        _add(_add(shape, "a:ln", w="19050"), "a:noFill")  # markers only: a scatter chart joins no points
        _reference(series, "c:xVal", "numRef", item["x"])
        _reference(series, "c:yVal", "numRef", item["values"])
        return
    if draft["categories"] is not None:
        _reference(series, "c:cat", "strRef", draft["categories"])
    _reference(series, "c:val", "numRef", item["values"])


def _plot_element(area, draft):
    kind, grouping = draft["type"], draft["grouping"]
    tag = {"column": "c:barChart", "bar": "c:barChart", "line": "c:lineChart", "area": "c:areaChart",
           "pie": "c:pieChart", "scatter": "c:scatterChart"}[kind]
    plot = _add(area, tag)
    if kind in ("column", "bar"):
        _add(plot, "c:barDir", val="col" if kind == "column" else "bar")
        _add(plot, "c:grouping", val="stacked" if grouping == "stacked" else "clustered")
    elif kind in ("line", "area"):
        _add(plot, "c:grouping", val="stacked" if grouping == "stacked" and kind == "area" else "standard")
    elif kind == "scatter":
        _add(plot, "c:scatterStyle", val="lineMarker")
    _add(plot, "c:varyColors", val="1" if kind == "pie" else "0")
    for number, item in enumerate(draft["series"]):
        _series_element(plot, number, draft, item)
    if kind in ("column", "bar") and grouping == "stacked":
        _add(plot, "c:overlap", val="100")  # Excel draws stacked bars side by side without it
    if kind != "pie":
        _add(plot, "c:axId", val="1")
        _add(plot, "c:axId", val="2")


def _axis_element(area, tag, number, position, other, title, low=None, high=None, **extras):
    axis = _add(area, tag)
    _add(axis, "c:axId", val=str(number))
    scaling = _add(axis, "c:scaling")
    _add(scaling, "c:orientation", val="minMax")
    if high is not None:
        _add(scaling, "c:max", val=repr(float(high)))
    if low is not None:
        _add(scaling, "c:min", val=repr(float(low)))
    _add(axis, "c:delete", val="0")
    _add(axis, "c:axPos", val=position)
    if title is not None:
        _title_element(axis, title)
    _add(axis, "c:crossAx", val=str(other))
    _add(axis, "c:crosses", val="autoZero")
    for name, value in extras.items():
        _add(axis, "c:" + name, val=value)


def chart_part(draft):
    """The ``c:chartSpace`` of an export draft: no cached values, no styling (the native chart has none)."""
    kind = draft["type"]
    if not draft["series"]:
        raise ValueError("a chart needs a series")
    root = ET.Element("c:chartSpace", {"xmlns:c": NS_C, "xmlns:a": NS_A, "xmlns:r": NS_R})
    chart = _add(root, "c:chart")
    title = draft["title"] if draft["title"] and _text(draft["title"]) else None
    if title is not None:
        _title_element(chart, title)
    _add(chart, "c:autoTitleDeleted", val="0" if title is not None else "1")
    area = _add(chart, "c:plotArea")
    _add(area, "c:layout")
    _plot_element(area, draft)
    if kind != "pie":
        titles = [draft[key] if draft[key] and _text(draft[key]) else None for key in ("category_title", "value_title")]
        if kind == "scatter":
            _axis_element(area, "c:valAx", 1, "b", 2, titles[0], crossBetween="midCat")
            _axis_element(area, "c:valAx", 2, "l", 1, titles[1], draft["min"], draft["max"], crossBetween="midCat")
        else:
            horizontal = kind == "bar"
            _axis_element(area, "c:catAx", 1, "l" if horizontal else "b", 2, titles[0])
            _axis_element(area, "c:valAx", 2, "b" if horizontal else "l", 1, titles[1], draft["min"], draft["max"],
                          crossBetween="between")
    if draft["legend"] in LEGEND_CODES:
        legend = _add(chart, "c:legend")
        _add(legend, "c:legendPos", val=LEGEND_CODES[draft["legend"]])
        _add(legend, "c:overlay", val="0")
    _add(chart, "c:plotVisOnly", val="1")
    return root


def _relationships_part(items):
    root = ET.Element("Relationships", {"xmlns": NS_PACKAGE})
    for identifier, kind, target in items:
        _add(root, "Relationship", Id=identifier, Type=f"{NS_R}/{kind}", Target=target)
    return root


def _frame(number, draft):
    anchor = ET.Element("xdr:oneCellAnchor")
    corner = _add(anchor, "xdr:from")
    place, offset, size = draft["anchor"], draft["offset"], draft["size"]
    _add(corner, "xdr:col", str(place["col"] - 1))
    _add(corner, "xdr:colOff", str(offset["x"] * EMU_PER_PIXEL))
    _add(corner, "xdr:row", str(place["row"] - 1))
    _add(corner, "xdr:rowOff", str(offset["y"] * EMU_PER_PIXEL))
    _add(anchor, "xdr:ext", cx=str(size["width"] * EMU_PER_PIXEL), cy=str(size["height"] * EMU_PER_PIXEL))
    frame = _add(anchor, "xdr:graphicFrame", macro="")
    properties = _add(frame, "xdr:nvGraphicFramePr")
    _add(properties, "xdr:cNvPr", id=str(number + 1), name=_text(draft["name"]))
    _add(properties, "xdr:cNvGraphicFramePr")
    transform = _add(frame, "xdr:xfrm")
    _add(transform, "a:off", x="0", y="0")
    _add(transform, "a:ext", cx="0", cy="0")
    data = _add(_add(frame, "a:graphic"), "a:graphicData", uri=NS_C)
    _add(data, "c:chart", **{"r:id": f"rId{number}"})
    _add(anchor, "xdr:clientData")
    return anchor


def package(built, names):
    """The parts that carry the built charts of an export of the sheets ``names`` (in order).

    ``built`` is ``export_charts.build``'s ``(draft, chart part)`` pairs.  Returns ``(parts, rids, overrides)``: the
    ``(path, element)`` of every chart part (``xl/charts/chartK.xml``, K counting the charts in draft order), of each
    drawing of a sheet that has charts (``xl/drawings/drawingN.xml``, N the 1-based sheet index, frames in draft order)
    with its relationships, and of the sheet's relationships (one, ``rId1``, to its drawing); ``rids`` maps the 1-based
    index of each such sheet to the id its ``<drawing r:id>`` carries; ``overrides`` are the ``(part name, content
    type)`` of the chart and drawing parts.  A sheet's relationships part is new: the caller adds the id's relationship
    to one it already writes."""
    charts, by_sheet = [], {}
    for number, (draft, part) in enumerate(built, 1):
        charts.append((f"xl/charts/chart{number}.xml", part))
        by_sheet.setdefault(names.index(draft["sheet"]) + 1, []).append((number, draft))
    parts, rids, overrides = list(charts), {}, [(path, CHART_TYPE) for path, _ in charts]
    for index in sorted(by_sheet):
        drawing = ET.Element("xdr:wsDr", {"xmlns:xdr": NS_XDR, "xmlns:a": NS_A, "xmlns:r": NS_R, "xmlns:c": NS_C})
        links = []
        for position, (number, draft) in enumerate(by_sheet[index], 1):
            drawing.append(_frame(position, draft))
            links.append((f"rId{position}", "chart", f"../charts/chart{number}.xml"))
        parts += [(f"xl/drawings/drawing{index}.xml", drawing),
                  (f"xl/drawings/_rels/drawing{index}.xml.rels", _relationships_part(links)),
                  (f"xl/worksheets/_rels/sheet{index}.xml.rels", _relationships_part([("rId1", "drawing", f"../drawings/drawing{index}.xml")]))]
        rids[index] = "rId1"
        overrides.append((f"xl/drawings/drawing{index}.xml", DRAWING_TYPE))
    return parts, rids, overrides
